"""Metrics derived from an IonQ experiment: the CSV of flux timestamps and the
JSON lines the workload printed for each circuit.

Per row, seconds:

    scout_lead_s     scout started to session posted. The time to get the hold:
                     create the session, run the warm-up, see it active.
    handoff_s        session posted to classical allocated. The flux half of
                     the handover.
    startup_s        classical allocated to classical running.
    to_first_s       submit to the first circuit's result. What a user waits
                     for, whichever arm.
    first_wait_s     the first circuit, from its submit to its result, as the
                     classical job saw it.
    circuit_s        median circuit latency over the job, submit to result.
    vendor_exec_ms   median execution_duration_ms the service reported.
    held_s           classical allocated to finished, the cores held.
    session_open_s   the session baseline's own session creation time.

Censored rows, timedout=1, keep their fields blank and are in no mean.
"""

import csv
import datetime
import glob
import json
import os
import statistics


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def iso(x):
    """An ISO 8601 stamp from the service as epoch seconds, or None."""
    if not x:
        return None
    try:
        return datetime.datetime.fromisoformat(
            str(x).replace("Z", "+00:00")
        ).timestamp()
    except ValueError:
        return None


def jobs_for(path, row):
    """The workload's JSON lines for a row, from the directory beside the CSV."""
    d = os.path.splitext(path)[0] + "-jobs"
    name = "%s-%s-%s-%s.jsonl" % (row["exp"], row["arm"], row["trial"], row["batch"])
    out = []
    try:
        with open(os.path.join(d, name)) as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("{"):
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
    except OSError:
        pass
    return out


def derive(row, events):
    out = dict(row)
    out["size"] = int(row["size"])
    out["iters"] = int(row["iters"])
    out["batch"] = int(row["batch"])
    out["timedout"] = int(num(row.get("timedout")) or 0)
    submit, prio = num(row.get("submit")), num(row.get("priority"))
    alloc, start, finish = (
        num(row.get("alloc")),
        num(row.get("start")),
        num(row.get("finish")),
    )
    s_start = num(row.get("scout_start"))

    def gap(a, b):
        return (b - a) if (a is not None and b is not None) else None

    out["scout_lead_s"] = gap(s_start, prio)
    out["handoff_s"] = gap(prio, alloc)
    out["startup_s"] = gap(alloc, start)
    out["held_s"] = gap(alloc, finish)
    out["time_to_alloc_s"] = gap(submit, alloc)

    jobs = [e for e in events if e.get("event") == "job"]
    starts = [e for e in events if e.get("event") == "start"]
    out["circuits"] = len(jobs)
    out["failed"] = sum(1 for e in jobs if e.get("status") != "completed")
    out["session_open_s"] = starts[0].get("session_open_s") if starts else None
    out["session"] = out.get("session") or (
        starts[0].get("session") if starts else None
    )
    waits = [
        e["done"] - e["submitted"] for e in jobs if e.get("done") and e.get("submitted")
    ]
    out["first_wait_s"] = waits[0] if waits else None
    out["circuit_s"] = statistics.median(waits) if waits else None
    execs = [
        e["execution_duration_ms"]
        for e in jobs
        if e.get("execution_duration_ms") is not None
    ]
    out["vendor_exec_ms"] = statistics.median(execs) if execs else None
    first_done = jobs[0].get("done") if jobs else None
    out["to_first_s"] = gap(submit, first_done)
    last_done = jobs[-1].get("done") if jobs else None
    out["to_last_s"] = gap(submit, last_done)
    # the service's own view of the first circuit, queue and execution
    if jobs:
        out["vendor_first_s"] = gap(
            iso(jobs[0].get("submitted_at")), iso(jobs[0].get("completed_at"))
        )
    else:
        out["vendor_first_s"] = None
    return out


def load(paths):
    rows = []
    for p in paths:
        with open(p) as fh:
            for r in csv.DictReader(fh):
                rows.append(derive(r, jobs_for(p, r)))
    return rows


def meta(path):
    """The .meta beside a CSV, as a dict."""
    out = {}
    try:
        with open(os.path.splitext(path)[0] + ".meta") as fh:
            for line in fh:
                parts = line.split(None, 1)
                if len(parts) == 2:
                    out[parts[0]] = parts[1].strip()
    except OSError:
        pass
    return out


def csvs(arg):
    """CSV paths from a path, a directory or a glob."""
    if os.path.isdir(arg):
        return sorted(glob.glob(os.path.join(arg, "*.csv")))
    return sorted(glob.glob(arg)) or [arg]
