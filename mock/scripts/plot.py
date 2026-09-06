#!/usr/bin/env python3
"""Publication figures from the experiment CSVs.

    ./plot.py e1.csv e2.csv e3.csv e4.csv

Writes a pdf and a png per figure into plots/. Distributions are shown as
boxplots with the individual trials over the top, because with ten trials a
mean and an error bar throws away most of what was measured.

    fig1-held         how long the allocation is held, by vendor queue depth
    fig3-knee         where coscheduling stops paying
    fig4-waste        total cluster consumed, by allocation size
    fig5-collapse     every size breaks at the same spare core count
    fig6-knee-size    the measured knee against the prediction

Form follows the variable. A quantity measured over an ordered numeric axis is
a line with a spread ribbon, a comparison across a few discrete groups is bars
or a boxplot. Boxplots on a numeric axis force equal spacing and hide the
trend.

Needs matplotlib and seaborn. Everything is derived from raw timestamps, so the
model can change without another run.
"""

import csv
import math
import os
import sys

QUANTUM_RATE = 1.60      # dollars per second of held session, ibm pay as you go
NODE_RATE = 0.0          # dollars per core second, set for your instance
OUTDIR = "plots"
CORES = int(os.environ.get("CORES", 128))

# Okabe and Ito, safe for the common colour vision deficiencies and it prints
ARMS = {
    "baseline": ("#D55E00", "baseline"),
    "coscheduled": ("#009E73", "coscheduled"),
    "nowarmup": ("#0072B2", "no warmup"),
}
ORDER = ["baseline", "coscheduled", "nowarmup"]


def style():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({
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
        "pdf.fonttype": 42,      # editable text in the pdf, journals ask for it
        "ps.fonttype": 42,
    })
    return plt


def save(fig, name):
    for ext in ("pdf", "png"):
        path = f"{OUTDIR}/{name}.{ext}"
        fig.savefig(path)
    print(f"wrote {OUTDIR}/{name}.pdf and .png")


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def derive(row):
    arm = row["arm"]
    size = int(row["size"])
    work = float(row["work_s"])
    sub, pri = num(row["submit"]), num(row["priority"])
    alloc, start = num(row["alloc"]), num(row["start"])
    finish = num(row["finish"])
    s0, s1 = num(row["scout_start"]), num(row["scout_finish"])

    out = dict(row)
    out["size"] = size
    out["depth"] = int(row["depth"])
    out["load_pct"] = int(row["load_pct"])
    out["arm_label"] = ARMS.get(arm, (None, arm))[1]
    held = (finish - alloc) if (finish and alloc) else None

    if arm == "baseline":
        # the job takes its nodes first, so it holds them through the vendor
        # wait and nothing is billed for waiting
        wait = (held - work) if held is not None else None
        out["vendor_wait_s"] = wait
        out["release_lat_s"] = 0.0
        out["idle_node_s"] = wait * size if wait is not None else None
        out["billable_s"] = work
    elif arm in ("coscheduled", "nowarmup"):
        out["vendor_wait_s"] = (pri - sub) if (pri and sub) else None
        out["release_lat_s"] = (alloc - pri) if (alloc and pri) else None
        out["idle_node_s"] = 0.0
        out["billable_s"] = (s1 - pri) if (s1 and pri) else None
    else:
        out["vendor_wait_s"] = (pri - num(row["t0"])) if pri else None
        out["release_lat_s"] = (start - pri) if (start and pri) else None
        out["idle_node_s"] = 0.0
        out["billable_s"] = (s1 - pri) if (s1 and pri) else None

    # Cores left over once the load is placed, minus what the classical asks
    # for. The scout needs one more, so the pair fits when slack is positive.
    # prefer the recorded core count. A percentage cannot address every core
    # count on a 128 core machine, so deriving it back would be approximate.
    recorded = num(row.get("load_cores"))
    load_cores = int(recorded) if recorded else CORES * out["load_pct"] // 100
    out["load_cores"] = load_cores
    out["timedout"] = int(row.get("timedout") or 0)
    spare = CORES - load_cores
    out["slack"] = spare - size
    need = size + 1 if arm in ("coscheduled", "nowarmup") else size
    out["preempt_cores"] = max(0, need - spare)
    out["node_seconds"] = held * size if held is not None else None

    # The scout holds one core for the whole vendor wait and the classical run.
    # Leaving it out understates the design and flips the sign at small sizes.
    if arm in ("coscheduled", "nowarmup"):
        out["scout_node_s"] = (s1 - s0) if (s1 and s0) else None
    else:
        out["scout_node_s"] = 0.0
    if out["node_seconds"] is not None and out["scout_node_s"] is not None:
        out["total_node_s"] = out["node_seconds"] + out["scout_node_s"]
    else:
        out["total_node_s"] = None
    wait = out["vendor_wait_s"]
    out["breakeven_size"] = ((wait + work) / wait) if wait else None
    out["quantum_usd"] = (out["billable_s"] or 0) * QUANTUM_RATE
    out["classical_usd"] = (out["idle_node_s"] or 0) * NODE_RATE
    return out


def frame(rows, exp):
    import pandas as pd
    sub = [r for r in rows if r["exp"] == exp]
    return pd.DataFrame(sub) if sub else None


def lineband(ax, df, x, y, arms, logy=False, spread="iqr"):
    """Median line with a spread ribbon, for a quantity measured over an
    ordered numeric variable. A boxplot would force the x values to equal
    spacing and hide the trend, which is the thing being claimed."""
    present = [a for a in ORDER if a in arms]
    for arm in present:
        d = df[df["arm"] == arm]
        if d.empty or d[y].isna().all():
            continue
        g = d.groupby(x)[y]
        mid = g.median()
        if spread == "iqr":
            lo, hi = g.quantile(0.25), g.quantile(0.75)
        else:
            sd = g.std().fillna(0)
            lo, hi = mid - sd, mid + sd
        color, label = ARMS[arm]
        ax.fill_between(mid.index, lo.values, hi.values, color=color, alpha=0.22,
                        linewidth=0)
        ax.plot(mid.index, mid.values, color=color, marker="o", markersize=4,
                linewidth=1.7, label=label)
    if logy:
        ax.set_yscale("log")
    ax.margins(x=0.03)


def groupbars(ax, df, x, y, arms):
    """Grouped bars with the spread, for comparing two arms at a handful of
    discrete sizes."""
    import numpy as np

    present = [a for a in ORDER if a in arms]
    xs = sorted(df[x].unique())
    width = 0.8 / max(1, len(present))
    for i, arm in enumerate(present):
        d = df[df["arm"] == arm]
        means = [d[d[x] == v][y].mean() for v in xs]
        sds = [d[d[x] == v][y].std() for v in xs]
        sds = [0 if (s != s) else s for s in sds]
        pos = np.arange(len(xs)) + i * width - 0.4 + width / 2
        color, label = ARMS[arm]
        ax.bar(pos, means, width=width * 0.92, color=color, label=label,
               yerr=sds, capsize=2, error_kw={"linewidth": 0.8})
    ax.set_xticks(np.arange(len(xs)))
    ax.set_xticklabels([str(v) for v in xs])


def box(ax, df, x, y, arms, logy=False, showpoints=True):
    """Boxplot per arm with the individual trials over the top."""
    import seaborn as sns

    present = [a for a in ORDER if a in arms]
    palette = {ARMS[a][1]: ARMS[a][0] for a in present}
    order = [ARMS[a][1] for a in present]
    sns.boxplot(data=df, x=x, y=y, hue="arm_label", hue_order=order,
                palette=palette, ax=ax, showfliers=False, width=0.7,
                linewidth=0.9, legend=True)
    if showpoints:
        sns.stripplot(data=df, x=x, y=y, hue="arm_label", hue_order=order,
                      palette=palette, ax=ax, dodge=True, size=2.5,
                      alpha=0.55, linewidth=0, legend=False)
    if logy:
        ax.set_yscale("log")


def fig_held(e1, plt):
    """How long the allocation is actually held, for each arm, as the vendor
    queue deepens. The baseline arm holds its nodes through the queue wait, the
    coscheduled arm does not, and the gap between the two lines is the saving.
    Both are measured, nothing here is inferred."""
    fig, ax = plt.subplots(figsize=(6.4, 3.9))
    have = set(e1["arm"])
    for arm in ("baseline", "coscheduled"):
        if arm not in have:
            continue
        d = e1[e1["arm"] == arm]
        held = (d["finish"].astype(float) - d["alloc"].astype(float))
        d = d.assign(held_s=held)
        g = d.groupby("depth")["held_s"]
        mid, lo, hi = g.median(), g.quantile(0.25), g.quantile(0.75)
        color, label = ARMS[arm]
        ax.fill_between(mid.index, lo.values, hi.values, color=color,
                        alpha=0.2, linewidth=0)
        ax.plot(mid.index, mid.values, color=color, marker="o", markersize=4,
                linewidth=1.7, label=label)
    if "baseline" in have and "coscheduled" in have:
        a = e1[e1["arm"] == "baseline"]
        b = e1[e1["arm"] == "coscheduled"]
        ha = (a["finish"].astype(float) - a["alloc"].astype(float)).groupby(a["depth"]).median()
        hb = (b["finish"].astype(float) - b["alloc"].astype(float)).groupby(b["depth"]).median()
        common = ha.index.intersection(hb.index)
        ax.fill_between(common, hb[common].values, ha[common].values,
                        color="#999999", alpha=0.25, linewidth=0,
                        label="allocation time coscheduling avoids")
    ax.set_xlabel("vendor queue depth (tasks ahead)")
    ax.set_ylabel("allocation held (s)")
    ax.set_title("The baseline arm holds its nodes through the vendor queue wait")
    ax.legend(loc="upper left")
    ax.margins(x=0.02)
    save(fig, "fig1-held")
    plt.close(fig)


def fig_latency(e1, plt):
    fig, ax = plt.subplots(figsize=(6.4, 3.8))
    released = e1[e1["arm"] != "baseline"]
    lineband(ax, released, "depth", "release_lat_s",
             set(e1["arm"]) - {"baseline"}, logy=True)
    ax.set_xlabel("vendor queue depth (tasks ahead)")
    ax.set_ylabel("submit to classical allocated (s), log scale")
    ax.set_title("Release latency does not grow with vendor queue depth")
    ax.annotate("band is the interquartile range",
                xy=(0.97, 0.04), xycoords="axes fraction", ha="right",
                fontsize=8, color="#666666")
    ax.legend(title=None, loc="upper left")
    save(fig, "fig2-latency")
    plt.close(fig)


def fig_knee(e2, plt):
    """One panel. The billed session is the consequence and the release latency
    is just the mechanism behind it, so plotting both is the same number twice.
    """
    fig, ax = plt.subplots(figsize=(6.4, 3.9))
    lineband(ax, e2, "load_pct", "quantum_usd", set(e2["arm"]))
    ax.set_xlabel("classical utilisation (%)")
    ax.set_ylabel("billed vendor session (USD)")
    ax.set_title("Past the knee, the session is billed while the job queues")
    ax.annotate("from here the classical job cannot\nstart when the scout releases it",
                xy=(0.52, 0.72), xycoords="axes fraction", fontsize=9,
                color="#666666")
    ax.legend(title=None, loc="center left")
    save(fig, "fig3-knee")
    plt.close(fig)


def fig_waste(e3, plt):
    """Total core seconds is idle plus work times size, so plotting both is the
    same quantity twice. Keep the total, which is what a site actually pays,
    and show the distribution across trials."""
    fig, ax = plt.subplots(figsize=(6.4, 3.9))
    for arm in ORDER:
        d = e3[e3["arm"] == arm]
        if d.empty:
            continue
        g = d.groupby("size")["total_node_s"]
        mean, sd = g.mean(), g.std().fillna(0)
        color, label = ARMS[arm]
        ax.errorbar(mean.index, mean.values, yerr=sd.values, color=color,
                    marker="o", markersize=5, linewidth=1.7, capsize=3,
                    label=label)
    # below the break even the scout core costs more than the wait saves, so
    # mark it rather than let the reader assume the design always wins
    be = e3[e3["arm"] == "baseline"]["breakeven_size"].dropna()
    if not be.empty:
        x = float(be.mean())
        ax.axvline(x, color="#999999", linestyle=":", linewidth=1)
        ax.annotate(f"break even, size {x:.1f}", xy=(x, 0.55),
                    xycoords=("data", "axes fraction"), rotation=90,
                    fontsize=8, color="#666666", ha="right", va="center")
    ax.set_xlabel("allocation size (tasks)")
    ax.set_ylabel("total core seconds consumed, scout included")
    ax.set_title("Identical work, and the baseline arm consumes roughly twice the cluster")
    ax.annotate("scout core included, bars are one sd over 10 trials",
                xy=(0.97, 0.10), xycoords="axes fraction", ha="right",
                fontsize=8, color="#666666")
    ax.legend(title=None, loc="upper left")
    ax.margins(x=0.04)
    save(fig, "fig4-waste")
    plt.close(fig)


def fig_knee_size(e4, plt):
    """The mechanism, tested by collapsing it.

    Each allocation size breaks at a different utilisation, which on its own is
    just four unrelated step functions. If the boundary really is the pair not
    fitting, then plotting against spare cores rather than utilisation should
    put every size on the same curve, stepping in the same place. That is a
    test and not a restatement.
    """
    import numpy as np

    sizes = sorted(e4["size"].unique())
    cmap = plt.get_cmap("viridis")
    cos = e4[e4["arm"] == "coscheduled"]

    # ---- one, the collapse ----
    fig, ax = plt.subplots(figsize=(6.4, 3.9))
    for i, size in enumerate(sizes):
        d = cos[cos["size"] == size]
        m = d.groupby("preempt_cores")["time_to_alloc_s"].median()
        # the sizes land on top of each other, which is the result, so nudge
        # them apart to make the agreement visible
        nudge = (i - (len(sizes) - 1) / 2) * 0.05
        ax.plot(m.index + nudge, m.values, marker="o", markersize=5,
                linewidth=1.4, alpha=0.85,
                color=cmap(i / max(1, len(sizes) - 1)), label=f"{size} tasks")
    # The cliff is at slack 0, where the spare equals the request and there is
    # no core left for the scout. Measured: slack 0 costs 5.002s, exactly one
    # preempt_after. It looked free only because that cost lands in
    # vendor_wait_s rather than release_lat_s, so plot time_to_alloc_s here.
    # 0 means the pair fitted without disturbing anyone. Everything to the right
    # had to take cores from preemptible classical work, which policy allows.
    ax.axvline(0.5, color="#333333", linestyle=":", linewidth=1.2)
    ax.annotate("fits without\npreempting", xy=(0.04, 0.30),
                xycoords="axes fraction", fontsize=9, color="#B04000")
    ax.set_yscale("log")
    ax.set_xlabel("cores the pair had to take from preemption")
    ax.set_ylabel("submit to classical allocated (s), log scale")
    ax.set_title("Every allocation size breaks at the same spare core count")
    ax.legend(title="allocation", loc="center right")
    save(fig, "fig5-collapse")
    plt.close(fig)

    # ---- two, measured against predicted ----
    fig, ax = plt.subplots(figsize=(6.4, 3.9))
    measured, predicted = [], []
    for size in sizes:
        d = cos[cos["size"] == size]
        m = d.groupby("load_pct")["release_lat_s"].mean()
        broke = [l for l, v in m.items() if v > 1.0]
        measured.append(min(broke) if broke else np.nan)
        # experiment.sh computes the load as want * 100 / CORES with integer
        # division, so the predicted break has to floor the same way
        predicted.append(int((CORES - size) * 100 // CORES))
    ax.plot(sizes, predicted, color="#999999", linewidth=9, alpha=0.55,
            solid_capstyle="round",
            label="predicted break, free cores = request (pair needs one more)")
    ax.plot(sizes, measured, marker="o", markersize=8, linewidth=0,
            color=ARMS["coscheduled"][0], label="measured knee")
    for x, y in zip(sizes, measured):
        if not math.isnan(y):
            ax.annotate(f"{y:.0f}%", (x, y), textcoords="offset points",
                        xytext=(13, -4), ha="left", fontsize=9,
                        color=ARMS["coscheduled"][0])
    ax.set_xlabel("allocation size (tasks)")
    ax.set_ylabel("utilisation at the knee (%)")
    ax.set_title("So the knee moves with the size of the request")
    ax.legend(loc="upper right")
    ax.margins(x=0.06)
    save(fig, "fig6-knee-size")
    plt.close(fig)


def fig_arms(rows_e2, plt):
    """Distributions of release latency by arm, below the knee. Three discrete
    categories and the shape of each distribution is the point, which is what a
    boxplot is for."""
    below = rows_e2[(rows_e2["load_pct"] <= 93) & (rows_e2["arm"] != "baseline")]
    if below.empty:
        return
    import seaborn as sns

    present = [a for a in ORDER if a in set(below["arm"])]
    order = [ARMS[a][1] for a in present]
    palette = {ARMS[a][1]: ARMS[a][0] for a in present}
    fig, ax = plt.subplots(figsize=(4.6, 3.8))
    # x is already the category, so no hue and no dodging, otherwise the boxes
    # come out full slot width
    sns.boxplot(data=below, x="arm_label", y="release_lat_s", order=order,
                hue="arm_label", hue_order=order, palette=palette, ax=ax,
                showfliers=False, width=0.45, linewidth=0.9, legend=False)
    sns.stripplot(data=below, x="arm_label", y="release_lat_s", order=order,
                  color="#333333", ax=ax, size=2.5, alpha=0.5, jitter=0.12,
                  linewidth=0)
    ax.set_yscale("log")
    ax.set_xlabel("")
    ax.set_ylabel("submit to classical allocated (s), log scale")
    ax.set_title("Release latency by arm, below the knee")
    save(fig, "fig6-arms")
    plt.close(fig)


def main(paths):
    rows = []
    for p in paths:
        with open(p) as fh:
            rows += [derive(r) for r in csv.DictReader(fh)]
    if not rows:
        sys.exit("no rows, pass the experiment CSVs")
    # clear the directory first. Figures come and go as the story changes, and
    # a stale png sitting next to a fresh one is worse than no figure at all.
    if os.path.isdir(OUTDIR):
        for old in sorted(os.listdir(OUTDIR)):
            if old.endswith((".png", ".pdf")):
                os.remove(os.path.join(OUTDIR, old))
                print("removed stale", old)
    os.makedirs(OUTDIR, exist_ok=True)
    plt = style()

    e1, e2, e3, e4 = (frame(rows, e) for e in ("e1", "e2", "e3", "e4"))
    if e1 is not None:
        fig_held(e1, plt)
    if e2 is not None:
        # the load job cannot schedule itself at 100, so the cluster sits empty
        e2 = e2[e2["load_pct"] < 100]
        fig_knee(e2, plt)
    if e3 is not None:
        fig_waste(e3, plt)
    if e4 is not None:
        fig_knee_size(e4, plt)

    print(f"\n{len(rows)} rows from {len(paths)} files, cores={CORES}")
    if NODE_RATE == 0.0:
        print("NODE_RATE is 0, so the classical side is not priced")


if __name__ == "__main__":
    main(sys.argv[1:])
