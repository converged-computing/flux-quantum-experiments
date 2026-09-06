#!/usr/bin/env python3
"""Figures from the experiment CSVs, a pdf and a png each into plots/.

    ./plot.py results/*.csv

    fig1-held        allocation held, by vendor queue depth
    fig3-knee        where coscheduling stops paying, and what bounds the cost
    fig4-waste       total core seconds, by allocation size
    fig5-collapse    every size breaks at the same count of preempted cores
    fig6-knee-size   the measured knee against the prediction
    fig7-admission   pairs admitted against pairs asked for

A quantity over an ordered numeric axis is a line with a spread ribbon. Needs
matplotlib, pandas and numpy.
"""

import math
import os
import sys

from metrics import NODE_RATE, load

OUTDIR = "plots"

# Okabe and Ito, safe for the common colour vision deficiencies
COLOR = {"baseline": "#D55E00", "coscheduled": "#009E73"}


def style():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update(
        {
            "figure.dpi": 150,
            "savefig.dpi": 300,
            "savefig.bbox": "tight",
            "font.size": 10,
            "axes.titlesize": 11,
            "axes.labelsize": 10,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": True,
            "axes.axisbelow": True,
            "grid.color": "#DDDDDD",
            "grid.linewidth": 0.6,
            "legend.frameon": False,
            "legend.fontsize": 9,
            "xtick.labelsize": 9,
            "ytick.labelsize": 9,
            "pdf.fonttype": 42,  # editable text in the pdf
            "ps.fonttype": 42,
        }
    )
    return plt


def save(fig, name):
    for ext in ("pdf", "png"):
        fig.savefig(f"{OUTDIR}/{name}.{ext}")
    print(f"wrote {OUTDIR}/{name}.pdf and .png")


def frame(rows, exp):
    import pandas as pd

    sub = [r for r in rows if r["exp"] == exp]
    return pd.DataFrame(sub) if sub else None


def lineband(ax, df, x, y):
    """Median line with the interquartile range, per arm. Censored trials
    have no measured cost, so they are left out rather than priced."""
    for arm in COLOR:
        d = df[(df["arm"] == arm) & (df["timedout"] == 0)]
        if d.empty or d[y].isna().all():
            continue
        g = d.groupby(x)[y]
        mid, lo, hi = g.median(), g.quantile(0.25), g.quantile(0.75)
        ax.fill_between(
            mid.index, lo.values, hi.values, color=COLOR[arm], alpha=0.22, linewidth=0
        )
        ax.plot(
            mid.index,
            mid.values,
            color=COLOR[arm],
            marker="o",
            markersize=4,
            linewidth=1.7,
            label=arm,
        )
    ax.margins(x=0.03)


def fig_held(e1, plt):
    """The baseline holds its nodes through the vendor wait, the coscheduled
    arm does not, and the shaded gap is the saving."""
    fig, ax = plt.subplots(figsize=(6.4, 3.9))
    held = {}
    for arm in COLOR:
        d = e1[e1["arm"] == arm]
        if d.empty:
            continue
        h = (d["finish"].astype(float) - d["alloc"].astype(float)).groupby(d["depth"])
        mid, lo, hi = h.median(), h.quantile(0.25), h.quantile(0.75)
        held[arm] = mid
        ax.fill_between(
            mid.index, lo.values, hi.values, color=COLOR[arm], alpha=0.2, linewidth=0
        )
        ax.plot(
            mid.index,
            mid.values,
            color=COLOR[arm],
            marker="o",
            markersize=4,
            linewidth=1.7,
            label=arm,
        )
    if len(held) == 2:
        common = held["baseline"].index.intersection(held["coscheduled"].index)
        ax.fill_between(
            common,
            held["coscheduled"][common].values,
            held["baseline"][common].values,
            color="#999999",
            alpha=0.25,
            linewidth=0,
            label="allocation time coscheduling avoids",
        )
    ax.set_xlabel("vendor queue depth (tasks ahead)")
    ax.set_ylabel("allocation held (s)")
    ax.set_title("The baseline holds its nodes through the vendor queue wait")
    ax.legend(loc="upper left")
    ax.margins(x=0.02)
    save(fig, "fig1-held")
    plt.close(fig)


def fig_knee(e2, plt):
    """Below the knee the pair fits and the arms cost the same. Above it the
    classical cannot start the moment it is released, the session is billed
    for the gap, and preemption is what keeps the gap short. The baseline
    does not start at all there, since nothing preempts for it."""
    fig, ax = plt.subplots(figsize=(6.4, 3.9))
    lineband(ax, e2, "load_pct", "quantum_usd")
    cos = e2[(e2["arm"] == "coscheduled") & (e2["timedout"] == 0)]
    if not cos.empty:
        by_load = cos.groupby("load_pct")["quantum_usd"].mean()
        floor = by_load.min()
        above = by_load[by_load > floor * 1.05]
        if len(above):
            knee = int(above.index.min())
            step = above.mean() - floor
            ax.axvline(knee - 1, color="#333333", linestyle=":", linewidth=1.2)
            ax.annotate(
                "the pair stops fitting at {}%\npreemption makes room, and the\nsession is billed ${:.2f} for the wait".format(
                    knee, step
                ),
                xy=(0.04, 0.62),
                xycoords="axes fraction",
                fontsize=9,
                color="#666666",
            )
    ax.set_xlabel("classical utilisation (%)")
    ax.set_ylabel("billed vendor session (USD)")
    ax.set_title("Preemption bounds what contention costs the session")
    ax.legend(loc="center left")
    save(fig, "fig3-knee")
    plt.close(fig)


def fig_waste(e3, plt):
    """Total core seconds, scout included, which is what a site pays."""
    fig, ax = plt.subplots(figsize=(6.4, 3.9))
    for arm in COLOR:
        d = e3[e3["arm"] == arm]
        if d.empty:
            continue
        g = d.groupby("size")["total_node_s"]
        mean, sd = g.mean(), g.std().fillna(0)
        ax.errorbar(
            mean.index,
            mean.values,
            yerr=sd.values,
            color=COLOR[arm],
            marker="o",
            markersize=5,
            linewidth=1.7,
            capsize=3,
            label=arm,
        )
    be = e3[e3["arm"] == "baseline"]["breakeven_size"].dropna()
    if not be.empty:
        x = float(be.mean())
        ax.axvline(x, color="#999999", linestyle=":", linewidth=1)
        ax.annotate(
            f"break even, size {x:.1f}",
            xy=(x, 0.55),
            xycoords=("data", "axes fraction"),
            rotation=90,
            fontsize=8,
            color="#666666",
            ha="right",
            va="center",
        )
    ax.set_xlabel("allocation size (tasks)")
    ax.set_ylabel("total core seconds consumed, scout included")
    ax.set_title("Identical work, and the baseline consumes roughly twice the cluster")
    ax.annotate(
        "scout core included, bars are one sd over 10 trials",
        xy=(0.97, 0.10),
        xycoords="axes fraction",
        ha="right",
        fontsize=8,
        color="#666666",
    )
    ax.legend(loc="upper left")
    ax.margins(x=0.04)
    save(fig, "fig4-waste")
    plt.close(fig)


def fig_knee_size(e4, plt, cores):
    """Against cores taken from preemption, every allocation size lands on the
    same curve. The axis is submit to allocated, which counts the grace period
    once wherever it fell. Then the measured knee against the predicted one."""
    import numpy as np

    sizes = sorted(e4["size"].unique())
    cmap = plt.get_cmap("viridis")
    cos = e4[e4["arm"] == "coscheduled"]

    fig, ax = plt.subplots(figsize=(6.4, 3.9))
    for i, size in enumerate(sizes):
        d = cos[cos["size"] == size]
        m = d.groupby("preempt_cores")["time_to_alloc_s"].median()
        # the sizes land on top of each other, so nudge them apart
        nudge = (i - (len(sizes) - 1) / 2) * 0.05
        ax.plot(
            m.index + nudge,
            m.values,
            marker="o",
            markersize=5,
            linewidth=1.4,
            alpha=0.85,
            color=cmap(i / max(1, len(sizes) - 1)),
            label=f"{size} tasks",
        )
    ax.axvline(0.5, color="#333333", linestyle=":", linewidth=1.2)
    ax.annotate(
        "fits without\npreempting",
        xy=(0.04, 0.30),
        xycoords="axes fraction",
        fontsize=9,
        color="#B04000",
    )
    ax.set_yscale("log")
    ax.set_xlabel("cores the pair had to take from preemption")
    ax.set_ylabel("submit to classical allocated (s), log scale")
    ax.set_title("One grace period, landing in whichever half has to wait")
    ax.legend(
        title="allocation",
        loc="lower left",
        bbox_to_anchor=(0.0, -0.42),
        ncol=4,
        frameon=False,
    )
    save(fig, "fig5-collapse")
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(6.4, 3.9))
    measured, predicted = [], []
    for size in sizes:
        d = cos[cos["size"] == size]
        m = d.groupby("load_pct")["release_lat_s"].mean()
        broke = [l for l, v in m.items() if v > 1.0]
        measured.append(min(broke) if broke else np.nan)
        # experiment.sh computes the load with integer division, so floor the same way
        predicted.append(int((cores - size) * 100 // cores))
    ax.plot(
        sizes,
        predicted,
        color="#999999",
        linewidth=9,
        alpha=0.55,
        solid_capstyle="round",
        label="predicted break, free cores = request (pair needs one more)",
    )
    ax.plot(
        sizes,
        measured,
        marker="o",
        markersize=8,
        linewidth=0,
        color=COLOR["coscheduled"],
        label="measured knee",
    )
    for x, y in zip(sizes, measured):
        if not math.isnan(y):
            ax.annotate(
                f"{y:.0f}%",
                (x, y),
                textcoords="offset points",
                xytext=(13, -4),
                ha="left",
                fontsize=9,
                color=COLOR["coscheduled"],
            )
    ax.set_xlabel("allocation size (tasks)")
    ax.set_ylabel("utilisation at the knee (%)")
    ax.set_title("The knee moves with the size of the request")
    ax.legend(loc="lower left", bbox_to_anchor=(0.0, -0.38), ncol=2, frameon=False)
    ax.margins(x=0.06)
    save(fig, "fig6-knee-size")
    plt.close(fig)


def fig_admission(e6, plt):
    """Admitted saturates at what the cores can reach. The rest are refused
    at submit, with no job and no vendor session."""
    e6 = e6.copy()
    e6["asked"] = e6["bg_total"].astype(float).astype(int)
    e6["admitted"] = e6["bg_done"].astype(float).astype(int)
    fig, ax = plt.subplots(figsize=(6.4, 3.9))
    g = e6.groupby("asked")["admitted"]
    asked = sorted(e6["asked"].unique())
    med = [g.median().get(k) for k in asked]
    lo = [g.min().get(k) for k in asked]
    hi = [g.max().get(k) for k in asked]
    ax.plot(
        asked,
        asked,
        color="#BBBBBB",
        linewidth=6,
        solid_capstyle="round",
        label="asked for",
        zorder=1,
    )
    ax.fill_between(asked, lo, hi, color="#0E7C61", alpha=0.18, zorder=2)
    ax.plot(
        asked, med, marker="o", color="#0E7C61", linewidth=2, label="admitted", zorder=3
    )
    ax.annotate(
        "admitted stops at {:g}, what the cores allow.\nthe rest are refused at submit, with no\njob and no vendor session".format(
            max(med) if med else 0
        ),
        xy=(0.04, 0.68),
        xycoords="axes fraction",
        fontsize=9,
        color="#666666",
    )
    ax.set_xlabel("pairs asked for at once")
    ax.set_ylabel("pairs admitted")
    ax.set_title("Admission promises the pair will run, not that the sum fits")
    ax.set_xticks(asked)
    ax.legend(loc="lower right", frameon=False)
    save(fig, "fig7-admission")
    plt.close(fig)


def main(paths):
    from metrics import cores_for

    rows = load(paths)
    if not rows:
        sys.exit("no rows, pass the experiment CSVs")
    cores = cores_for(paths[0])
    # a stale figure next to a fresh one is worse than none
    if os.path.isdir(OUTDIR):
        for old in sorted(os.listdir(OUTDIR)):
            if old.endswith((".png", ".pdf")):
                os.remove(os.path.join(OUTDIR, old))
    os.makedirs(OUTDIR, exist_ok=True)
    plt = style()

    e1, e2, e3, e4, e6 = (frame(rows, e) for e in ("e1", "e2", "e3", "e4", "e6"))
    if e1 is not None:
        fig_held(e1, plt)
    if e2 is not None:
        # the load cannot schedule itself at 100, so the cluster sits empty
        fig_knee(e2[e2["load_pct"] < 100], plt)
    if e3 is not None:
        fig_waste(e3, plt)
    if e4 is not None:
        fig_knee_size(e4, plt, cores)
    if e6 is not None:
        fig_admission(e6, plt)

    print(f"\n{len(rows)} rows from {len(paths)} files, cores={cores}")
    if NODE_RATE == 0.0:
        print("NODE_RATE is 0, so the classical side is not priced")


if __name__ == "__main__":
    main(sys.argv[1:])
