#!/usr/bin/env python3
"""Plot the busy-ness sweep: per-trial saved core-seconds vs level.

Every trial is a dot, because the distribution IS the result: most draws
from the measured queue distribution are idle (the flat ~22 core-s floor),
and the heavy tail carries the effect. A bar of means would hide that.
Means with bootstrap CIs are overlaid per level, medians as ticks.

    python3 scripts/plot.py "run 1=_results" "run 2=results" -o results/saved-vs-busyness.png

Each positional argument is label=dir where dir holds the per-level CSVs
that scripts/experiment.sh wrote. Needs matplotlib; everything else is
stdlib.
"""

import argparse
import csv
import glob
import os
import random
import statistics
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

# ordered by scale factor; anything unexpected sorts to the end by name
LEVEL_ORDER = {"idle": 0, "today": 1, "x2": 2, "x5": 5, "x10": 10}
# categorical slots 1 and 2 of the validated reference palette
SERIES = ["#257de0", "#f47230"]
TEXT, MUTED, GRID = "#191919", "#666666", "#e3e3e3"


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def saved_per_trial(result_dir):
    """level -> [saved core-s per complete pair], same arithmetic as analyze.py."""
    rows = []
    for p in sorted(glob.glob(os.path.join(result_dir, "*.csv"))):
        with open(p) as fh:
            rows.extend(csv.DictReader(fh))
    pairs = {}
    for r in rows:
        pairs.setdefault((r["level"], r["trial"], r["depth"]), {})[r["arm"]] = r
    out = {}
    for (level, _t, _d), arms in pairs.items():
        b, c = arms.get("baseline"), arms.get("coscheduled")
        if not b or not c:
            continue
        vals = (f(b["finish"]), f(b["alloc"]), f(c["finish"]), f(c["alloc"]))
        if b["timedout"] == "1" or c["timedout"] == "1" or None in vals:
            continue
        size = float(b["size"])
        span = (f(c["scout_finish"]) or 0) - (f(c["scout_start"]) or 0)
        saved = size * (vals[0] - vals[1]) - (size * (vals[2] - vals[3]) + max(span, 0))
        out.setdefault(level, []).append(saved)
    return out


def boot_ci_mean(xs, n=10000, seed=7):
    rng = random.Random(seed)
    reps = sorted(statistics.mean(rng.choices(xs, k=len(xs))) for _ in range(n))
    return reps[int(0.025 * n)], reps[int(0.975 * n)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+", help="label=dir of experiment.sh CSVs")
    ap.add_argument("-o", "--out", default="results/saved-vs-busyness.png")
    args = ap.parse_args()

    runs = []
    for spec in args.runs:
        label, _, d = spec.partition("=")
        if not d:
            label, d = os.path.basename(spec.rstrip("/")), spec
        runs.append((label, saved_per_trial(d)))
    if len(runs) > len(SERIES):
        sys.exit("plot.py: at most {} runs, one hue each".format(len(SERIES)))

    levels = sorted(
        {lv for _, data in runs for lv in data},
        key=lambda lv: (LEVEL_ORDER.get(lv, 99), lv),
    )

    fig, ax = plt.subplots(figsize=(7.2, 4.2), dpi=160)
    rng = random.Random(11)
    nruns = len(runs)
    for ri, (label, data) in enumerate(runs):
        color = SERIES[ri]
        # side-by-side offsets inside each level slot
        off = (ri - (nruns - 1) / 2.0) * 0.34
        for xi, lv in enumerate(levels):
            xs = data.get(lv, [])
            if not xs:
                continue
            jitter = [xi + off + rng.uniform(-0.09, 0.09) for _ in xs]
            ax.scatter(jitter, xs, s=14, color=color, alpha=0.5, linewidths=0, zorder=2)
            mean = statistics.mean(xs)
            lo, hi = boot_ci_mean(xs)
            ax.vlines(xi + off, lo, hi, color=color, linewidth=2, zorder=3)
            ax.scatter([xi + off], [mean], s=52, color=color, zorder=4,
                       edgecolors="white", linewidths=1.2,
                       label=label if xi == 0 else None)
            ax.hlines(statistics.median(xs), xi + off - 0.12, xi + off + 0.12,
                      color=color, linewidth=1.4, zorder=3)
            if lv == levels[-1]:
                ax.annotate("mean {:.0f}".format(mean),
                            (xi + off, mean), textcoords="offset points",
                            xytext=(10, -4), fontsize=8, color=TEXT)

    allvals = [v for _, d in runs for xs in d.values() for v in xs]
    if min(allvals) > 0:
        ax.set_yscale("log")
    ax.set_xticks(range(len(levels)))
    ax.set_xticklabels(levels)
    ax.set_xlabel("busy-ness level (scaling of the measured 42-day distribution)",
                  color=MUTED, fontsize=9)
    ax.set_ylabel("saved core-seconds per trial (paired)", color=MUTED, fontsize=9)
    ax.set_title("Coscheduling returns more as queues densify",
                 color=TEXT, fontsize=11, loc="left")
    ax.tick_params(colors=MUTED, labelsize=9)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.yaxis.grid(True, color=GRID, linewidth=0.6, zorder=0)
    ax.set_axisbelow(True)
    leg = ax.legend(frameon=False, fontsize=9, loc="upper left")
    for t in leg.get_texts():
        t.set_color(TEXT)
    fig.text(0.01, 0.01,
             "dot = one paired trial  |  filled circle = mean, bar = 95% bootstrap CI,"
             " tick = median  |  both arms replay the same sampled depth",
             fontsize=7.5, color=MUTED)
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    fig.savefig(args.out, facecolor="white")
    root, _ = os.path.splitext(args.out)
    fig.savefig(root + ".pdf", facecolor="white")
    print("wrote", args.out, "and", root + ".pdf")


if __name__ == "__main__":
    main()
