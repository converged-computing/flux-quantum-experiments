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
    vendor_queue_s   median per circuit of the service's submitted_at to
                     started_at, the device's queue as the circuit saw it.

Core seconds, what the classical side cost:

    held_core_s      size × held_s, the classical job's cores for its life.
    queue_core_s     size × the sum of one task's circuit queue waits, the
                     cores held while a circuit sat in the device's queue.
    scout_core_s     the scout's one core for its life, pair only.
    pair_core_s      held_core_s plus scout_core_s, what the pair cost.

Censored rows, timedout=1, keep their fields blank and are in no mean.
"""

import csv
import datetime
import glob
import json
import os
import statistics

SCOUT_CORES = 1  # the scout is submitted -n1


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


def read_events(path):
    out = []
    try:
        with open(path) as fh:
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


def jobs_for(path, row):
    """The workload's JSON lines for a row, from the directory beside the CSV.

    The name carries the circuit count. Older runs named the file without
    it, and the e3 sweep overwrote it at every count, so a file named the
    old way is only believed when the workload says it ran this row's count."""
    d = os.path.splitext(path)[0] + "-jobs"
    stem = "%s-%s-%s-%s" % (row["exp"], row["arm"], row["trial"], row["batch"])
    name = "%s-i%s" % (stem, row["iters"])
    if row.get("queue"):
        name += "-q%s" % row["queue"]
    events = read_events(os.path.join(d, name + ".jsonl"))
    if events:
        return events
    events = read_events(os.path.join(d, stem + ".jsonl"))
    starts = [e for e in events if e.get("event") == "start"]
    if starts and str(starts[0].get("iterations")) == str(row["iters"]):
        return events
    return []


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

    s_finish = num(row.get("scout_finish"))
    out["queue"] = row.get("queue") or ""

    out["scout_lead_s"] = gap(s_start, prio)
    out["handoff_s"] = gap(prio, alloc)
    out["startup_s"] = gap(alloc, start)
    out["held_s"] = gap(alloc, finish)
    out["time_to_alloc_s"] = gap(submit, alloc)
    out["held_core_s"] = (
        out["size"] * out["held_s"] if out["held_s"] is not None else None
    )
    scout_life = gap(s_start, s_finish)
    out["scout_core_s"] = SCOUT_CORES * scout_life if scout_life is not None else None
    out["pair_core_s"] = (
        out["held_core_s"] + (out["scout_core_s"] or 0.0)
        if out["held_core_s"] is not None
        else None
    )

    jobs = [e for e in events if e.get("event") == "job"]
    starts = [e for e in events if e.get("event") == "start"]
    # every task of the job runs the workload, so the records are per task
    # and in lockstep. Sums over circuits are per task, not over the job
    tasks = max(1, len(starts))
    out["tasks"] = tasks
    out["circuits"] = len(jobs) // tasks
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
    # the device's queue per circuit, as the service reported it
    queued = [
        gap(iso(e.get("submitted_at")), iso(e.get("started_at")))
        for e in jobs
        if iso(e.get("submitted_at")) is not None and iso(e.get("started_at")) is not None
    ]
    out["vendor_queue_s"] = statistics.median(queued) if queued else None
    out["queue_core_s"] = out["size"] * sum(queued) / tasks if queued else None
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
