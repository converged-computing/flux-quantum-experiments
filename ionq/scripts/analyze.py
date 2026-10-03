#!/usr/bin/env python3
"""Tables from the IonQ experiment CSVs.

    ./analyze.py results/*.csv

Means with the sample standard deviation and n per cell. Censored trials are
in no mean and are counted separately.
"""

import sys
from collections import defaultdict

from metrics import load, meta

TABLES = (
    (
        "e1",
        "overhead of the pair, one at a time",
        ["arm"],
        [
            "to_first_s",
            "scout_lead_s",
            "handoff_s",
            "startup_s",
            "first_wait_s",
            "vendor_first_s",
            "held_s",
        ],
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
        rs = [r for r in groups[key]]
        cells = [str(k) for k in key]
        live = [r for r in rs if not r["timedout"]]
        for m in metrics:
            vals = [float(r[m]) for r in live if r.get(m) not in (None, "")]
            if not vals:
                cells.append("-")
                continue
            n = len(vals)
            mean = sum(vals) / n
            sd = (sum((v - mean) ** 2 for v in vals) / (n - 1)) ** 0.5 if n > 1 else 0.0
            cells.append(f"{mean:.3f}±{sd:.3f}")
        nto = len(rs) - len(live)
        cells.append(f"n={len(live)}" + (f" +{nto} censored" if nto else ""))
        print("  " + "  ".join(f"{str(c):>14s}" for c in cells))
    print()


def main(paths):
    rows = load(paths)
    for p in paths:
        m = meta(p)
        if m:
            print(
                "%s: target=%s dry_run=%s hold=%s size=%s iters=%s"
                % (
                    p,
                    m.get("target"),
                    m.get("dry_run"),
                    m.get("hold"),
                    m.get("size"),
                    m.get("iters"),
                )
            )
    print()
    for exp, title, keys, metrics in TABLES:
        sub = [r for r in rows if r["exp"] == exp]
        if sub:
            print(f"== {exp}: {title} ==")
            summarize(sub, keys, metrics)
    failed = sum(r["failed"] for r in rows)
    if failed:
        print(f"note: {failed} circuits did not complete, see the -jobs directories")


if __name__ == "__main__":
    main(sys.argv[1:] or ["results/e1.csv"])
