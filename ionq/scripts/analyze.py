#!/usr/bin/env python3
"""Tables from the IonQ experiment CSVs.

    ./analyze.py results/*.csv

Means with the sample standard deviation and n per cell. Censored trials are
in no mean and are counted separately.
"""

import sys
from collections import defaultdict

from metrics import load, meta, num

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
            "held_core_s",
        ],
    ),
    (
        "e2",
        "pairs at once, per pair",
        ["batch"],
        ["time_to_alloc_s", "scout_lead_s", "handoff_s", "to_first_s", "held_s"],
    ),
    (
        "e3",
        "an iterative workload, per circuit",
        ["arm", "iters"],
        ["to_first_s", "circuit_s", "vendor_exec_ms", "to_last_s", "held_s"],
    ),
    (
        "e5",
        "cores held through the device's queue, per job",
        ["queue", "arm"],
        [
            "vendor_queue_s",
            "to_first_s",
            "held_s",
            "held_core_s",
            "queue_core_s",
            "scout_core_s",
            "pair_core_s",
        ],
    ),
)


def batches(e2):
    """Per batch: pairs asked for, created, finished, and the makespan from
    the first submit to the last finish."""
    print("== e2: pairs at once, per batch ==")
    by = defaultdict(list)
    for r in e2:
        by[(r["batch"], r["trial"])].append(r)
    print(
        "  "
        + "  ".join(
            f"{h:>12s}" for h in ("asked", "created", "finished", "makespan_s", "n")
        )
    )
    agg = defaultdict(list)
    for (k, _), rs in by.items():
        done = [r for r in rs if not r["timedout"]]
        subs = [num(r["submit"]) for r in rs if num(r["submit"])]
        fins = [num(r["finish"]) for r in done if num(r["finish"])]
        span = (max(fins) - min(subs)) if subs and fins else None
        agg[k].append((len(rs), len(done), span))
    for k in sorted(agg):
        rows = agg[k]
        created = sum(c for c, _, _ in rows) / len(rows)
        finished = sum(d for _, d, _ in rows) / len(rows)
        spans = [x for _, _, x in rows if x is not None]
        span = f"{sum(spans) / len(spans):.3f}" if spans else "-"
        print(
            "  "
            + "  ".join(
                f"{x:>12s}"
                for x in (
                    str(k),
                    f"{created:.1f}",
                    f"{finished:.1f}",
                    span,
                    str(len(rows)),
                )
            )
        )
    print()


def checks(path):
    """e4 rows, as recorded."""
    import csv

    with open(path) as fh:
        rows = list(csv.DictReader(fh))
    if not rows or "check" not in rows[0]:
        return
    print("== e4: the hold is released whatever happens to the pair ==")
    print(
        "  "
        + "  ".join(
            f"{h:>16s}"
            for h in (
                "check",
                "trial",
                "session_status",
                "classical_rc",
                "scout_rc",
                "note",
            )
        )
    )
    for r in rows:
        cells = (
            r["check"],
            r["trial"],
            r["session_status"],
            r["classical_rc"],
            r["scout_rc"],
            r["note"],
        )
        print("  " + "  ".join(f"{str(x):>16s}" for x in cells))
    left = [r for r in rows if r["session_status"] not in ("ended", "none")]
    print()
    print("  %d checks, %d left a session that was not ended" % (len(rows), len(left)))
    print()


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
    e4 = [p for p in paths if p.endswith("e4.csv")]
    paths = [p for p in paths if p not in e4]
    rows = load(paths) if paths else []
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
    e2 = [r for r in rows if r["exp"] == "e2"]
    if e2:
        batches(e2)
    for p in e4:
        checks(p)
    failed = sum(r["failed"] for r in rows)
    if failed:
        print(f"note: {failed} circuits did not complete, see the -jobs directories")


if __name__ == "__main__":
    main(sys.argv[1:] or ["results/e1.csv"])
