#!/usr/bin/env python3
"""Figures from the queue samples, a pdf and a png each into plots/.

    ./plot.py results/queues.jsonl
    ./plot.py --out plots-synthetic results-synthetic/queues.jsonl

    fig1-<machine>   the same machine through both doors: Braket's count and
                     IonQ's average, one panel each on a shared time axis
    fig2-dist        each device's measure as a distribution
    fig3-hours       median by hour of day, one panel per vendor
    fig4-agree       Braket's count against IonQ's average at the same tick,
                     one panel per paired machine, with the rank correlation

One y axis per panel. Two measures of different units never share one, so
a paired machine is two panels, not two scales. Needs matplotlib.
"""

import argparse
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from metrics import (  # noqa: E402
    by_device,
    hour_of,
    load,
    measure,
    paired,
    quantiles,
    spearman,
)

HERE = os.path.dirname(os.path.abspath(__file__))

# Okabe and Ito, the sibling experiments' palette, one hue per vendor and
# never reassigned
COLOR = {"braket": "#D55E00", "ionq": "#0072B2", "ibm": "#009E73"}
UNIT = {
    "braket": "tasks waiting (Normal queue)",
    "ionq": "average queue time (s)",
    "ibm": "pending jobs",
}
INK, MUTED, GRID = "#333333", "#777777", "#DDDDDD"


def style():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update(
        {
            "font.size": 9,
            "axes.edgecolor": MUTED,
            "axes.labelcolor": INK,
            "axes.titlecolor": INK,
            "xtick.color": INK,
            "ytick.color": INK,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": True,
            "grid.color": GRID,
            "grid.linewidth": 0.6,
            "legend.frameon": False,
            "figure.dpi": 120,
        }
    )
    return plt


def save(fig, out, name):
    os.makedirs(out, exist_ok=True)
    for ext in ("pdf", "png"):
        fig.savefig(os.path.join(out, "{}.{}".format(name, ext)), bbox_inches="tight")
    print("  {}/{}.png".format(out, name))


def short(device):
    return device.split("/")[-1]


def days(ts, t0):
    return [(t - t0) / 86400 for t in ts]


def fig_pairs(plt, rows, pairs, out):
    for name, trip in paired(rows, pairs).items():
        if len(trip) < 3:
            continue
        t0 = trip[0][0]
        fig, axes = plt.subplots(2, 1, figsize=(8, 4.2), sharex=True)
        for ax, which, idx in ((axes[0], "braket", 1), (axes[1], "ionq", 2)):
            ys = [measure(r[idx]) for r in trip]
            ax.plot(
                days([r[0] for r in trip], t0), ys, color=COLOR[which], linewidth=1.2
            )
            ax.set_ylabel(UNIT[which])
            ax.text(0.01, 0.92, which, transform=ax.transAxes, color=INK, fontsize=9)
            ax.set_ylim(bottom=0)
        axes[1].set_xlabel("days since the first sample")
        fig.suptitle(
            "{}: the same machine through Braket and through IonQ".format(name),
            color=INK,
        )
        save(fig, out, "fig1-{}".format(name))
        plt.close(fig)


def fig_dist(plt, rows, out):
    dev = by_device(rows)
    vendors = sorted({v for v, _ in dev})
    fig, axes = plt.subplots(
        1, len(vendors), figsize=(3.2 * len(vendors), 3), squeeze=False
    )
    for ax, vendor in zip(axes[0], vendors):
        for (v, device), rs in sorted(dev.items()):
            if v != vendor:
                continue
            xs = sorted(x for x in (measure(r) for r in rs) if x is not None)
            if not xs:
                continue
            ax.plot(
                xs,
                [k / len(xs) for k in range(1, len(xs) + 1)],
                color=COLOR[vendor],
                linewidth=1.2,
                alpha=0.9,
            )
            ax.text(
                xs[-1],
                1.0,
                short(device),
                color=INK,
                fontsize=7,
                ha="right",
                va="bottom",
            )
        ax.set_xlabel(UNIT[vendor])
        ax.set_ylabel("fraction of samples at or below")
        ax.set_ylim(0, 1.05)
    fig.suptitle("what each device's queue looked like, every sample", color=INK)
    save(fig, out, "fig2-dist")
    plt.close(fig)


def fig_hours(plt, rows, out, tz):
    dev = by_device(rows)
    vendors = sorted({v for v, _ in dev})
    fig, axes = plt.subplots(
        1, len(vendors), figsize=(3.4 * len(vendors), 3), squeeze=False
    )
    for ax, vendor in zip(axes[0], vendors):
        for (v, device), rs in sorted(dev.items()):
            if v != vendor:
                continue
            by_h = defaultdict(list)
            for r in rs:
                by_h[hour_of(r["t"], tz)[0]].append(measure(r))
            hs = sorted(by_h)
            med = [quantiles(by_h[h]).get("0.5") for h in hs]
            ax.plot(
                hs, med, color=COLOR[vendor], linewidth=1.2, marker="o", markersize=3
            )
            ax.text(
                hs[-1], med[-1], " " + short(device), color=INK, fontsize=7, va="center"
            )
        ax.set_xlabel("hour of day (UTC{:+d})".format(tz))
        ax.set_ylabel("median " + UNIT[vendor])
        ax.set_xticks(range(0, 24, 6))
    fig.suptitle("when the queue is short", color=INK)
    save(fig, out, "fig3-hours")
    plt.close(fig)


def fig_agree(plt, rows, pairs, out):
    pr = {k: v for k, v in paired(rows, pairs).items() if len(v) >= 3}
    if not pr:
        return
    fig, axes = plt.subplots(1, len(pr), figsize=(3.4 * len(pr), 3.2), squeeze=False)
    for ax, (name, trip) in zip(axes[0], sorted(pr.items())):
        xs = [measure(b) for _, b, _ in trip]
        ys = [measure(i) for _, _, i in trip]
        keep = [k for k in range(len(xs)) if xs[k] is not None and ys[k] is not None]
        xs, ys = [xs[k] for k in keep], [ys[k] for k in keep]
        ax.scatter(xs, ys, s=10, color=COLOR["ionq"], alpha=0.35, edgecolors="none")
        rho = spearman(xs, ys)
        ax.set_title(
            "{}  rank correlation {}".format(
                name, "-" if rho is None else "{:.2f}".format(rho)
            ),
            color=INK,
            fontsize=9,
        )
        ax.set_xlabel("Braket: " + UNIT["braket"])
        ax.set_ylabel("IonQ: " + UNIT["ionq"])
    fig.suptitle("do the two doors show the same queue", color=INK)
    save(fig, out, "fig4-agree")
    plt.close(fig)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("files", nargs="+")
    ap.add_argument("--devices", default=os.path.join(HERE, "..", "devices.json"))
    ap.add_argument("--out", default=os.path.join(HERE, "..", "plots"))
    ap.add_argument("--tz", type=int, default=0)
    args = ap.parse_args(argv)
    plt = style()
    rows = load(args.files)
    with open(args.devices) as f:
        pairs = json.load(f).get("pairs", {})
    fig_pairs(plt, rows, pairs, args.out)
    fig_dist(plt, rows, args.out)
    fig_hours(plt, rows, args.out, args.tz)
    fig_agree(plt, rows, pairs, args.out)


if __name__ == "__main__":
    main()
