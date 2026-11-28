"""The ways to reach a quantum device, behind one interface.

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
    ibm      An IBM device through QRMI, the third vendor door. The hold is
             a qiskit-runtime-service session (batch mode: billed for
             hardware time only). Tasks are SamplerV2 jobs; QRMI exposes
             status but no service timestamps and no queue position.

The default task is the same one qubit circuit everywhere: a Hadamard,
measured (measure only on ibm, where an untranspiled h is not ISA).
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

# what IBM bills a qiskit-runtime-service job: the pay-as-you-go rate per
# QPU second (ibm.com/quantum pricing, November 2026) times roughly what
# one small 100 shot sampler task uses. IBM has no estimate endpoint, so
# unlike the other two vendors this is never refreshed from the service.
IBM_PER_SECOND = 1.60
IBM_TASK_SECONDS = 2.05

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
# measure only, so it is ISA valid on any IBM backend without transpiling,
# the same dodge the scout's warmup task uses
ONE_QUBIT_MEASURE_QASM = """OPENQASM 3.0;
include "stdgates.inc";
bit[1] c;
qubit[1] q;
c[0] = measure q[0];
"""


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


class IBMPath:
    """An IBM device through QRMI, the same calls the ibm backend's scout
    makes. Needs qrmi[ibm] (python 3.11+), which brings qiskit and the
    runtime result decoder.

    QRMI differs from the other two services in three ways this class has
    to absorb. The credential travels in the environment, not in the task:
    QRMI reads <resource>_QRMI_JOB_ACQUISITION_TOKEN when the resource
    handle is made, so submit rebuilds the handle when the credential
    changes. The service reports status only - no created/started/ended
    timestamps and no queue position - so created and ended are stamped
    here by the polls that first saw them, the same stand-in trial.follow
    already uses for started. And IBM requires ISA circuits: program_from_ops
    transpiles against the backend target before submitting.
    """

    name = "ibm"

    def __init__(self, device=None, hold="session"):
        from qrmi import Payload, QuantumResource, TaskStatus
        from flux_quantum.backends.qrmi import (
            missing_env,
            resource_type,
            resources_in_environment,
        )
        from flux_quantum.keys import ACQUISITION_TOKEN

        self.rtype = os.environ.get("QRMI_JOB_QPU_TYPES") or "qiskit-runtime-service"
        if not device:
            # inside a flux pair the wrap says which resource the scout
            # holds; otherwise the one resource with credentials configured
            device = (
                (os.environ.get("QRMI_JOB_QPU_RESOURCES") or "")
                .split(",")[0]
                .strip()
            )
        if not device:
            found = resources_in_environment(self.rtype)
            if len(found) == 1:
                device = found[0]
            else:
                raise RuntimeError(
                    "ibm: pass --device (the QRMI resource id, e.g. "
                    "ibm_kingston); credentials found for: {}".format(
                        ", ".join(found) or "none"
                    )
                )
        missing = missing_env(device, self.rtype)
        if missing:
            raise RuntimeError(
                "ibm: {} is missing {}".format(
                    device, ", ".join(device + s for s in missing)
                )
            )
        self.device = device
        self.hold_mode = hold
        self._Payload = Payload
        self._TaskStatus = TaskStatus
        self._QuantumResource = QuantumResource
        self._rtype_attr = resource_type(self.rtype)
        self._token_var = device + ACQUISITION_TOKEN
        self._handle = None
        self._handle_token = None
        self._backend = None
        self._tasks = {}  # id -> what QRMI will not tell us later

    def _qr(self, credential=None):
        """The QRMI resource handle, rebuilt when the credential changed."""
        if credential and os.environ.get(self._token_var) != str(credential):
            os.environ[self._token_var] = str(credential)
        token = os.environ.get(self._token_var)
        if self._handle is None or token != self._handle_token:
            self._handle = self._QuantumResource(self.device, self._rtype_attr)
            self._handle_token = token
            self._backend = None
        return self._handle

    def _status_name(self, status):
        for name in ("Queued", "Running", "Completed", "Failed", "Cancelled"):
            if status == getattr(self._TaskStatus, name, None):
                return name
        return str(status).rsplit(".", 1)[-1] or "UNKNOWN"

    # ---- the device --------------------------------------------------------
    def backend(self):
        """Qiskit BackendV2 for the resource, carrying the ISA target."""
        if self._backend is None:
            from qrmi.primitives.ibm import get_backend

            self._backend = get_backend(self._qr())
        return self._backend

    def device_state(self):
        out = {"resource": self.device, "type": self.rtype}
        try:
            out["open"] = bool(self._qr().is_accessible())
        except Exception as e:
            out["status_error"] = str(e)
        try:
            m = self._qr().metadata()
            out["metadata"] = m if isinstance(m, dict) else str(m)
        except Exception:
            pass
        return out

    def task_cost(self, shots):
        return IBM_PER_SECOND * IBM_TASK_SECONDS

    def hold_cost(self, seconds):
        # a batch session bills hardware time only, which the tasks already
        # count. A dedicated session would bill the wall clock instead; this
        # path assumes batch, the backend's default
        return 0.0

    # ---- tasks -------------------------------------------------------------
    def program_from_ops(self, n, ops):
        """Lowered app ops -> ISA OpenQASM 3 for this backend.

        IBM rejects circuits outside the device basis, so unlike the other
        paths the circuit is transpiled client side, against the target QRMI
        hands over. The classical register keeps qiskit's default name c and
        clbit i stays qubit i through the layout, which result() relies on.
        """
        from qiskit import QuantumCircuit, qasm3
        from qiskit.transpiler.preset_passmanagers import (
            generate_preset_pass_manager,
        )

        qc = QuantumCircuit(n, n)
        for op in ops:
            if op[0] == "x":
                qc.x(op[1])
            elif op[0] == "h":
                qc.h(op[1])
            elif op[0] == "rx":
                qc.rx(op[2], op[1])
            elif op[0] == "rz":
                qc.rz(op[2], op[1])
            elif op[0] == "cnot":
                qc.cx(op[1], op[2])
            else:
                raise ValueError("no ibm gate for {}".format(op[0]))
        qc.measure(range(n), range(n))
        pm = generate_preset_pass_manager(
            optimization_level=1, backend=self.backend()
        )
        return qasm3.dumps(
            pm.run(qc), disable_constants=True, allow_aliasing=True
        )

    def submit(self, shots, credential=None, label="task", program=None):
        """One SamplerV2 task. program is ISA OpenQASM 3 from
        program_from_ops; without one the task runs the measure-only one
        qubit circuit. The credential is not in the request: QRMI submits
        into the session whose token is in the environment."""
        qr = self._qr(credential)
        input_json = {
            "pubs": [[program or ONE_QUBIT_MEASURE_QASM, None, shots]],
            "shots": shots,
            "options": {},
            "version": 2,
            "support_qiskit": True,
        }
        payload = self._Payload.QiskitPrimitive(
            input=json.dumps(input_json), program_id="sampler"
        )
        task = str(qr.task_start(payload))
        self._tasks[task] = {
            "created": time.time(),
            "in_hold": bool(os.environ.get(self._token_var)),
            "ended": None,
        }
        return task

    def describe(self, task_id):
        rec = self._tasks.setdefault(
            task_id, {"created": None, "in_hold": False, "ended": None}
        )
        try:
            name = self._status_name(self._qr().task_status(task_id))
        except Exception as e:
            name = "UNKNOWN ({})".format(e)
        terminal = name in ("Completed", "Failed", "Cancelled")
        if terminal and rec.get("ended") is None:
            # QRMI gives no end time; the poll that saw it terminal stands
            # in, as follow's first-running sighting does for started
            rec["ended"] = time.time()
        return {
            "status": name.upper(),
            "in_hold": rec.get("in_hold", False),
            "hold_id": (
                os.environ.get(self._token_var) if rec.get("in_hold") else None
            ),
            "queue": "session" if rec.get("in_hold") else "fair-share",
            "position": None,
            "priority": None,
            "created": rec.get("created"),
            "started": None,
            "ended": rec.get("ended"),
            "terminal": terminal,
            "cost": None,
        }

    def result(self, task_id, qubits=None):
        """Measured weights for a finished task, {state: count}. Character
        i of a state is qubit i: the runtime decoder returns qiskit's order,
        clbit n-1 leftmost, so the keys are reversed here. Retrieval
        consumes the result on the QRMI side, so it is kept."""
        rec = self._tasks.setdefault(task_id, {})
        if "weights" in rec:
            return rec["weights"]
        try:
            from qiskit_ibm_runtime.decoders.result_decoder import ResultDecoder
        except ImportError:  # qiskit-ibm-runtime < 0.48
            from qiskit_ibm_runtime.utils.result_decoder import ResultDecoder

        raw = self._qr().task_result(task_id).value
        data = ResultDecoder.decode(raw)[0].data
        bits = getattr(data, "c", None)
        if bits is None:  # a register not named c: take the one there is
            bits = next(iter(data.values()))
        rec["weights"] = {
            str(k)[::-1]: float(v) for k, v in bits.get_counts().items()
        }
        return rec["weights"]

    def cancel(self, task_id):
        try:
            if self.describe(task_id)["terminal"]:
                return False
            self._qr().task_stop(task_id)
            return True
        except Exception as e:
            print("ibm: could not cancel {}: {}".format(task_id, e))
            return False

    # ---- the hold ----------------------------------------------------------
    def hold(self, max_seconds, wait_seconds):
        from flux_quantum.backends import get_backend

        backend = get_backend("ibm")
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
    if name == "ibm":
        return IBMPath(device, hold or "session")
    raise ValueError("path is braket, ionq or ibm, not {}".format(name))
