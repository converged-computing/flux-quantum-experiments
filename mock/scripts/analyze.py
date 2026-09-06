#!/usr/bin/env python3
"""Tables from the experiment CSVs.

    ./analyze.py results/*.csv

Means with the sample standard deviation and n per cell. A censored trial,
one that never started inside the timeout, is in no mean and is listed on
its own.
"""

import sys
from collections import defaultdict

from metrics import NODE_RATE, load

TABLES = (
    (
        "e1",
        "allocation held, against vendor queue depth",
        ["arm", "depth"],
        ["vendor_wait_s", "release_lat_s", "billable_s", "quantum_usd"],
    ),
    (
        "e2",
        "the arms under classical contention",
        ["arm", "load_pct"],
        ["release_lat_s", "idle_node_s", "quantum_usd"],
    ),
    (
        "e3",
        "core seconds consumed, against allocation size",
        ["arm", "size"],
        ["idle_node_s", "node_seconds", "scout_node_s", "total_node_s"],
    ),
    (
        "e4",
        "does the knee track allocation size",
        ["arm", "size", "preempt_cores"],
        ["vendor_wait_s", "release_lat_s", "time_to_alloc_s", "quantum_usd"],
    ),
    (
        "e5",
        "what contention costs, against how long the other work has left",
        ["arm", "load_secs"],
        ["vendor_wait_s", "release_lat_s", "time_to_alloc_s", "quantum_usd"],
    ),
)


def _num(x):
    try:
        return (0, float(x))
    except (TypeError, ValueError):
        return (1, str(x))


def summarize(rows, group_keys, metrics):
    groups = defaultdict(list)
    for r in rows:
        groups[tuple(r[k] for k in group_keys)].append(r)
    print("  " + "  ".join(f"{k:>14s}" for k in group_keys + metrics) + "       n")
    for key in sorted(groups, key=lambda k: [_num(x) for x in k]):
        rs = groups[key]
        cells = [str(k) for k in key]
        for m in metrics:
            vals = [float(r[m]) for r in rs if r.get(m) not in (None, "")]
            if not vals:
                cells.append("-")
                continue
            n = len(vals)
            mean = sum(vals) / n
            sd = (sum((v - mean) ** 2 for v in vals) / (n - 1)) ** 0.5 if n > 1 else 0.0
            cells.append(f"{mean:.3f}±{sd:.3f}")
        nto = sum(1 for r in rs if r.get("timedout"))
        cells.append(f"n={len(rs) - nto}" + (f" +{nto} censored" if nto else ""))
        print("  " + "  ".join(f"{str(c):>14s}" for c in cells))
    print()


def censored(rows):
    seen = defaultdict(int)
    for r in rows:
        if r.get("timedout"):
            seen[(r["exp"], r["arm"], r["size"], r.get("slack"))] += 1
    if not seen:
        return
    print("== conditions where an arm could not run ==")
    print("  the arm never started inside the timeout")
    print("  " + "  ".join(f"{h:>12s}" for h in ("exp", "arm", "size", "slack", "n")))
    for k in sorted(seen, key=lambda x: (x[0], x[1], x[2])):
        cells = (k[0], k[1], str(k[2]), "" if k[3] is None else str(k[3]), str(seen[k]))
        print("  " + "  ".join(f"{x:>12s}" for x in cells))
    print()


def core_seconds(e3):
    print("== e3: total core seconds, scout included ==")
    by = defaultdict(dict)
    for r in e3:
        v = r.get("total_node_s")
        if v is not None:
            by[r["size"]].setdefault(r["arm"], []).append(v)
    print(
        "  "
        + "  ".join(f"{h:>14s}" for h in ("size", "baseline", "coscheduled", "ratio"))
    )
    for size in sorted(by):
        arms = by[size]
        if "baseline" not in arms or "coscheduled" not in arms:
            continue
        b = sum(arms["baseline"]) / len(arms["baseline"])
        c = sum(arms["coscheduled"]) / len(arms["coscheduled"])
        cells = (str(size), f"{b:.1f}", f"{c:.1f}", f"{b / c:.2f}x" if c else "-")
        print("  " + "  ".join(f"{x:>14s}" for x in cells))
    be = [
        r["breakeven_size"]
        for r in e3
        if r["arm"] == "baseline" and r["breakeven_size"]
    ]
    if be:
        print(f"\n  break even at size > {sum(be) / len(be):.2f}")
    print()


def admission(e6):
    print("== e6: pairs admitted against pairs asked for ==")
    by = defaultdict(list)
    for r in e6:
        by[int(float(r["bg_total"]))].append(int(float(r["bg_done"])))
    print("  " + "  ".join(f"{h:>12s}" for h in ("asked", "admitted", "n")))
    for asked in sorted(by):
        got = by[asked]
        print(
            "  "
            + "  ".join(
                f"{x:>12s}"
                for x in (str(asked), f"{sum(got) / len(got):.1f}", str(len(got)))
            )
        )
    print()


def main(paths):
    rows = load(paths)
    for exp, title, keys, metrics in TABLES:
        sub = [r for r in rows if r["exp"] == exp]
        if sub:
            print(f"== {exp}: {title} ==")
            summarize(sub, keys, metrics)
    censored(rows)
    e3 = [r for r in rows if r["exp"] == "e3"]
    if e3:
        core_seconds(e3)
    e6 = [r for r in rows if r["exp"] == "e6"]
    if e6:
        admission(e6)
    if NODE_RATE == 0.0:
        print("note: NODE_RATE is 0, so classical_usd is not priced")


if __name__ == "__main__":
    main(sys.argv[1:] or ["e1.csv"])
