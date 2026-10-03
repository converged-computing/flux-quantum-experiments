"""Metrics derived from the raw timestamps in an experiment CSV.

Everything analyze.py prints and plot.py draws comes from here, so the model
can change without another run.

    vendor_wait_s     how long the QPU queue took. Free in every arm, since a
                      vendor bills from the first task, not the session.
    release_lat_s     from having the QPU to the classical being allocated.
    time_to_alloc_s   submit to classical allocated, for the whole pair. One
                      grace period lands in vendor_wait_s or release_lat_s
                      depending on which half waited, and only the sum
                      counts it once.
    idle_node_s       cores held while waiting on the vendor, times seconds.
    node_seconds      the classical allocation, cores times seconds held.
    scout_node_s      the scout's core, for the vendor wait and the run.
    total_node_s      the two together, which is what a site pays.
    billable_s        what a vendor bills, from the first task to release.
    preempt_cores     how many of the pair's cores had to come from
                      preemption. Never negative: preemptible work is part of
                      a pair's budget by policy.
"""

import csv
import os

QUANTUM_RATE = 1.60  # dollars per second of held session, IBM pay as you go
NODE_RATE = 0.0  # dollars per core second, set it for your instance
ARMS = ("baseline", "coscheduled")


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def cores_for(path, default=128):
    """The core count the run was measured on, from the .meta beside the CSV,
    then CORES in the environment, then the default."""
    meta = os.path.splitext(path)[0] + ".meta"
    try:
        with open(meta) as fh:
            for line in fh:
                parts = line.split()
                if parts[:1] == ["cores"] and len(parts) > 1:
                    return int(parts[1])
    except OSError:
        pass
    return int(os.environ.get("CORES", default))


def derive(row, cores):
    arm = row["arm"]
    size = int(row["size"])
    work = float(row["work_s"])
    submit, prio = num(row.get("submit")), num(row.get("priority"))
    alloc, finish = num(row.get("alloc")), num(row.get("finish"))
    s0, s1 = num(row.get("scout_start")), num(row.get("scout_finish"))

    out = dict(row)
    out["size"] = size
    out["depth"] = int(row["depth"])
    out["load_pct"] = int(row["load_pct"])
    out["timedout"] = int(num(row.get("timedout")) or 0)
    held = (finish - alloc) if (finish and alloc) else None

    if arm == "baseline":
        # holds its nodes through the vendor wait, so the wait is whatever it
        # ran for beyond the work
        wait = (held - work) if held is not None else None
        out["vendor_wait_s"] = wait
        out["release_lat_s"] = 0.0
        out["idle_node_s"] = wait * size if wait is not None else None
        out["billable_s"] = work
        out["scout_node_s"] = 0.0
    else:
        out["vendor_wait_s"] = (prio - submit) if (prio and submit) else None
        out["release_lat_s"] = (alloc - prio) if (alloc and prio) else None
        out["idle_node_s"] = 0.0
        out["billable_s"] = (s1 - prio) if (s1 and prio) else None
        out["scout_node_s"] = (s1 - s0) if (s1 and s0) else None
    out["time_to_alloc_s"] = (alloc - submit) if (alloc and submit) else None

    # the recorded core count, since a percentage cannot address every core
    # count on the machine
    recorded = num(row.get("load_cores"))
    load_cores = int(recorded) if recorded else cores * out["load_pct"] // 100
    out["load_cores"] = load_cores
    spare = cores - load_cores
    out["slack"] = spare - size
    need = size + 1 if arm == "coscheduled" else size
    out["preempt_cores"] = max(0, need - spare)

    out["node_seconds"] = held * size if held is not None else None
    if out["node_seconds"] is not None and out["scout_node_s"] is not None:
        out["total_node_s"] = out["node_seconds"] + out["scout_node_s"]
    else:
        out["total_node_s"] = None
    # the baseline spends (wait + work) * size, coscheduled spends
    # work * size + (wait + work), so they cross at size = (wait + work) / wait
    wait = out["vendor_wait_s"]
    out["breakeven_size"] = ((wait + work) / wait) if wait else None

    out["quantum_usd"] = (out["billable_s"] or 0) * QUANTUM_RATE
    out["classical_usd"] = (out["idle_node_s"] or 0) * NODE_RATE
    return out


def load(paths):
    """Every row of every CSV, derived, each against its own core count."""
    rows = []
    for p in paths:
        cores = cores_for(p)
        with open(p) as fh:
            rows += [derive(r, cores) for r in csv.DictReader(fh)]
    return rows
