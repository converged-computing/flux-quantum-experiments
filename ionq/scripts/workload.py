#!/usr/bin/env python3
"""The classical half of a pair, instrumented.

Submits circuits to IonQ one after another, as an iterative hybrid workload
does, and prints one JSON line per circuit with the clock on both sides: when
this process submitted it and saw it finish, and the service's submitted_at,
started_at and completed_at.

Under the scout it submits into the session it was handed. As a baseline it
can open a session of its own first, or use none.

    workload.py --iterations 5                 inside QUANTUM_SESSION_ID
    workload.py --iterations 5 --own-session   opens and ends its own
    workload.py --iterations 5 --no-session    plain jobs

The key is IONQ_API_KEY. The scout sets IONQ_API_URL, IONQ_BACKEND and
IONQ_NOISE_MODEL for the job; the baselines read the same variables.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request


def circuit(qubits):
    """A GHZ state on the asked for qubits."""
    gates = [{"gate": "h", "target": 0}]
    gates += [{"gate": "cnot", "control": 0, "target": q} for q in range(1, qubits)]
    return {"qubits": qubits, "gateset": "qis", "circuit": gates}


def call(method, path, body=None):
    url = (os.environ.get("IONQ_API_URL") or "https://api.ionq.co/v0.4").rstrip("/")
    req = urllib.request.Request(
        url + path,
        data=None if body is None else json.dumps(body).encode(),
        method=method,
    )
    req.add_header("Authorization", "apiKey " + os.environ["IONQ_API_KEY"])
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        text = e.read()[:200].decode(errors="replace")
        raise SystemExit("%s %s returned %d: %s" % (method, path, e.code, text))


def say(**row):
    print(json.dumps(row), flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--iterations", type=int, default=1)
    ap.add_argument("--shots", type=int, default=100)
    ap.add_argument("--qubits", type=int, default=2)
    ap.add_argument("--poll", type=float, default=0.5, help="seconds between polls")
    ap.add_argument(
        "--think", type=float, default=0.0, help="classical seconds between circuits"
    )
    ap.add_argument(
        "--own-session", action="store_true", help="baseline: open a session first"
    )
    ap.add_argument("--no-session", action="store_true", help="baseline: plain jobs")
    ap.add_argument("--session-minutes", type=int, default=15)
    args = ap.parse_args()

    if not os.environ.get("IONQ_API_KEY"):
        sys.exit("no IONQ_API_KEY in the environment")
    target = os.environ.get("IONQ_BACKEND", "simulator")
    noise = os.environ.get("IONQ_NOISE_MODEL")

    session, own, open_s = None, False, None
    if args.own_session:
        t0 = time.time()
        made = call(
            "POST",
            "/sessions",
            {
                "backend": target,
                "settings": {"duration_limit_min": args.session_minutes},
            },
        )
        session, own, open_s = made["id"], True, time.time() - t0
    elif not args.no_session:
        handed = os.environ.get("QUANTUM_SESSION_ID")
        if not handed:
            sys.exit("no QUANTUM_SESSION_ID, this job was not started by the scout")
        # a probe hold hands over a job id, which nothing can be submitted into
        session = None if handed.startswith("job:") else handed
    say(
        event="start",
        t=time.time(),
        target=target,
        noise=noise,
        session=session,
        own_session=own,
        session_open_s=open_s,
        iterations=args.iterations,
        shots=args.shots,
    )

    body = {
        "type": "ionq.circuit.v1",
        "name": "flux-quantum-workload",
        "shots": args.shots,
        "backend": target,
        "input": circuit(args.qubits),
    }
    if session:
        body["session_id"] = session
    if noise:
        body["noise"] = {"model": noise}

    failed = 0
    for i in range(args.iterations):
        submitted = time.time()
        job = call("POST", "/jobs", body)
        while True:
            state = call("GET", "/jobs/%s" % job["id"])
            if state.get("status") in ("completed", "failed", "canceled"):
                break
            time.sleep(args.poll)
        done = time.time()
        if state.get("status") != "completed":
            failed += 1
        say(
            event="job",
            i=i,
            job=job["id"],
            status=state.get("status"),
            submitted=submitted,
            done=done,
            submitted_at=state.get("submitted_at"),
            started_at=state.get("started_at"),
            completed_at=state.get("completed_at"),
            execution_duration_ms=state.get("execution_duration_ms"),
            failure=(state.get("failure") or {}).get("error"),
        )
        if args.think:
            time.sleep(args.think)

    if own and session:
        call("POST", "/sessions/%s/end" % session)
    say(event="end", t=time.time(), failed=failed)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
