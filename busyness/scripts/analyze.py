#!/usr/bin/env python3
"""Paired analysis of the busy-ness sweep.

Reads the per-level CSVs that scripts/experiment.sh wrote and reports, per
level, the PAIRED per-trial contrast (both arms replayed the same sampled
depth, so each trial yields one difference):

  vendor_wait_s   what the queue cost that trial (overhead + depth*service)
  held_core_s     cores * seconds held while anything was pending:
                  baseline = size * (finish - alloc)
                  cosched  = size * (finish - alloc) + 1 * (scout span)
  release_lat_s   cosched only: classical start - session memo
  saved_core_s    baseline held - cosched held, per trial

Point estimates are medians with 95% bootstrap confidence intervals
(10,000 resamples, seeded), which is the robust summary a sampled sweep
calls for; means are shown beside them. Censored (timed-out) trials are
reported, never silently dropped.

    python3 scripts/analyze.py results/*.csv > results/summary.txt
"""

import csv
import random
import statistics
import sys


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def boot_ci(xs, stat=statistics.median, n=10000, seed=7):
    if len(xs) < 2:
        return (None, None)
    rng = random.Random(seed)
    reps = sorted(stat(rng.choices(xs, k=len(xs))) for _ in range(n))
    return (reps[int(0.025 * n)], reps[int(0.975 * n)])


def fmt(v):
    return "{:8.2f}".format(v) if v is not None else "       -"


def main(paths):
    rows = []
    for p in paths:
        with open(p) as fh:
            rows.extend(csv.DictReader(fh))
    levels = {}
    for r in rows:
        levels.setdefault(r["level"], {}).setdefault(
            (r["trial"], r["depth"]), {}
        )[r["arm"]] = r

    print("paired busy-ness sweep: median [95% bootstrap CI] (mean), n pairs")
    print()
    for level in sorted(levels, key=lambda L: min(levels[L], key=lambda k: k)):
        pairs = levels[level]
        saved, waits, lats, base_held, co_held = [], [], [], [], []
        censored = 0
        for (_t, depth), arms in sorted(pairs.items()):
            b, c = arms.get("baseline"), arms.get("coscheduled")
            if not b or not c:
                continue
            vals = (f(b["finish"]), f(b["alloc"]), f(c["finish"]), f(c["alloc"]))
            if b["timedout"] == "1" or c["timedout"] == "1" or None in vals:
                # a timeout, or a job that never ran (e.g. cancelled by the
                # scout-death backstop): censored either way, never dropped
                censored += 1
                continue
            size = float(b["size"])
            wait = float(b["overhead"]) + float(depth) * float(b["service"])
            bh = size * (vals[0] - vals[1])
            span = (f(c["scout_finish"]) or 0) - (f(c["scout_start"]) or 0)
            ch = size * (vals[2] - vals[3]) + max(span, 0)
            waits.append(wait)
            base_held.append(bh)
            co_held.append(ch)
            saved.append(bh - ch)
            if f(c["priority"]) and f(c["start"]):
                lats.append(f(c["start"]) - f(c["priority"]))
        n = len(saved)
        if not n:
            print("{:>8}: no complete pairs ({} censored)".format(level, censored))
            continue
        lo, hi = boot_ci(saved)
        print(
            "{:>8}: n={:<3} censored={:<2} vendor wait s: med {} | "
            "held core-s baseline {} cosched {} | SAVED {} [{} .. {}] "
            "(mean {:.2f}) | release lat s med {}".format(
                level,
                n,
                censored,
                fmt(statistics.median(waits)),
                fmt(statistics.median(base_held)),
                fmt(statistics.median(co_held)),
                fmt(statistics.median(saved)),
                fmt(lo),
                fmt(hi),
                statistics.mean(saved),
                fmt(statistics.median(lats) if lats else None),
            )
        )
    print()
    print("saved core-s per trial = size*(baseline span) - [size*(cosched")
    print("span) + scout span]. Positive means coscheduling returned cores")
    print("to the cluster while the vendor queue was waited out.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or sys.exit("usage: analyze.py results/*.csv")))
