#!/usr/bin/env python3
"""Plot the busy-ness sweep: core-seconds held, baseline vs coscheduled.

Every trial contributes one dot per arm, both arms having replayed the
SAME sampled queue depth. The baseline holds its full allocation through
the vendor wait, so it climbs with busy-ness; the coscheduled arm holds
almost nothing extra, so it stays flat. The widening gap is the saving.
Means with 95% bootstrap CIs are overlaid per arm, medians as ticks.

    python3 scripts/plot.py results -o results/held-vs-busyness.png

The positional argument is a directory of per-level CSVs that
scripts/experiment.sh wrote. Needs matplotlib; everything else is stdlib.
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
ARMS = [("baseline", "#257de0"), ("coscheduled", "#f47230")]
TEXT, MUTED, GRID = "#191919", "#666666", "#e3e3e3"


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def held_per_trial(result_dir):
    """level -> {"baseline": [core-s], "coscheduled": [core-s]}, pairs only,
    same arithmetic as analyze.py (cosched includes the scout's span)."""
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
        held = out.setdefault(level, {"baseline": [], "coscheduled": []})
        held["baseline"].append(size * (vals[0] - vals[1]))
        held["coscheduled"].append(size * (vals[2] - vals[3]) + max(span, 0))
    return out


def boot_ci_mean(xs, n=10000, seed=7):
    rng = random.Random(seed)
    reps = sorted(statistics.mean(rng.choices(xs, k=len(xs))) for _ in range(n))
    return reps[int(0.025 * n)], reps[int(0.975 * n)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("results", nargs="?", default="results",
                    help="directory of experiment.sh CSVs")
    ap.add_argument("-o", "--out", default="results/held-vs-busyness.png")
    args = ap.parse_args()

    data = held_per_trial(args.results)
    if not data:
        sys.exit("plot.py: no complete pairs under {}".format(args.results))
    levels = sorted(data, key=lambda lv: (LEVEL_ORDER.get(lv, 99), lv))

    fig, ax = plt.subplots(figsize=(7.2, 4.2), dpi=160)
    rng = random.Random(11)
    for ai, (arm, color) in enumerate(ARMS):
        off = (ai - 0.5) * 0.34  # arms side by side inside each level slot
        for xi, lv in enumerate(levels):
            xs = data[lv][arm]
            if not xs:
                continue
            jitter = [xi + off + rng.uniform(-0.09, 0.09) for _ in xs]
            ax.scatter(jitter, xs, s=14, color=color, alpha=0.5, linewidths=0, zorder=2)
            mean = statistics.mean(xs)
            lo, hi = boot_ci_mean(xs)
            ax.vlines(xi + off, lo, hi, color=color, linewidth=2, zorder=3)
            ax.scatter([xi + off], [mean], s=52, color=color, zorder=4,
                       edgecolors="white", linewidths=1.2,
                       label=arm if xi == 0 else None)
            ax.hlines(statistics.median(xs), xi + off - 0.12, xi + off + 0.12,
                      color=color, linewidth=1.4, zorder=3)
            if lv == levels[-1]:
                ax.annotate("mean {:.0f}".format(mean),
                            (xi + off, mean), textcoords="offset points",
                            xytext=(10, -4), fontsize=8, color=TEXT)

    allvals = [v for lv in data.values() for xs in lv.values() for v in xs]
    if min(allvals) > 0:
        ax.set_yscale("log")
    ax.set_xticks(range(len(levels)))
    ax.set_xticklabels(levels)
    ax.set_xlabel("busy-ness level (scaling of the measured 42-day distribution)",
                  color=MUTED, fontsize=9)
    ax.set_ylabel("core-seconds held per trial", color=MUTED, fontsize=9)
    ax.set_title("The baseline pays for the queue, coscheduling does not",
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
             "dot = one trial arm (both arms replay the same sampled depth)  |  "
             "filled circle = mean, bar = 95% bootstrap CI, tick = median",
             fontsize=7.5, color=MUTED)
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    fig.savefig(args.out, facecolor="white")
    root, _ = os.path.splitext(args.out)
    fig.savefig(root + ".pdf", facecolor="white")
    print("wrote", args.out, "and", root + ".pdf")


if __name__ == "__main__":
    main()
