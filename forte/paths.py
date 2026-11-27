"""The two ways to reach IonQ Forte, behind one interface.

A path knows how to submit a task to the device, with or without the hold's
credential, how to read the task back with its timestamps and queue, how to
open and close the hold, and what things cost. The trial only talks to this
interface, so a trial reads the same whichever way the device was reached.

The hold is opened with the backend classes, with the same calls the scout
makes, so what is measured is what the pipeline does.

    braket   Forte-1 through AWS Braket. The hold is a hybrid job whose token
             puts a task in the Priority queue, or a probe task. Braket says
             which queue a task waited in and where.
    ionq     Forte-1 through IonQ's own API. The hold is a session, which the
             trial refuses unless told otherwise since a session that runs a
             job bills an hour of device time, or a probe job. IonQ gives a
             job's submitted, started and completed times.

Both submit the same one qubit circuit, a Hadamard, measured.
"""

import datetime
import json
import os
import time
import uuid

# Braket bills a hybrid job's instance by the minute, an ml.m5.large
HOLD_PER_HOUR = 0.115

# what Braket charges per task and per shot on Forte when the service cannot
# be asked. Read from the device properties in October 2026: $0.08 a shot,
# and a task has to ask for at least 100 shots, so the cheapest task is $8.30
BRAKET_TASK_FEE = 0.30
BRAKET_SHOT = 0.08
BRAKET_MIN_SHOTS = 100

# what a direct account was quoted for a small circuit on Forte, and the
# cost a small job never gets above
IONQ_JOB_MINIMUM = 25.79

BRAKET_FORTE = "arn:aws:braket:us-east-1::device/qpu/ionq/Forte-1"
IONQ_FORTE = "qpu.forte-1"

TERMINAL = ("COMPLETED", "FAILED", "CANCELLED")

ONE_QUBIT_QASM = """OPENQASM 3;
qubit[1] q;
bit[1] c;
h q[0];
c[0] = measure q[0];
"""
ONE_QUBIT_IONQ = {
    "qubits": 1,
    "gateset": "qis",
    "circuit": [{"gate": "h", "target": 0}],
}


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def parse_iso(s):
    """A service timestamp as seconds since the epoch, or None."""
    if not s:
        return None
    if isinstance(s, datetime.datetime):
        d = s
    else:
        try:
            d = datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=datetime.timezone.utc)
    return d.timestamp()


class Hold:
    """The hold, opened and closed with the backend's own methods.

    credential is what a task submits with once the hold is ready: the job
    token on Braket, the session id on IonQ, None for a probe, which holds
    nothing a task can be submitted into.
    """

    def __init__(self, backend, options):
        self.backend = backend
        self.options = options
        self.opened = None
        self.credential = None
        self.opened_at = None
        self.ready_at = None
        self.why = None

    def open(self):
        self.opened_at = time.time()
        self.opened = self.backend.open_session(self.options)
        return self.opened

    def wait(self, interval=5.0):
        ok, why = self.backend.wait_for_priority(self.options, interval=interval)
        self.why = why
        if not ok:
            raise RuntimeError("the hold never became ours: {}".format(why))
        self.ready_at = time.time()
        sid = self.backend.session_id(self.opened)
        # a probe hands over a marker and not something to submit into
        self.credential = None if str(sid).startswith("job:") else sid
        if self.options.get("hold") == "probe":
            self.credential = None
        return self.credential

    def close(self):
        if self.opened is None:
            return
        try:
            self.backend.close_session(self.opened)
        finally:
            self.opened = None

    @property
    def wait_s(self):
        if self.opened_at is None or self.ready_at is None:
            return None
        return self.ready_at - self.opened_at

    def record(self):
        return {
            "id": str(self.opened) if self.opened else None,
            "mode": self.options.get("hold"),
            "opened_at": self.opened_at,
            "ready_at": self.ready_at,
            "wait_s": self.wait_s,
            "why": self.why,
            "credential_kind": (
                "none" if self.credential is None else type(self.credential).__name__
            ),
        }


class BraketPath:
    name = "braket"

    def __init__(self, device=BRAKET_FORTE, hold="session"):
        import boto3
        from flux_quantum.backends.braket import region_for

        self.device = device
        self.hold_mode = hold
        self.region = region_for(device)
        os.environ["AWS_DEFAULT_REGION"] = self.region
        self.client = boto3.client("braket", region_name=self.region)
        self._bucket = None
        self._hold_job = None

    # ---- the device ------------------------------------------------------
    def device_state(self):
        """Queue and status before the trial, from the service."""
        from flux_quantum.backends.braket import is_open, queue_depth
        from braket.aws import AwsDevice

        out = {}
        try:
            out.update(queue_depth(self.device))
        except Exception as e:
            out["queue_error"] = str(e)
        try:
            d = AwsDevice(self.device)
            out["status"] = d.status
            out["open"] = is_open(d)
        except Exception as e:
            out["status_error"] = str(e)
        return out

    def task_cost(self, shots):
        """What one task costs, from the service when it answers."""
        try:
            from braket.aws import AwsDevice
            from flux_quantum.backends.braket import cost

            c = cost(AwsDevice(self.device), shots)
            if c is not None:
                return c
        except Exception:
            pass
        return BRAKET_TASK_FEE + BRAKET_SHOT * max(shots, BRAKET_MIN_SHOTS)

    def hold_cost(self, seconds):
        return HOLD_PER_HOUR * seconds / 3600.0 if self.hold_mode == "session" else 0.0

    # ---- tasks -----------------------------------------------------------
    def bucket(self):
        if self._bucket is None:
            from braket.aws import AwsSession

            self._bucket = AwsSession().default_bucket()
        return self._bucket

    def submit(self, shots, credential=None, label="task", program=None):
        """One task, with the job token when there is one. Returns its id.

        program is an OpenQASM 3 source string; without one the task runs
        the default one qubit circuit."""
        args = {
            "clientToken": str(uuid.uuid4()),
            "deviceArn": self.device,
            "shots": shots,
            "outputS3Bucket": self.bucket(),
            "outputS3KeyPrefix": "flux-quantum-experiment/{}".format(label),
            "action": json.dumps(
                {
                    "braketSchemaHeader": {
                        "name": "braket.ir.openqasm.program",
                        "version": "1",
                    },
                    "source": program or ONE_QUBIT_QASM,
                }
            ),
        }
        if credential:
            args["jobToken"] = credential
        return self.client.create_quantum_task(**args)["quantumTaskArn"]

    def result(self, task_id, qubits=None):
        """Measured weights for a finished task, {state: count}. Character
        i of a state is qubit i, which is how Braket orders its keys."""
        from braket.aws import AwsQuantumTask

        r = AwsQuantumTask(task_id).result()
        return {str(k): float(v) for k, v in r.measurement_counts.items()}

    def describe(self, task_id):
        """The task with its queue. The queue only comes back when asked for,
        and only while the task is waiting."""
        t = self.client.get_quantum_task(
            quantumTaskArn=task_id, additionalAttributeNames=["QueueInfo"]
        )
        q = t.get("queueInfo") or {}
        position = q.get("position")
        if position in ("None", ""):
            position = None
        return {
            "status": t.get("status"),
            "in_hold": bool(t.get("jobArn")),
            "hold_id": t.get("jobArn"),
            "queue": q.get("queue"),
            "position": position,
            "priority": q.get("queuePriority"),
            "created": parse_iso(t.get("createdAt")),
            "started": None,  # Braket does not say when a task started
            "ended": parse_iso(t.get("endedAt")),
            "terminal": t.get("status") in TERMINAL,
            "cost": None,
        }

    def cancel(self, task_id):
        try:
            self.client.cancel_quantum_task(
                quantumTaskArn=task_id, clientToken=str(uuid.uuid4())
            )
            return True
        except Exception as e:
            if "COMPLETED" not in str(e):
                print("braket: could not cancel {}: {}".format(task_id[-12:], e))
            return False

    # ---- the hold --------------------------------------------------------
    def hold(self, max_seconds, wait_seconds):
        from flux_quantum.backends import get_backend

        backend = get_backend("braket")
        options = backend.scout_options(
            {
                "hold": self.hold_mode,
                "device": self.device,
                "hold_max": max_seconds,
                "wait": wait_seconds,
            }
        )
        return Hold(backend, options)

    def credential_from_environment(self):
        """What the scout handed a classical job: the token, or nothing for
        a probe."""
        sid = os.environ.get("QUANTUM_SESSION_ID")
        if not sid or sid.startswith("job:") or sid.startswith("arn:"):
            return None
        return sid


class IonQPath:
    name = "ionq"

    def __init__(self, device=IONQ_FORTE, hold="probe", allow_session=False):
        from flux_quantum.backends.ionq.client import API_URL, Client

        key = os.environ.get("IONQ_API_KEY") or os.environ.get("IONQ_API_TOKEN")
        if not key:
            raise RuntimeError("ionq: no IONQ_API_KEY in the environment")
        if hold == "session" and not allow_session:
            raise RuntimeError(
                "ionq: a session that runs a job bills an hour of device time "
                "on Forte. Pass --allow-ionq-session if that is intended, or "
                "hold with probe"
            )
        self.device = device
        self.hold_mode = hold
        self.client = Client(key, os.environ.get("IONQ_API_URL") or API_URL)

    def device_state(self):
        try:
            b = self.client.get("/backends/{}".format(self.device))
        except Exception as e:
            return {"status_error": str(e)}
        return {
            "status": b.get("status"),
            "degraded": b.get("degraded"),
            "average_queue_time": b.get("average_queue_time"),
            "open": b.get("status") == "available",
        }

    def task_cost(self, shots):
        """IonQ's own estimate for the circuit, or the quoted minimum."""
        try:
            e = self.client.get(
                "/jobs/estimate?backend={}&shots={}&qubits=1&1q_gates=1&2q_gates=0".format(
                    self.device, shots
                )
            )
            c = e.get("estimated_total_cost")
            if c is not None:
                return float(c)
        except Exception:
            pass
        return IONQ_JOB_MINIMUM

    def hold_cost(self, seconds):
        # a session's hour minimum is charged when it ends, by the device,
        # and the probe is one job, counted with the tasks
        return 0.0

    def submit(self, shots, credential=None, label="task", program=None):
        """program is an IonQ circuit dict, {"qubits", "gateset", "circuit"};
        without one the job runs the default one qubit circuit."""
        body = {
            "type": "ionq.circuit.v1",
            "name": "flux-quantum-experiment-{}".format(label),
            "metadata": {"flux_quantum": "experiment", "label": label},
            "shots": shots,
            "backend": self.device,
            "input": program or ONE_QUBIT_IONQ,
        }
        if credential:
            body["session_id"] = credential
        noise = os.environ.get("IONQ_NOISE_MODEL")
        if noise and self.device == "simulator":
            body["noise"] = {"model": noise}
        return self.client.post("/jobs", body)["id"]

    def describe(self, task_id):
        j = self.client.get("/jobs/{}".format(task_id))
        status = (j.get("status") or "").lower()
        terminal = status in ("completed", "failed", "canceled")
        return {
            "status": status,
            "in_hold": bool(j.get("session_id")),
            "hold_id": j.get("session_id"),
            "queue": "session" if j.get("session_id") else "public",
            "position": None,
            "priority": None,
            "created": parse_iso(j.get("submitted_at")),
            "started": parse_iso(j.get("started_at")),
            "ended": parse_iso(j.get("completed_at") or j.get("ended_at")),
            "terminal": terminal,
            "cost": self._cost(task_id) if terminal else None,
        }

    def result(self, task_id, qubits=1):
        """Measured weights for a finished job, {state: probability}.

        IonQ keys its histogram by the decimal value of the measured state.
        This takes qubit 0 as the least significant bit, which is what the
        fake service does; check the convention once per real device with a
        state preparation circuit before trusting a long run.
        """
        out = {}
        histogram = self.client.get("/jobs/{}/results".format(task_id))
        for key, p in histogram.items():
            k = int(key)
            state = "".join("1" if (k >> i) & 1 else "0" for i in range(qubits))
            out[state] = float(p)
        return out

    def _cost(self, task_id):
        try:
            c = self.client.get("/jobs/{}/cost".format(task_id))
            v = (c.get("cost") or {}).get("value")
            return None if v is None else float(v)
        except Exception:
            return None

    def cancel(self, task_id):
        try:
            if self.describe(task_id)["terminal"]:
                return False
            self.client.put("/jobs/{}/status/cancel".format(task_id))
            return True
        except Exception as e:
            print("ionq: could not cancel {}: {}".format(task_id, e))
            return False

    def hold(self, max_seconds, wait_seconds):
        from flux_quantum.backends import get_backend

        backend = get_backend("ionq")
        options = backend.scout_options(
            {
                "hold": self.hold_mode,
                "device": self.device,
                "hold_max": max_seconds,
                "wait": wait_seconds,
            }
        )
        return Hold(backend, options)

    def credential_from_environment(self):
        sid = os.environ.get("QUANTUM_SESSION_ID")
        if not sid or sid.startswith("job:"):
            return None
        return sid


def make_path(name, device=None, hold=None, allow_ionq_session=False):
    if name == "braket":
        return BraketPath(device or BRAKET_FORTE, hold or "session")
    if name == "ionq":
        return IonQPath(device or IONQ_FORTE, hold or "probe", allow_ionq_session)
    raise ValueError("path is braket or ionq, not {}".format(name))
