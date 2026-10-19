#!/usr/bin/env python3
"""Sample what each service says about its queue, every minute, for days.

    sample.py --once                              one tick, printed
    sample.py --out results/queues.jsonl          until stopped
    sample.py --vendors braket,ionq               only those

One pass over every vendor is a tick, and every row carries its tick, so a
machine Braket and IonQ both serve is read through both doors within the
same few seconds. Reading is free: Braket's queue depth, IonQ's backend list
and IBM's backend status are all metadata calls.

Each vendor is tried on its own, so one that is down, or has no credentials
here, costs the others nothing, and the failure is a row too. Stop it with
SIGTERM or SIGINT and the file is left whole.

Nothing is ever submitted. The queue is read, not joined, so the samples
are what every user of the device sees and cost nothing.

Credentials come from the environment: AWS for Braket, IONQ_API_KEY for
IonQ, and for IBM either QISKIT_IBM_TOKEN with QISKIT_IBM_INSTANCE, or
IBM_CLOUD_TOKEN with IBM_CLOUD_CRN, or the <resource>_QRMI_IBM_QRS_*
variables QRMI reads.
"""

import argparse
import datetime
import json
import os
import signal
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def now():
    return time.time()


def stamp(t=None):
    return datetime.datetime.fromtimestamp(
        t or now(), datetime.timezone.utc
    ).isoformat()


def plain(obj):
    """Whatever a call returned, as something JSON can hold. SDK objects
    offer to_dict, dict or json; anything else is kept as its string."""
    for attr in ("to_dict", "dict"):
        f = getattr(obj, attr, None)
        if callable(f):
            try:
                return plain(f())
            except Exception:
                pass
    f = getattr(obj, "json", None)
    if callable(f):
        try:
            return json.loads(f())
        except Exception:
            pass
    if isinstance(obj, dict):
        return {str(k): plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [plain(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    if isinstance(obj, datetime.datetime):
        return obj.isoformat()
    return str(obj)


def digest(obj):
    import hashlib

    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]


class Unchanged:
    """Large things a service repeats every call, like a device's full
    properties, are kept once per change rather than once per tick. Every
    tick still carries the digest, so the record says which version was in
    force, and the full text is in the row where it first appeared."""

    def __init__(self):
        self.seen = {}

    def __call__(self, key, obj):
        """(digest, obj or None): the object when it is new or changed."""
        d = digest(obj)
        if self.seen.get(key) == d:
            return d, None
        self.seen[key] = d
        return d, obj


class Braket:
    name = "braket"

    def __init__(self, devices):
        import boto3
        from braket.aws import AwsDevice

        self.devices = {}
        self.clients = {}
        self.changed = Unchanged()
        for arn in devices:
            region = arn.split(":")[3] or "us-east-1"
            if region not in self.clients:
                self.clients[region] = boto3.client("braket", region_name=region)
            self.devices[arn] = AwsDevice(arn)

    def sample(self):
        from flux_quantum.backends.braket import is_open
        from braket.aws.queue_information import QueueType

        for arn, d in self.devices.items():
            try:
                t_start = now()
                q = d.queue_depth()

                def count(v):
                    try:
                        return int(v)
                    except (TypeError, ValueError):
                        return 2000 if ">" in str(v) else None

                t_q = now()
                d.refresh_metadata()
                t_m = now()
                # the whole device record, kept in full whenever it changes
                props = plain(d.properties)
                pd, fresh = self.changed(arn, props)
                row = {
                    "device": arn,
                    "normal": count(q.quantum_tasks.get(QueueType.NORMAL)),
                    "priority": count(q.quantum_tasks.get(QueueType.PRIORITY)),
                    "jobs": count(q.jobs),
                    "jobs_note": str(q.jobs or ""),
                    "status": d.status,
                    "open": is_open(d),
                    "raw": {
                        "queue_depth": plain(q),
                        "status": d.status,
                        "name": getattr(d, "name", None),
                        "provider": getattr(d, "provider_name", None),
                        "type": str(getattr(d, "type", "")),
                        "properties_digest": pd,
                    },
                    "elapsed_s": {"queue_depth": t_q - t_start, "metadata": t_m - t_q},
                }
                if fresh is not None:
                    row["properties"] = fresh
                yield row
            except Exception as e:
                yield {"device": arn, "error": repr(e)}


class IonQ:
    name = "ionq"

    def __init__(self, devices):
        from flux_quantum.backends.ionq.client import API_URL, Client

        key = os.environ.get("IONQ_API_KEY") or os.environ.get("IONQ_API_TOKEN")
        if not key:
            raise RuntimeError("no IONQ_API_KEY")
        self.client = Client(key, os.environ.get("IONQ_API_URL") or API_URL)
        self.devices = devices

    def sample(self):
        # one call for the whole list when the service gives it, else one per
        # device, which the fake does
        listed = {}
        try:
            got = self.client.get("/backends")
            if isinstance(got, list):
                listed = {b.get("backend"): b for b in got}
        except Exception:
            listed = {}
        for name in self.devices:
            b = listed.get(name)
            try:
                if b is None:
                    b = self.client.get("/backends/{}".format(name))
                yield {
                    "device": name,
                    "status": b.get("status"),
                    "degraded": b.get("degraded"),
                    "average_queue_time": b.get("average_queue_time"),
                    "last_updated": b.get("last_updated"),
                    "has_access": b.get("has_access"),
                    "qubits": b.get("qubits"),
                    "raw": plain(b),
                }
            except Exception as e:
                yield {"device": name, "error": repr(e)}


class IBM:
    name = "ibm"

    def __init__(self, devices):
        from qiskit_ibm_runtime import QiskitRuntimeService

        token = os.environ.get("QISKIT_IBM_TOKEN") or os.environ.get("IBM_CLOUD_TOKEN")
        instance = os.environ.get("QISKIT_IBM_INSTANCE") or os.environ.get(
            "IBM_CLOUD_CRN"
        )
        if not token or not instance:
            for k, v in os.environ.items():
                if k.endswith("_QRMI_IBM_QRS_IAM_APIKEY"):
                    token = v
                    instance = os.environ.get(
                        k[: -len("_QRMI_IBM_QRS_IAM_APIKEY")]
                        + "_QRMI_IBM_QRS_SERVICE_CRN"
                    )
        if not token or not instance:
            raise RuntimeError(
                "no IBM credentials: QISKIT_IBM_TOKEN and QISKIT_IBM_INSTANCE, IBM_CLOUD_TOKEN and IBM_CLOUD_CRN, or the QRMI variables"
            )
        self.service = QiskitRuntimeService(
            channel="ibm_quantum_platform", token=token, instance=instance
        )
        self.devices = devices
        self.changed = Unchanged()

    def sample(self):
        try:
            backends = self.service.backends(simulator=False)
        except Exception as e:
            yield {"device": "*", "error": repr(e)}
            return
        for b in backends:
            if self.devices and b.name not in self.devices:
                continue
            try:
                t0 = now()
                s = b.status()
                t1 = now()
                # calibration and configuration, kept in full when they change
                props = plain(b.properties()) if hasattr(b, "properties") else None
                conf = plain(b.configuration()) if hasattr(b, "configuration") else None
                pd, fresh = self.changed(b.name, props)
                cd, cfresh = self.changed(b.name + ":configuration", conf)
                row = {
                    "device": b.name,
                    "pending_jobs": s.pending_jobs,
                    "operational": s.operational,
                    "status_msg": s.status_msg,
                    "qubits": getattr(b, "num_qubits", None),
                    "raw": {
                        "status": plain(s),
                        "properties_digest": pd,
                        "configuration_digest": cd,
                    },
                    "elapsed_s": {"status": t1 - t0, "properties": now() - t1},
                }
                if fresh is not None:
                    row["properties"] = fresh
                if cfresh is not None:
                    row["configuration"] = cfresh
                yield row
            except Exception as e:
                yield {"device": b.name, "error": repr(e)}


VENDORS = {"braket": Braket, "ionq": IonQ, "ibm": IBM}


class Sampler:
    def __init__(
        self,
        spec,
        vendors,
        out,
        interval,
    ):
        self.spec = spec
        self.out = out
        self.interval = interval
        self.vendors = {}
        for name in vendors:
            try:
                self.vendors[name] = VENDORS[name](spec.get(name) or [])
                listed = spec.get(name) or []
                print(
                    "{}: sampling {}".format(
                        name,
                        (
                            "{} device(s)".format(len(listed))
                            if listed
                            else "every backend the account can see"
                        ),
                    ),
                    flush=True,
                )
            except Exception as e:
                print("{}: not sampled, {}".format(name, e), flush=True)
                self.write(
                    {
                        "kind": "setup",
                        "vendor": name,
                        "error": repr(e),
                        "t": now(),
                        "ts": stamp(),
                    }
                )
        self.tick = 0
        self.stop = False

    def write(self, row):
        with open(self.out, "a") as f:
            f.write(json.dumps(row, default=str) + "\n")

    def one_tick(self, echo=False):
        self.tick += 1
        t = now()
        for name, vendor in self.vendors.items():
            try:
                for r in vendor.sample():
                    r.update(
                        {
                            "kind": "sample",
                            "vendor": name,
                            "tick": self.tick,
                            "t": t,
                            "ts": stamp(t),
                            "t_call": now(),
                        }
                    )
                    self.write(r)
                    if echo:
                        print(json.dumps(r, default=str))
            except Exception as e:
                self.write(
                    {
                        "kind": "sample",
                        "vendor": name,
                        "device": "*",
                        "tick": self.tick,
                        "t": t,
                        "ts": stamp(t),
                        "error": repr(e),
                    }
                )
                if echo:
                    print("{}: {}".format(name, e))

    def run(self):
        def stop(signum, frame):
            self.stop = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        print(
            "sampling every {}s to {}, {} vendor(s)".format(
                self.interval, self.out, len(self.vendors)
            ),
            flush=True,
        )
        while not self.stop:
            started = now()
            self.one_tick()
            if self.tick % 10 == 1:
                print("tick {} at {}".format(self.tick, stamp()), flush=True)
            while not self.stop and now() - started < self.interval:
                time.sleep(1)
        print("stopped after {} ticks".format(self.tick), flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--devices", default=os.path.join(HERE, "..", "devices.json"))
    ap.add_argument("--vendors", default="braket,ionq,ibm")
    ap.add_argument(
        "--out", default=os.path.join(HERE, "..", "results", "queues.jsonl")
    )
    ap.add_argument("--interval", type=float, default=60.0)
    ap.add_argument("--once", action="store_true", help="one tick, printed, then stop")
    args = ap.parse_args(argv)
    with open(args.devices) as f:
        spec = json.load(f)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    s = Sampler(
        spec,
        [v for v in args.vendors.split(",") if v],
        args.out,
        args.interval,
    )
    if not s.vendors:
        sys.exit("no vendor could be set up")
    if args.once:
        s.one_tick(echo=True)
        return
    s.run()


if __name__ == "__main__":
    main()
