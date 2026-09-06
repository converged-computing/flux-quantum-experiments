#!/usr/bin/env python3
"""Derive the metrics from a raw experiment CSV.

    ./analyze.py e1.csv e2.csv e3.csv

Everything here comes from the raw timestamps, so the analysis can be changed
without spending another run.

    vendor_wait_s     how long the QPU queue took. Free in every arm.
    release_lat_s     from having the QPU to the classical actually running.
                      This is what coscheduling is for.
    node_seconds      nodes multiplied by the time they were held. The waste in
                      the inline arm is the vendor wait times the allocation.
    billable_s        what a real vendor would bill, which starts when the
                      first task runs and not when the session was opened.
    bg_drain_s        how long a competing stream of small jobs took to clear.
                      An arm that sits on nodes it is not using makes this
                      longer, which is the cost coscheduling avoids.
"""

import csv
import os
import sys
from collections import defaultdict

QUANTUM_RATE = 1.60      # dollars per second of held session, ibm pay as you go
NODE_RATE = 0.0          # dollars per core second, set it for your instance


def f(row, key):
    try:
        return float(row[key])
    except (TypeError, ValueError, KeyError):
        return None


def derive(row):
    size = int(row["size"])
    work = float(row["work_s"])
    submit, prio = f(row, "submit"), f(row, "priority")
    alloc, start, finish = f(row, "alloc"), f(row, "start"), f(row, "finish")
    s0, s1 = f(row, "scout_start"), f(row, "scout_finish")
    arm = row["arm"]

    out = dict(row)
    held = (finish - alloc) if (finish and alloc) else None

    # A vendor bills from when the first task runs, not from when the session
    # was opened, so the queue wait is free in every arm and billable time
    # starts at priority.
    if arm == "inline":
        # the job holds its nodes through the vendor wait, so the wait is
        # whatever it ran for beyond the actual work
        wait = (held - work) if held is not None else None
        out["vendor_wait_s"] = wait
        out["release_lat_s"] = 0.0            # nothing to release, it is inline
        out["idle_node_s"] = wait * size if wait is not None else None
        out["billable_s"] = work
    elif arm in ("coscheduled", "nowarmup"):
        out["vendor_wait_s"] = (prio - submit) if (prio and submit) else None
        out["release_lat_s"] = (alloc - prio) if (alloc and prio) else None
        # the classical holds nodes only for the work, and the scout holds one
        # core, so nothing large sits idle
        out["idle_node_s"] = 0.0
        out["billable_s"] = (s1 - prio) if (s1 and prio) else None
    else:  # sessionfirst
        out["vendor_wait_s"] = (prio - f(row, "t0")) if prio else None
        out["release_lat_s"] = (start - prio) if (start and prio) else None
        out["idle_node_s"] = 0.0
        # the session sits open while the classical queues, and that is billed
        out["billable_s"] = (s1 - prio) if (s1 and prio) else None

    # bg_done used to be a count of finished jobs and is now the timestamp the
    # stream drained, so ignore the old format rather than subtract an epoch
    # from a small integer
    bg_drain = f(row, "bg_done")
    t0 = f(row, "t0")
    if bg_drain is not None and bg_drain < 1e6:
        bg_drain = None
    out["bg_drain_s"] = (bg_drain - t0) if (bg_drain and t0) else None
    recorded = f(row, "load_cores")
    load_cores = int(recorded) if recorded else int(row["load_pct"]) * 128 // 100
    out["slack"] = (int(os.environ.get("CORES", 128)) - load_cores) - size
    out["node_seconds"] = held * size if held is not None else None
    out["quantum_usd"] = (out["billable_s"] or 0) * QUANTUM_RATE
    out["classical_usd"] = (out["idle_node_s"] or 0) * NODE_RATE
    return out


def summarize(rows, group_keys, metrics):
    groups = defaultdict(list)
    for r in rows:
        groups[tuple(r[k] for k in group_keys)].append(r)
    print("  " + "  ".join(f"{k:>14s}" for k in group_keys + metrics) + "       n")
    for key in sorted(groups, key=lambda k: [_num(x) for x in k]):
        rs = groups[key]
        cells = list(key)
        for m in metrics:
            vals = []
            for r in rs:
                v = r.get(m)
                if v is None or v == "":
                    continue
                try:
                    vals.append(float(v))
                except (TypeError, ValueError):
                    pass
            if not vals:
                cells.append("-")
                continue
            n = len(vals)
            mean = sum(vals) / n
            # a mean with no spread is not reportable, so carry the sd and n
            sd = (sum((v - mean) ** 2 for v in vals) / (n - 1)) ** 0.5 if n > 1 else 0.0
            cells.append(f"{mean:.3f}±{sd:.3f}")
        cells.append(f"n={len(rs)}")
        print("  " + "  ".join(f"{c:>14s}" for c in cells))
    print()


def _num(x):
    try:
        return (0, float(x))
    except ValueError:
        return (1, x)


def main(paths):
    rows = []
    for p in paths:
        with open(p) as fh:
            rows += [derive(r) for r in csv.DictReader(fh)]

    for exp, title, keys, metrics in (
        ("e1", "release latency against vendor queue depth",
         ["arm", "depth"], ["vendor_wait_s", "release_lat_s", "billable_s", "quantum_usd"]),
        ("e2", "the arms under classical contention",
         ["arm", "load_pct"],
         ["release_lat_s", "idle_node_s", "bg_drain_s", "quantum_usd"]),
        ("e3", "node seconds wasted against allocation size",
         ["arm", "size"], ["idle_node_s", "node_seconds"]),
        ("e4", "does the knee track allocation size",
         ["arm", "size", "slack"], ["release_lat_s", "quantum_usd"]),
    ):
        sub = [r for r in rows if r["exp"] == exp]
        if not sub:
            continue
        print(f"== {exp}: {title} ==")
        summarize(sub, keys, metrics)

    if NODE_RATE == 0.0:
        print("note: NODE_RATE is 0, so classical_usd is not priced. Set it to "
              "your core second rate to compare the two sides in money.")


if __name__ == "__main__":
    main(sys.argv[1:] or ["e1.csv"])
