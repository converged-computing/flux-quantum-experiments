#!/usr/bin/env python3
"""One trial of the coscheduling experiment on IonQ Forte.

What does coscheduling buy. Two things are measured, for the same work done
the same way with and without it:

    how long classical resources sit held while the device is waited for
    how quickly the classical half's requests are served once it has the
    device, and whether they were served inside the hold at all

Two arms. In both the classical submits K one qubit tasks back to back and
waits for all of them.

    baseline    no hold. The classical starts, submits to the device's public
                queue and waits. Its cores are held from the first submit to
                the last result.
    coschedule  the hold is opened first, with the backend's own calls, and
                the cores are free until it is ready. Then the classical
                submits the same K tasks into the hold. Its cores are held
                from ready to the last result.

Contention is N filler tasks put in the public queue before the trial,
without any hold, so the device has work ahead of us either way. They are
cancelled afterwards, so queued shots are not paid for.

Two paths to the same machine: Forte-1 through Braket, or through IonQ's
API. The hold is Braket's hybrid job token or IonQ's session, or a probe on
either, which holds nothing a task can be submitted into. IonQ sessions are
refused unless asked for, since one that runs a job bills an hour of Forte.

    trial.py run --path braket --arm baseline                  # no hold, K tasks
    trial.py run --path braket --arm coschedule --fillers 3    # the hold, behind a queue
    trial.py run --path ionq --arm coschedule --hold probe
    trial.py estimate --path ionq --arm coschedule --fillers 2 # cost, nothing created

Inside a flux job the same script is the classical half: classical reads
what the scout handed over, fill and drain make and cancel the fillers
around the submit, and annotate adds the job's own timings. See
flux-run.sh.

Every trial appends one JSON line to runs.jsonl, and report.py reads them.
"""

import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths as P  # noqa: E402

RECORD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runs.jsonl")


def say(msg):
    print(msg, flush=True)


def short(task_id):
    return str(task_id).split("/")[-1][:12]


# ---------------------------------------------------------------- tasks --
def follow(
    path, ids, timeout, interval=5.0, clock=time.time, sleep=time.sleep, quiet=False
):
    """Poll the tasks until every one is terminal or the timeout passes.

    Keeps the first queue sighting, since the queue fields are gone once a
    task runs, and a timeline of every change. When the service gives no
    start time the first poll that saw the task running stands in for it.
    """
    views = {i: {"id": i, "timeline": []} for i in ids}
    deadline = clock() + timeout
    while True:
        now = clock()
        for i in ids:
            v = views[i]
            if v.get("terminal"):
                continue
            seen = path.describe(i)
            for k in ("created", "started", "ended", "hold_id", "in_hold", "cost"):
                if seen.get(k) not in (None, False) and v.get(k) in (None, False):
                    v[k] = seen[k]
            if seen.get("queue") and not v.get("queue"):
                v["queue"] = seen["queue"]
                v["position"] = seen.get("position")
                v["priority"] = seen.get("priority")
            if seen.get("position") is not None:
                v["position_last"] = seen["position"]
            v["status"] = seen["status"]
            v["terminal"] = bool(seen["terminal"])
            if (
                v.get("started") is None
                and "started_seen" not in v
                and str(seen["status"]).lower() in ("running", "started")
            ):
                v["started_seen"] = now
            change = [seen["status"], seen.get("queue"), seen.get("position")]
            if not v["timeline"] or v["timeline"][-1][1:] != change:
                v["timeline"].append([now] + change)
                if not quiet:
                    say(
                        "  {:<12} {:<10} {}".format(
                            short(i),
                            seen["status"],
                            (
                                "{} queue, position {}".format(
                                    seen.get("queue"), seen.get("position")
                                )
                                if seen.get("queue")
                                else ""
                            ),
                        )
                    )
        if all(views[i].get("terminal") for i in ids):
            return views
        if clock() + interval > deadline:
            return views
        sleep(interval)


def task_record(view, submitted_at, submit_latency):
    created = view.get("created") or submitted_at
    started = view.get("started") or view.get("started_seen")
    ended = view.get("ended")
    return {
        "id": view["id"],
        "status": view.get("status"),
        "in_hold": bool(view.get("in_hold")),
        "hold_id": view.get("hold_id"),
        "queue": view.get("queue"),
        "position": view.get("position"),
        "position_last": view.get("position_last"),
        "submitted_at": submitted_at,
        "submit_latency_s": submit_latency,
        "created": created,
        "started": started,
        "started_estimated": view.get("started") is None and started is not None,
        "ended": ended,
        "wait_s": (started - created) if (started and created) else None,
        "turnaround_s": (ended - created) if (ended and created) else None,
        "cost_usd": view.get("cost"),
        "timeline": view.get("timeline", []),
    }


def run_tasks(path, count, shots, credential, timeout, label="task", quiet=False):
    """Submit count tasks back to back, with the credential when there is
    one, and follow them to the end. Returns the records and the phase."""
    started = time.time()
    ids, meta = [], {}
    for n in range(count):
        t0 = time.time()
        i = path.submit(shots, credential, "{}-{}".format(label, n))
        meta[i] = (t0, time.time() - t0)
        ids.append(i)
        say(
            "  submitted {} {}{}".format(
                label, short(i), " into the hold" if credential else ""
            )
        )
    submitted = time.time()
    views = follow(path, ids, timeout, quiet=quiet)
    ended = time.time()
    records = [task_record(views[i], *meta[i]) for i in ids]
    return records, {
        "started_at": started,
        "all_submitted_at": submitted,
        "submit_s": submitted - started,
        "ended_at": ended,
        "makespan_s": ended - started,
        "all_done": all(
            r["status"] and str(r["status"]).lower() == "completed" for r in records
        ),
    }


# ------------------------------------------------------------- fillers --
def fill(path, count, shots):
    ids = []
    for n in range(count):
        ids.append(path.submit(shots, None, "filler-{}".format(n)))
        say("  filler {} {}".format(n, short(ids[-1])))
    return ids


def drain(path, ids):
    """Cancel what is still waiting and read back what each filler did."""
    out = []
    for i in ids:
        cancelled = path.cancel(i)
        try:
            d = path.describe(i)
        except Exception as e:
            d = {"status": "unknown: {}".format(e)}
        out.append(
            {
                "id": i,
                "cancelled": cancelled,
                "status": d.get("status"),
                "created": d.get("created"),
                "ended": d.get("ended"),
                "cost_usd": d.get("cost"),
            }
        )
    return out


# ---------------------------------------------------------------- cost --
def estimate(path, arm, tasks, fillers, shots, filler_shots, max_seconds):
    """What the trial will cost, before anything is created."""
    per = path.task_cost(shots)
    rows = [("{} tasks at {} shots".format(tasks, shots), tasks * per)]
    if fillers:
        rows.append(
            (
                "{} fillers at {} shots".format(fillers, filler_shots),
                fillers * path.task_cost(filler_shots),
            )
        )
    if arm == "coschedule":
        if path.hold_mode == "session" and path.name == "braket":
            rows.append(
                (
                    "hybrid job instance, up to {}s".format(max_seconds),
                    path.hold_cost(max_seconds),
                )
            )
        elif path.hold_mode == "session":
            rows.append(
                ("IonQ session: one hour of Forte, rate not known here", float("nan"))
            )
        else:
            # the probe is one small task on either path
            probe_shots = int(
                os.environ.get(
                    (
                        "FLUX_QUANTUM_IONQ_SHOTS"
                        if path.name == "ionq"
                        else "FLUX_QUANTUM_BRAKET_SHOTS"
                    ),
                    1 if path.name == "braket" else 100,
                )
            )
            rows.append(
                (
                    "probe task at {} shots".format(probe_shots),
                    path.task_cost(probe_shots),
                )
            )
    total = sum(c for _, c in rows if c == c)  # nan is left out of the sum
    return rows, total


def print_estimate(rows, total):
    say("estimated cost:")
    for what, c in rows:
        say("  {:<48} {}".format(what, "unknown" if c != c else "${:.2f}".format(c)))
    say(
        "  {:<48} ${:.2f}{}".format(
            "total", total, " plus unknown" if any(c != c for _, c in rows) else ""
        )
    )


# -------------------------------------------------------------- record --
def write(record, where):
    with open(where, "a") as f:
        f.write(json.dumps(record) + "\n")
    say("recorded to {}".format(where))


def base_record(args, path):
    return {
        "schema": 1,
        "started": P.now_iso(),
        "cell": {
            "path": path.name,
            "arm": args.arm,
            "hold": path.hold_mode if args.arm == "coschedule" else None,
            "fillers": args.fillers,
        },
        "device": path.device,
        "tasks_requested": args.tasks,
        "shots": args.shots,
        "filler_shots": args.filler_shots,
        "mode": "standalone",
        "notes": [],
    }


def finish_record(rec, tasks, phase, hold, fillers, path, cost_rows):
    held_from = (
        hold["ready_at"]
        if hold and hold.get("ready_at") is not None
        else phase["started_at"]
    )
    rec["tasks"] = tasks
    rec["phase"] = phase
    rec["hold"] = hold
    rec["fillers_record"] = fillers
    rec["classical"] = {
        "held_from": held_from,
        "held_to": phase["ended_at"],
        "cores_held_s": phase["ended_at"] - held_from,
        "device_wait_s": hold["wait_s"] if hold else None,
    }
    served = [t for t in tasks if t["in_hold"]]
    waits = [t["wait_s"] for t in tasks if t["wait_s"] is not None]
    turns = [t["turnaround_s"] for t in tasks if t["turnaround_s"] is not None]
    rec["summary"] = {
        "tasks_done": sum(1 for t in tasks if str(t["status"]).lower() == "completed"),
        "tasks_in_hold": len(served),
        "mean_wait_s": sum(waits) / len(waits) if waits else None,
        "mean_turnaround_s": sum(turns) / len(turns) if turns else None,
        "makespan_s": phase["makespan_s"],
        "submit_s": phase["submit_s"],
        "cores_held_s": rec["classical"]["cores_held_s"],
        "device_wait_s": rec["classical"]["device_wait_s"],
    }
    charged = [t["cost_usd"] for t in tasks if t.get("cost_usd") is not None]
    charged += [f["cost_usd"] for f in fillers if f.get("cost_usd") is not None]
    hold_s = (hold["ready_at"] or 0) - (hold["opened_at"] or 0) if hold else 0
    hold_s += rec["classical"]["cores_held_s"] if hold else 0
    rec["cost"] = {
        "estimated_usd": sum(c for _, c in cost_rows if c == c),
        "charged_usd": sum(charged) if charged else None,
        "hold_usd": path.hold_cost(hold_s) if hold else 0.0,
    }
    rec["ended"] = P.now_iso()
    return rec


# ------------------------------------------------------------ commands --
def common_path(args):
    return P.make_path(
        args.path,
        device=args.device,
        hold=args.hold,
        allow_ionq_session=getattr(args, "allow_ionq_session", False),
    )


def confirm(args, rows, total):
    print_estimate(rows, total)
    if total > args.max_usd:
        sys.exit(
            "that is over --max-usd {:.2f}. Raise it if the spend is intended".format(
                args.max_usd
            )
        )
    if not args.yes:
        ans = input("continue? [y/N] ")
        if ans.strip().lower() not in ("y", "yes"):
            sys.exit("not run")


def cmd_estimate(args):
    path = common_path(args)
    rows, total = estimate(
        path,
        args.arm,
        args.tasks,
        args.fillers,
        args.shots,
        args.filler_shots,
        args.max_seconds,
    )
    print_estimate(rows, total)


def cmd_run(args):
    path = common_path(args)
    rows, total = estimate(
        path,
        args.arm,
        args.tasks,
        args.fillers,
        args.shots,
        args.filler_shots,
        args.max_seconds,
    )
    confirm(args, rows, total)

    rec = base_record(args, path)
    say("== device before ==")
    rec["device_before"] = path.device_state()
    say("  {}".format(json.dumps(rec["device_before"])))

    fillers, hold, hold_rec, credential = [], None, None, None
    tasks, phase = [], None
    try:
        if args.fillers:
            say("== {} fillers into the public queue ==".format(args.fillers))
            fillers = fill(path, args.fillers, args.filler_shots)
            rec["fillers_submitted_at"] = time.time()
            time.sleep(args.settle)

        if args.arm == "coschedule":
            say("== opening the {} hold, cores free ==".format(path.hold_mode))
            hold = path.hold(args.max_seconds, args.max_seconds)
            hold.open()
            say("  opened {}".format(hold.opened))
            credential = hold.wait()
            say("  ready after {:.0f}s: {}".format(hold.wait_s, hold.why))
            if credential is None:
                rec["notes"].append(
                    "a probe hold hands over nothing to submit into, so the "
                    "tasks went to the public queue"
                )
            hold_rec = hold.record()

        say(
            "== classical: {} tasks at {} shots, cores held from now ==".format(
                args.tasks, args.shots
            )
        )
        tasks, phase = run_tasks(
            path, args.tasks, args.shots, credential, args.max_seconds
        )
        say(
            "  done in {:.0f}s, {} of {} completed".format(
                phase["makespan_s"],
                sum(1 for t in tasks if str(t["status"]).lower() == "completed"),
                len(tasks),
            )
        )
    finally:
        if hold is not None:
            say("== closing the hold ==")
            try:
                hold.close()
            except Exception as e:
                say("  could not close the hold: {}".format(e))
            if hold_rec is None:
                hold_rec = hold.record()
        if fillers:
            say("== draining {} fillers ==".format(len(fillers)))
            fillers = drain(path, fillers)
        if phase is None:
            # the tasks never ran, so the record is what happened up to there
            phase = {
                "started_at": time.time(),
                "all_submitted_at": None,
                "submit_s": None,
                "ended_at": time.time(),
                "makespan_s": 0.0,
                "all_done": False,
            }
        finish_record(rec, tasks, phase, hold_rec, fillers, path, rows)
        write(rec, args.record)
    say("== summary ==")
    for k, v in rec["summary"].items():
        say(
            "  {:<18} {}".format(
                k, v if not isinstance(v, float) else "{:.1f}".format(v)
            )
        )


def cmd_classical(args):
    """The classical half inside a flux job. The hold, if any, is the
    scout's, and what it handed over is in the environment."""
    path = common_path(args)
    rec = base_record(args, path)
    rec["mode"] = "flux"
    rec["flux_job"] = os.environ.get("FLUX_JOB_ID")
    rec["cell"]["fillers"] = 0
    credential = None
    if args.arm == "coschedule":
        credential = path.credential_from_environment()
        rec["hold_credential"] = "present" if credential else "none"
        if credential is None:
            rec["notes"].append(
                "no credential in the environment, the tasks went to the public queue"
            )
    fillers = []
    if args.fillers_file and os.path.exists(args.fillers_file):
        with open(args.fillers_file) as f:
            fillers = json.load(f)
        rec["cell"]["fillers"] = len(fillers)
    rec["device_before"] = path.device_state()
    say(
        "classical: {} tasks at {} shots{}".format(
            args.tasks, args.shots, " into the hold" if credential else ""
        )
    )
    tasks, phase = run_tasks(path, args.tasks, args.shots, credential, args.max_seconds)
    if fillers and args.drain:
        fillers = drain(path, fillers)
    else:
        fillers = [{"id": i} for i in fillers]
    rows, _ = estimate(
        path,
        args.arm,
        args.tasks,
        len(fillers),
        args.shots,
        args.filler_shots,
        args.max_seconds,
    )
    finish_record(rec, tasks, phase, None, fillers, path, rows)
    write(rec, args.record)
    say("done in {:.0f}s".format(phase["makespan_s"]))


def cmd_fill(args):
    path = common_path(args)
    ids = fill(path, args.fillers, args.filler_shots)
    with open(args.fillers_file, "w") as f:
        json.dump(ids, f)
    say("{} fillers written to {}".format(len(ids), args.fillers_file))


def cmd_drain(args):
    path = common_path(args)
    with open(args.fillers_file) as f:
        ids = json.load(f)
    for row in drain(path, ids):
        say(
            "  {} {} {}".format(
                short(row["id"]), row["status"], "cancelled" if row["cancelled"] else ""
            )
        )


def eventlog(jobid):
    out = subprocess.run(
        ["flux", "job", "eventlog", str(jobid)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    events = {}
    for line in out.splitlines():
        parts = line.split(" ", 2)
        if len(parts) >= 2:
            events.setdefault(parts[1], float(parts[0]))
            if parts[1] == "memo" and len(parts) > 2 and "quantum_session" in parts[2]:
                events["memo_session"] = float(parts[0])
    return events


def cmd_annotate(args):
    """Add the flux timings to the record of a classical job: when its
    cores were allocated and freed, and when the scout started and handed
    the session over. Those are the measurement in flux mode."""
    rows = [json.loads(l) for l in open(args.record)]
    hit = None
    for r in rows:
        if r.get("flux_job") in (args.job, str(args.job)):
            hit = r
    if hit is None:
        sys.exit("no record with flux_job {} in {}".format(args.job, args.record))
    ev = eventlog(args.job)
    flux = {
        "classical": ev,
        "cores_held_s": (
            (ev.get("finish") or ev.get("clean") or 0) - (ev.get("alloc") or 0)
            if ev.get("alloc")
            else None
        ),
        "submitted_to_alloc_s": (
            (ev.get("alloc") - ev.get("submit"))
            if ev.get("alloc") and ev.get("submit")
            else None
        ),
    }
    if args.scout:
        sev = eventlog(args.scout)
        flux["scout"] = sev
        if sev.get("start") and ev.get("memo_session"):
            flux["device_wait_s"] = ev["memo_session"] - sev["start"]
    hit["flux"] = flux
    hit["summary"]["cores_held_s"] = flux["cores_held_s"]
    if flux.get("device_wait_s") is not None:
        hit["summary"]["device_wait_s"] = flux["device_wait_s"]
    with open(args.record, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    say(
        "annotated job {}: {}".format(
            args.job,
            json.dumps(
                {k: v for k, v in flux.items() if k not in ("classical", "scout")}
            ),
        )
    )


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, arm=True):
        p.add_argument("--path", choices=("braket", "ionq"), required=True)
        p.add_argument(
            "--device",
            help="override the Forte-1 ARN or backend name, e.g. the SV1 simulator or the fake IonQ service",
        )
        p.add_argument(
            "--hold",
            choices=("session", "probe"),
            help="session (Braket hybrid job, IonQ session) or probe. Default session on Braket, probe on IonQ",
        )
        p.add_argument(
            "--allow-ionq-session",
            action="store_true",
            help="an IonQ session bills an hour of Forte once it runs a job",
        )
        if arm:
            p.add_argument("--arm", choices=("baseline", "coschedule"), required=True)
        p.add_argument(
            "--tasks", type=int, default=3, help="tasks the classical submits, 3"
        )
        p.add_argument("--shots", type=int, default=100)
        p.add_argument(
            "--fillers",
            type=int,
            default=0,
            help="tasks put in the public queue first, 0",
        )
        p.add_argument("--filler-shots", type=int, default=100)
        p.add_argument(
            "--max-seconds",
            type=int,
            default=1800,
            help="the hold's limit and how long to wait for tasks, 1800",
        )
        p.add_argument("--record", default=RECORD)

    p = sub.add_parser("run", help="one trial, standalone")
    common(p)
    p.add_argument(
        "--settle",
        type=float,
        default=10.0,
        help="seconds after the fillers before the trial starts, 10",
    )
    p.add_argument(
        "--max-usd",
        type=float,
        default=40.0,
        help="refuse a trial estimated over this, 40",
    )
    p.add_argument("--yes", action="store_true", help="do not ask")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("estimate", help="the cost of a trial, nothing created")
    common(p)
    p.set_defaults(fn=cmd_estimate)

    p = sub.add_parser("classical", help="the classical half, inside a flux job")
    common(p)
    p.add_argument("--fillers-file", help="the fillers the driver submitted, from fill")
    p.add_argument("--drain", action="store_true", help="cancel the fillers when done")
    p.set_defaults(fn=cmd_classical)

    p = sub.add_parser("fill", help="submit fillers and write their ids")
    common(p, arm=False)
    p.add_argument("--fillers-file", required=True)
    p.set_defaults(fn=cmd_fill)

    p = sub.add_parser("drain", help="cancel the fillers in a file")
    common(p, arm=False)
    p.add_argument("--fillers-file", required=True)
    p.set_defaults(fn=cmd_drain)

    p = sub.add_parser("annotate", help="add a flux job's timings to its record")
    p.add_argument("--job", required=True, help="the classical job id")
    p.add_argument("--scout", help="the scout job id, for the device wait")
    p.add_argument("--record", default=RECORD)
    p.set_defaults(fn=cmd_annotate)

    args = ap.parse_args(argv)
    if getattr(args, "arm", None) == "baseline":
        args.hold = None
    args.fn(args)


if __name__ == "__main__":
    main()
