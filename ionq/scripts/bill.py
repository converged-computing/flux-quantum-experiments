#!/usr/bin/env python3
"""What IonQ charged, job by job, read back from the service.

    flux python scripts/bill.py results-forte            every circuit the workload recorded
    flux python scripts/bill.py results-forte --recent 40 the organisation's last 40 jobs too

The workload records the id of every circuit it submitted, beside each
experiment's CSV. For each one this asks the service for the job and its
cost, and prints the backend it ran on, the session it ran in, its status
and shots, and the estimated and billed cost. The totals are what the run
cost. --recent lists the organisation's latest jobs as well, which is where
the scouts' warm-up jobs are, since the workload does not see those.

The key is IONQ_API_KEY and the endpoint IONQ_API_URL, as everywhere.
"""

import argparse
import glob
import json
import os
import sys

from flux_quantum.backends.ionq import IonQBackend


def circuit_ids(dirs):
    """(experiment, arm, trial, job id) for every circuit recorded."""
    out = []
    for d in dirs:
        for path in sorted(glob.glob(os.path.join(d, "*-jobs", "*.jsonl"))):
            tag = os.path.basename(path).rsplit(".", 1)[0]
            with open(path) as fh:
                for line in fh:
                    if not line.startswith("{"):
                        continue
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if e.get("event") == "job" and e.get("job"):
                        out.append((tag, e["job"]))
    return out


def money(x):
    if isinstance(x, dict):
        x = x.get("value")
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def session_of(job):
    """The session a job ran in, whichever key the service uses for it."""
    for key in ("session_id", "session"):
        v = job.get(key)
        if isinstance(v, dict):
            v = v.get("id")
        if v:
            return str(v)
    return ""


RAW = {"shown": False}


def describe(client, jid, raw=False):
    job = client.get("/jobs/%s" % jid)
    try:
        cost = client.get("/jobs/%s/cost" % jid)
    except Exception as e:  # the cost route is newer than the job route
        cost = {"error": str(e)}
    if raw and not RAW["shown"]:
        RAW["shown"] = True
        print("== raw answers for %s ==" % jid)
        print(json.dumps(job, indent=1))
        print(json.dumps(cost, indent=1))
        print()
    failure = job.get("failure") or {}
    if isinstance(failure, dict):
        failure = failure.get("error") or failure.get("code") or ""
    # the service answers the cost route with only dry_run for a job that
    # cost nothing, the simulator's, so no cost keys means free, not unknown
    free = "cost" not in cost and "estimated_cost" not in cost and not cost.get("error")
    return {
        "id": jid,
        "free": free,
        "backend": job.get("backend"),
        "session": session_of(job),
        "status": job.get("status"),
        "shots": job.get("shots") if job.get("shots") is not None else (job.get("input") or {}).get("shots"),
        "name": job.get("name") or "",
        "dry_run": cost.get("dry_run"),
        "estimated": 0.0 if free else money(cost.get("estimated_cost")),
        "billed": 0.0 if free else money(cost.get("cost")),
        "error": cost.get("error"),
        "failure": str(failure)[:80],
    }


def table(rows, title):
    print("== %s ==" % title)
    hdr = ("record", "backend", "session", "status", "shots", "estimated", "billed")
    print("  " + "  ".join("%-*s" % (w, h) for h, w in zip(hdr, (26, 16, 10, 10, 6, 9, 9))))
    est = billed = 0.0
    unknown = 0
    for tag, r in rows:
        cells = (
            tag[:26],
            str(r["backend"])[:16],
            r["session"][:8],
            str(r["status"]),
            str(r["shots"]),
            "free" if r.get("free") else "-" if r["estimated"] is None else "%.2f" % r["estimated"],
            "free" if r.get("free") else "-" if r["billed"] is None else "%.2f" % r["billed"],
        )
        print("  " + "  ".join("%-*s" % (w, c) for c, w in zip(cells, (26, 16, 10, 10, 6, 9, 9))))
        if r["status"] == "failed" and r["failure"]:
            print("      failed: %s" % r["failure"])
        if r["billed"] is None:
            unknown += 1
            if r["error"] and unknown == 1:
                print("      no cost record: %s" % r["error"])
        else:
            billed += r["billed"]
        est += r["estimated"] or 0.0
    print(
        "  %d jobs, estimated %.2f, billed %.2f%s"
        % (len(rows), est, billed, ", %d with no cost record" % unknown if unknown else "")
    )
    print()
    return billed


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("dirs", nargs="+", help="results directories")
    ap.add_argument("--recent", type=int, default=0, help="also list the organisation's latest N jobs")
    ap.add_argument("--raw", action="store_true", help="print the service's raw job and cost answers for the first job")
    args = ap.parse_args(argv)
    client = IonQBackend().client

    ids = circuit_ids(args.dirs)
    rows = []
    for tag, jid in ids:
        try:
            rows.append((tag, describe(client, jid, raw=args.raw)))
        except Exception as e:
            print("  %s %s: %s" % (tag, jid, e), file=sys.stderr)
    total = table(rows, "circuits the workload recorded") if rows else 0.0
    if not rows:
        print("no circuit records under %s" % ", ".join(args.dirs))

    # anything that ran somewhere other than the target, or was a dry run, is a flag
    off = [(t, r) for t, r in rows if r["dry_run"] or str(r["backend"]).startswith("simulator")]
    if off:
        print("NOTE %d of these ran on the simulator or as a dry run" % len(off))
    no_session = [(t, r) for t, r in rows if "coscheduled" in t and not r["session"]]
    if no_session:
        print("NOTE %d coscheduled circuits ran outside a session" % len(no_session))

    if args.recent:
        try:
            listed = client.get("/jobs?limit=%d" % args.recent)
        except Exception as e:
            print("could not list recent jobs: %s" % e)
            return
        jobs = listed.get("jobs") if isinstance(listed, dict) else listed
        recent = []
        for j in jobs or []:
            try:
                recent.append((j.get("name") or j.get("id", "")[:8], describe(client, j["id"])))
            except Exception as e:
                print("  %s: %s" % (j.get("id"), e), file=sys.stderr)
        warm = [(t, r) for t, r in recent if r["name"] == "flux-quantum-warmup"]
        table(recent, "the organisation's latest %d jobs" % len(recent))
        if warm:
            print(
                "  of which %d warm-up jobs, billed %.2f"
                % (len(warm), sum(r["billed"] or 0.0 for _, r in warm))
            )
    print("circuits billed %.2f" % total)


if __name__ == "__main__":
    main(sys.argv[1:])
