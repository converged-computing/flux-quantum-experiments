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
    if arm == "baseline":
        # the job holds its nodes through the vendor wait, so the wait is
        # whatever it ran for beyond the actual work
        wait = (held - work) if held is not None else None
        out["vendor_wait_s"] = wait
        out["release_lat_s"] = 0.0            # nothing to release, the baseline holds its own nodes
        out["idle_node_s"] = wait * size if wait is not None else None
        out["billable_s"] = work
    elif arm in ("coscheduled", "nowarmup"):
        out["vendor_wait_s"] = (prio - submit) if (prio and submit) else None
        out["release_lat_s"] = (alloc - prio) if (alloc and prio) else None
        # the classical holds nodes only for the work, and the scout holds one
        # core, so nothing large sits idle
        out["idle_node_s"] = 0.0
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
    spare = int(os.environ.get("CORES", 128)) - load_cores
    out["slack"] = spare - size
    # What the sweep actually varies. Preemptible classical work is part of a
    # pair's budget by policy, so a pair is never short of room: it just has to
    # take some of its cores from preemption. That quantity is >= 0, unlike
    # slack, which reports a deficit that policy says cannot exist.
    need = size + 1 if arm in ("coscheduled", "nowarmup") else size
    out["preempt_cores"] = max(0, need - spare)
    out["node_seconds"] = held * size if held is not None else None

    # The scout holds a core of its own for the whole vendor wait and for the
    # classical run, and that core is unavailable to anyone else. Counting only
    # the classical allocation understates what coscheduling costs, and at small
    # sizes it reverses the sign of the result.
    if arm in ("coscheduled", "nowarmup"):
        out["scout_node_s"] = (s1 - s0) if (s1 and s0) else None
    else:
        # the baseline has no scout
        out["scout_node_s"] = 0.0
    if out["node_seconds"] is not None and out["scout_node_s"] is not None:
        out["total_node_s"] = out["node_seconds"] + out["scout_node_s"]
    else:
        out["total_node_s"] = None

    # Coscheduling wins once the cores freed during the wait outweigh the one
    # core the scout occupies. The baseline spends (wait + work) * size, coscheduled
    # spends work * size + (wait + work), so the crossover is at
    # size > (wait + work) / wait.
    wait = out.get("vendor_wait_s")
    out["breakeven_size"] = ((wait + work) / wait) if wait else None

    # 1 when the arm never completed inside TIMEOUT. Its timing fields are
    # blank, so it must be kept out of every mean, but it is not missing data:
    # it says this arm could not run under this condition.
    # release_lat_s alone hides the cost of a pair that could not be placed.
    # Measured on hardware: at slack 0 the classical waits 5.002s, one whole
    # preempt_after, and every bit of it lands in vendor_wait_s because the
    # scout is what gets held. Only the sum tells the truth.
    out["time_to_alloc_s"] = (alloc - submit) if (alloc and submit) else None

    out["timedout"] = int(f(row, "timedout") or 0)

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
        # group keys can be ints, e.g. slack, so render them as text
        cells = [str(k) for k in key]
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
        # n is completed trials. Censored rows contribute to no mean, so
        # folding them into n would overstate how much was actually measured.
        nto = sum(1 for r in rs if r.get("timedout"))
        cells.append(f"n={len(rs) - nto}" + (f" +{nto} censored" if nto else ""))
        print("  " + "  ".join(f"{str(c):>14s}" for c in cells))
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
         ["arm", "size"],
         ["idle_node_s", "node_seconds", "scout_node_s", "total_node_s"]),
        ("e4", "does the knee track allocation size",
         ["arm", "size", "preempt_cores"],
         ["vendor_wait_s", "release_lat_s", "time_to_alloc_s", "quantum_usd"]),
    ):
        sub = [r for r in rows if r["exp"] == exp]
        if not sub:
            continue
        print(f"== {exp}: {title} ==")
        summarize(sub, keys, metrics)

    censored = [r for r in rows if r.get("timedout")]
    if censored:
        print("== conditions where an arm could not run ==")
        print("  not missing data. The arm never started inside the timeout.")
        seen = {}
        for r in censored:
            k = (r["exp"], r["arm"], int(r["size"]), r.get("slack"))
            seen[k] = seen.get(k, 0) + 1
        print("  " + "  ".join(f"{h:>12s}" for h in
                               ("exp", "arm", "size", "slack", "n")))
        for k in sorted(seen, key=lambda x: (x[0], x[1], x[2])):
            cells = (k[0], k[1], str(k[2]),
                     "" if k[3] is None else str(k[3]), str(seen[k]))
            print("  " + "  ".join(f"{x:>12s}" for x in cells))
        print()

    e3 = [r for r in rows if r["exp"] == "e3"]
    if e3:
        print("== e3: total core seconds, scout included ==")
        print("  the headline ratio, counting the scout core against the design")
        by = defaultdict(dict)
        for r in e3:
            v = r.get("total_node_s")
            if v is not None:
                by[int(r["size"])].setdefault(r["arm"], []).append(v)
        be = [r["breakeven_size"] for r in e3
              if r["arm"] == "baseline" and r["breakeven_size"]]
        print("  " + "  ".join(f"{h:>14s}" for h in
                               ("size", "baseline", "coscheduled", "ratio")))
        for size in sorted(by):
            arms = by[size]
            if "baseline" not in arms or "coscheduled" not in arms:
                continue
            i = sum(arms["baseline"]) / len(arms["baseline"])
            c = sum(arms["coscheduled"]) / len(arms["coscheduled"])
            cells = (str(size), f"{i:.1f}", f"{c:.1f}",
                     f"{i / c:.2f}x" if c else "-")
            print("  " + "  ".join(f"{x:>14s}" for x in cells))
        if be:
            print(f"\n  break even at size > {sum(be) / len(be):.2f}, "
                  "below that the scout core costs more than the wait saves")
        print()

    if NODE_RATE == 0.0:
        print("note: NODE_RATE is 0, so classical_usd is not priced. Set it to "
              "your core second rate to compare the two sides in money.")


if __name__ == "__main__":
    main(sys.argv[1:] or ["e1.csv"])
