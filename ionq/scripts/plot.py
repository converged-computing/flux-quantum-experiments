#!/usr/bin/env python3
"""Figures from the IonQ experiment CSVs, a pdf and a png each into plots/.

    ./plot.py results/*.csv
    ./plot.py --out plots-fake results/*.csv

    fig1-anatomy    one pair on a timeline, against the plain job
    fig2-first      time to the first result, by arm, and what it is made of
    fig3-pairs      pairs submitted at once: when each got its cores, and the makespan
    fig4-circuits   an iterative workload: job time against circuits, per circuit latency
    fig5-cores      e5: core seconds held per job against the device's queue, per arm,
                    and each baseline's cost relative to the pair

Needs matplotlib. e4 is a table of checks and stays in analyze.py.
"""

import argparse
import os
import statistics
import sys
from collections import defaultdict

from metrics import iso, jobs_for, load, num

# Okabe and Ito, safe for the common colour vision deficiencies. The same
# two as the mock plots, and blue for the arm the mock did not have
COLOR = {"plain": "#D55E00", "session": "#0072B2", "coscheduled": "#009E73"}
ARMS = ("plain", "session", "coscheduled")
INK = "#333333"
MUTED = "#777777"
GRID = "#DDDDDD"


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
            "grid.color": GRID,
            "grid.linewidth": 0.6,
            "legend.frameon": False,
            "legend.fontsize": 9,
            "xtick.labelsize": 9,
            "ytick.labelsize": 9,
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
        }
    )
    return plt


def save(fig, outdir, name):
    for ext in ("pdf", "png"):
        fig.savefig(os.path.join(outdir, "%s.%s" % (name, ext)))
    print("wrote %s/%s.pdf and .png" % (outdir, name))


def live(rows, exp, arm=None):
    return [
        r
        for r in rows
        if r["exp"] == exp and not r["timedout"] and (arm is None or r["arm"] == arm)
    ]


def stats(vals):
    """median and the interquartile range, or None for nothing."""
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    q = statistics.quantiles(vals, n=4) if len(vals) > 1 else [vals[0]] * 3
    return statistics.median(vals), q[0], q[2]


def one_pair(rows, path, exp="e1"):
    """The coscheduled trial whose first-result time is the median, with its
    circuits, and the plain trial beside it. The anatomy figure draws one
    trial rather than a mean so every bar is a real interval."""
    cos = live(rows, exp, "coscheduled")
    cos = [r for r in cos if r["to_first_s"] is not None]
    if not cos:
        return None, None, []
    cos.sort(key=lambda r: r["to_first_s"])
    pair = cos[len(cos) // 2]
    plain = live(rows, exp, "plain")
    plain = sorted(
        [r for r in plain if r["to_first_s"] is not None], key=lambda r: r["to_first_s"]
    )
    plain = plain[len(plain) // 2] if plain else None
    return pair, plain, [e for e in jobs_for(path, pair) if e.get("event") == "job"]


def fig_anatomy(rows, paths, plt, outdir):
    """Every interval of one pair, from the submit, on one clock."""
    path = next((p for p in paths if p.endswith("e1.csv")), None)
    pair, plain, jobs = one_pair(rows, path)
    if pair is None:
        return
    t0 = num(pair["submit"])

    def at(key, r=pair):
        v = num(r.get(key))
        return None if v is None else v - t0

    lanes = []  # (label, [(start, end, colour, text)])
    lanes.append(
        (
            "scout",
            [
                (at("scout_start"), at("priority"), "#8FD3BE", "getting the hold"),
                (at("priority"), at("scout_finish"), COLOR["coscheduled"], "holding"),
            ],
        )
    )
    lanes.append(
        (
            "classical (held)",
            [
                (0.0, at("alloc"), "#CCCCCC", "held, no cores"),
                (at("alloc"), at("finish"), COLOR["coscheduled"], "running"),
            ],
        )
    )
    if jobs:
        j = jobs[0]
        s, d = j["submitted"] - t0, j["done"] - t0
        segs = [(s, d, "#8FD3BE", "circuit, as the job saw it")]
        vs, ve = iso(j.get("started_at")), iso(j.get("completed_at"))
        if vs is not None and ve is not None:
            segs.append((vs - t0, ve - t0, COLOR["coscheduled"], "executing"))
        lanes.append(("first circuit", segs))
    if plain is not None:
        p0 = num(plain["submit"])
        lanes.append(
            (
                "plain job",
                [
                    (
                        num(plain["alloc"]) - p0,
                        num(plain["finish"]) - p0,
                        COLOR["plain"],
                        "running",
                    )
                ],
            )
        )

    fig, ax = plt.subplots(figsize=(7.2, 0.62 * len(lanes) + 1.4))
    ys = list(range(len(lanes)))[::-1]
    span = max(
        (end for _, segs in lanes for _, end, _, _ in segs if end is not None),
        default=1.0,
    )
    for y, (label, segs) in zip(ys, lanes):
        outside = []
        for start, end, colour, text in segs:
            if start is None or end is None:
                continue
            ax.barh(
                y,
                end - start,
                left=start,
                height=0.5,
                color=colour,
                edgecolor="white",
                linewidth=1,
            )
            # a segment narrower than a fifth of the axis cannot hold its text
            if end - start > 0.2 * span:
                ax.text(
                    (start + end) / 2,
                    y,
                    text,
                    ha="center",
                    va="center",
                    fontsize=8,
                    color="white" if colour not in ("#CCCCCC", "#8FD3BE") else INK,
                )
            else:
                outside.append("%s %.1f s" % (text, end - start))
        if outside:
            right = max(end for _, end, _, _ in segs if end is not None)
            ax.text(
                right + 0.01 * span,
                y,
                ", ".join(outside),
                ha="left",
                va="center",
                fontsize=8,
                color=INK,
            )
    ax.set_yticks(ys)
    ax.set_yticklabels([l for l, _ in lanes])
    ax.set_xlabel("seconds from submit")
    ax.set_title("One pair against the plain job. The hold is all of the difference")
    ax.grid(axis="y", visible=False)
    prio = at("priority")
    if prio is not None:
        ax.axvline(prio, color=INK, linestyle=":", linewidth=1)
        ax.text(
            prio,
            max(ys) + 0.5,
            " session active, job released",
            fontsize=8,
            color=INK,
            va="bottom",
        )
    ax.set_ylim(-0.6, max(ys) + 1.0)
    ax.set_xlim(0, span * 1.45)
    save(fig, outdir, "fig1-anatomy")
    plt.close(fig)


def fig_first(rows, plt, outdir):
    """Time to the first result by arm, as the sum of its intervals, with the
    trials as dots."""
    parts = [
        ("scout_lead_s", "scout: session created, warmed up, active", "#8FD3BE"),
        ("handoff_s", "handover: release to allocation", "#666666"),
        ("startup_s", "startup: allocation to running", "#999999"),
        ("first_wait_s", "first circuit: submit to result, in the arm's colour", None),
        ("session_open_s", "own session created", "#9ECAE1"),
    ]
    tiny = []  # parts too thin to see, named under the axes instead
    data = {}
    for arm in ARMS:
        rs = live(rows, "e1", arm)
        if not rs:
            continue
        data[arm] = rs
    if not data:
        return
    fig, ax = plt.subplots(figsize=(7.0, 2.9))
    arms = [a for a in ARMS if a in data]
    ys = list(range(len(arms)))[::-1]
    seen = set()
    for y, arm in zip(ys, arms):
        rs = data[arm]
        left = 0.0
        # the plain and session arms have no scout, so their first interval
        # is submit to allocation, the time flux took
        wait = stats(
            [num(r["alloc"]) - num(r["submit"]) for r in rs if num(r.get("alloc"))]
        )
        if arm != "coscheduled" and wait and wait[0] > 0.05:
            ax.barh(y, wait[0], left=left, height=0.55, color="#999999")
            left += wait[0]
        for key, label, colour in parts:
            if key == "session_open_s" and arm != "session":
                continue
            if key == "scout_lead_s" and arm != "coscheduled":
                continue
            st = stats([r.get(key) for r in rs])
            if not st or st[0] <= 0:
                continue
            c = colour or COLOR[arm]
            lab = label if label not in seen else None
            seen.add(label)
            if st[0] < 0.1:
                tiny.append("%s %.0f ms" % (label.split(":")[0], 1000 * st[0]))
                lab = None
            ax.barh(
                y,
                st[0],
                left=left,
                height=0.55,
                color=c,
                edgecolor="white",
                linewidth=1,
                label=lab,
            )
            left += st[0]
        firsts = [r["to_first_s"] for r in rs if r["to_first_s"] is not None]
        ax.scatter(
            firsts,
            [y + 0.38] * len(firsts),
            s=14,
            color=INK,
            zorder=3,
            marker="v",
        )
        if firsts:
            ax.text(
                max(firsts) + 0.15,
                y + 0.38,
                "%.1f s" % statistics.median(firsts),
                va="center",
                fontsize=8,
                color=INK,
            )
    ax.set_yticks(ys)
    ax.set_yticklabels(arms)
    ax.set_xlabel("seconds from submit to the first circuit's result (median of trials)")
    ax.set_title("What a user waits for, and where the pair spends it")
    ax.grid(axis="y", visible=False)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.22), ncol=2, fontsize=8)
    if tiny:
        ax.text(
            0.0,
            -0.42,
            "too thin to draw: " + ", ".join(sorted(set(tiny))),
            transform=ax.transAxes,
            fontsize=8,
            color=MUTED,
        )
    ax.set_ylim(-0.6, max(ys) + 0.9)
    save(fig, outdir, "fig2-first")
    plt.close(fig)


def fig_pairs(rows, plt, outdir):
    """k pairs at once. Left, when each pair got its cores against its place
    in the batch. Right, the makespan against k, with the serial line."""
    e2 = live(rows, "e2")
    if not e2:
        return
    by = defaultdict(list)
    for r in e2:
        by[(r["batch"], r["trial"])].append(r)
    pos = defaultdict(list)  # (k, position) -> time to alloc
    span = defaultdict(list)  # k -> makespan
    created = defaultdict(list)
    for (k, _), rs in by.items():
        rs.sort(key=lambda r: num(r["submit"]))
        created[k].append(len(rs))
        for i, r in enumerate(rs, 1):
            if r["time_to_alloc_s"] is not None:
                pos[(k, i)].append(r["time_to_alloc_s"])
        subs = [num(r["submit"]) for r in rs]
        fins = [num(r["finish"]) for r in rs if num(r.get("finish"))]
        if subs and fins:
            span[k].append(max(fins) - min(subs))
    ks = sorted(span)
    single = statistics.median(span[1]) if 1 in span else None

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(9.2, 3.6))
    # one hue for one entity. The batches are told apart by marker, and
    # labelled at their last point
    markers = ["o", "s", "D", "^", "v", "P"]
    for mi, k in enumerate(ks):
        xs = [i for i in range(1, k + 1) if (k, i) in pos]
        med = [statistics.median(pos[(k, i)]) for i in xs]
        ax1.plot(
            xs,
            med,
            color=COLOR["coscheduled"],
            marker=markers[mi % len(markers)],
            markersize=5,
            linewidth=1.4,
            alpha=0.5 + 0.5 * (mi + 1) / len(ks),
        )
        if xs:
            ax1.text(
                xs[-1] + 0.15,
                med[-1],
                "%d pair%s" % (k, "" if k == 1 else "s"),
                fontsize=8,
                va="center",
                color=INK,
            )
    if single is not None and ks:
        kmax = max(ks)
        lead = statistics.median(pos[(1, 1)]) if (1, 1) in pos else 0
        ax1.plot(
            [1, kmax],
            [lead, lead + (kmax - 1) * single],
            color=MUTED,
            linestyle="--",
            linewidth=1,
            label="one device: each pair waits for the one before",
        )
        ax1.legend(loc="upper left", fontsize=8)
    ax1.set_xlabel("place in the batch, by submit order")
    ax1.set_ylabel("submit to cores allocated (s)")
    ax1.set_title("The device is held by one pair at a time")
    ax1.margins(x=0.12)

    med = [statistics.median(span[k]) for k in ks]
    lo = [min(span[k]) for k in ks]
    hi = [max(span[k]) for k in ks]
    ax2.fill_between(ks, lo, hi, color=COLOR["coscheduled"], alpha=0.2, linewidth=0)
    ax2.plot(
        ks,
        med,
        color=COLOR["coscheduled"],
        marker="o",
        markersize=5,
        linewidth=1.7,
        label="measured makespan",
    )
    if single is not None:
        ax2.plot(
            ks,
            [k * single for k in ks],
            color=MUTED,
            linestyle="--",
            linewidth=1,
            label="k × one pair",
        )
    for k, m in zip(ks, med):
        ax2.text(k, m, "  %.0f s" % m, fontsize=8, va="bottom", color=INK)
    asked = {k: statistics.mean(created[k]) for k in ks}
    if any(abs(asked[k] - k) > 0.01 for k in ks):
        ax2.text(
            0.02,
            0.95,
            "created per batch: "
            + ", ".join("%d of %d" % (round(asked[k]), k) for k in ks),
            transform=ax2.transAxes,
            fontsize=8,
            color=MUTED,
            va="top",
        )
    ax2.set_xlabel("pairs submitted at once")
    ax2.set_ylabel("first submit to last finish (s)")
    ax2.set_title("Makespan grows with the batch")
    ax2.set_xticks(ks)
    ax2.legend(loc="upper left", fontsize=8)
    ax2.margins(x=0.08)
    fig.tight_layout()
    save(fig, outdir, "fig3-pairs")
    plt.close(fig)


def fig_circuits(rows, plt, outdir):
    """Left, the job's time from submit to finish against circuits per job,
    per arm. The hold is a fixed cost, so the lines are parallel. Right, the
    per circuit latency per arm where the workload's lines were kept."""
    e3 = live(rows, "e3")
    if not e3:
        return
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(9.2, 3.6))
    iters = sorted({r["iters"] for r in e3})
    ends = []  # (x, y, arm) of each line's last point, for shared labels
    for arm in ARMS:
        pts = []
        for it in iters:
            rs = [r for r in e3 if r["arm"] == arm and r["iters"] == it]
            st = stats(
                [
                    num(r["finish"]) - num(r["submit"])
                    for r in rs
                    if num(r.get("finish")) and num(r.get("submit"))
                ]
            )
            if st:
                pts.append((it, st))
        if not pts:
            continue
        xs = [p[0] for p in pts]
        ends.append((xs[-1], pts[-1][1][0], arm))
        ax1.fill_between(
            xs,
            [p[1][1] for p in pts],
            [p[1][2] for p in pts],
            color=COLOR[arm],
            alpha=0.2,
            linewidth=0,
        )
        ax1.plot(
            xs,
            [p[1][0] for p in pts],
            color=COLOR[arm],
            marker="o",
            markersize=4,
            linewidth=1.7,
            label=arm,
        )
    # lines that end together get one label between them
    if ends:
        top = max(y for _, y, _ in ends)
        groups = []
        for x, y, arm in sorted(ends, key=lambda e: e[1]):
            if groups and abs(groups[-1][1] - y) < 0.04 * top:
                groups[-1][2].append(arm)
            else:
                groups.append([x, y, [arm]])
        for x, y, arms in groups:
            ax1.text(
                x,
                y,
                "  " + ", ".join(arms),
                fontsize=8,
                va="center",
                color=COLOR[arms[0]] if len(arms) == 1 else INK,
                clip_on=True,
            )
    ax1.set_xlabel("circuits per job")
    ax1.set_ylabel("submit to job finished (s)")
    ax1.set_title("The hold is a fixed cost per job")
    ax1.set_xticks(iters)
    ax1.margins(x=0.18)
    ax1.legend(loc="upper left", fontsize=8)

    # per circuit latency, every circuit of every trial, per arm and count
    width = 0.25
    offsets = {arm: (i - 1) * width for i, arm in enumerate(ARMS)}
    have = False
    top = 0.0
    for arm in ARMS:
        xs, ys = [], []
        for xi, it in enumerate(iters):
            rs = [r for r in e3 if r["arm"] == arm and r["iters"] == it]
            vals = [r["circuit_s"] for r in rs if r.get("circuit_s") is not None]
            for v in vals:
                xs.append(xi + offsets[arm])
                ys.append(v)
        if xs:
            have = True
            top = max(top, max(ys))
            ax2.scatter(xs, ys, s=16, color=COLOR[arm], alpha=0.8, label=arm, zorder=3)
    ax2.set_xticks(range(len(iters)))
    ax2.set_xticklabels([str(i) for i in iters])
    ax2.set_xlabel("circuits per job")
    ax2.set_ylabel("median circuit latency in the job (s)")
    ax2.set_title("Per circuit, inside or outside a session")
    if have:
        ax2.legend(loc="lower right", fontsize=8)
        # from zero, or a few milliseconds of noise look like a difference
        ax2.set_ylim(0, top * 1.3)
    else:
        ax2.text(
            0.5,
            0.5,
            "no per circuit records",
            transform=ax2.transAxes,
            ha="center",
            color=MUTED,
        )
    missing = sorted(
        {
            (r["arm"], r["iters"])
            for r in e3
            if r.get("circuit_s") is None
        }
    )
    if missing and have:
        counts = sorted({it for _, it in missing})
        ax2.text(
            0.02,
            0.04,
            "no per circuit records at %s circuits" % ", ".join(map(str, counts)),
            transform=ax2.transAxes,
            fontsize=8,
            color=MUTED,
        )
    fig.tight_layout()
    save(fig, outdir, "fig4-circuits")
    plt.close(fig)


def fig_cores(rows, plt, outdir):
    """Core seconds per job against the device's queue. A plain job holds
    its cores through the queue for every circuit, a session job for its
    first, the pair for none, and the scout's one core is what the pair
    pays instead."""
    e5 = [r for r in live(rows, "e5") if num(r.get("queue")) is not None]
    if not e5:
        return
    qs = sorted({num(r["queue"]) for r in e5})
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(9.2, 3.6))

    def series(arm, key):
        pts = []
        for q in qs:
            st = stats(
                [r.get(key) for r in e5 if r["arm"] == arm and num(r["queue"]) == q]
            )
            if st:
                pts.append((q, st))
        return pts

    for arm in ARMS:
        pts = series(arm, "held_core_s")
        if not pts:
            continue
        xs = [p[0] for p in pts]
        ax1.fill_between(
            xs,
            [p[1][1] for p in pts],
            [p[1][2] for p in pts],
            color=COLOR[arm],
            alpha=0.2,
            linewidth=0,
        )
        ax1.plot(
            xs,
            [p[1][0] for p in pts],
            color=COLOR[arm],
            marker="o",
            markersize=4,
            linewidth=1.7,
            label=arm + ", classical cores",
        )
    pair = series("coscheduled", "pair_core_s")
    if pair:
        ax1.plot(
            [p[0] for p in pair],
            [p[1][0] for p in pair],
            color=COLOR["coscheduled"],
            linestyle="--",
            linewidth=1.2,
            label="coscheduled, with the scout's core",
        )
    ax1.set_xlabel("device queue (s per circuit)")
    ax1.set_ylabel("core seconds per job")
    ax1.set_title("What the classical side holds through the queue")
    ax1.set_xticks(qs)
    ax1.legend(loc="upper left", fontsize=8)
    ax1.margins(x=0.05)

    # the pair's whole cost, scout included, as the unit. Above the line a
    # baseline costs more classical core seconds than coscheduling does
    unit = {p[0]: p[1][0] for p in pair}
    for arm in ("plain", "session"):
        pts = []
        for q in qs:
            if q not in unit or not unit[q]:
                continue
            st = stats(
                [
                    r["held_core_s"] / unit[q]
                    for r in e5
                    if r["arm"] == arm
                    and num(r["queue"]) == q
                    and r.get("held_core_s") is not None
                ]
            )
            if st:
                pts.append((q, st))
        if not pts:
            continue
        xs = [p[0] for p in pts]
        ax2.fill_between(
            xs,
            [p[1][1] for p in pts],
            [p[1][2] for p in pts],
            color=COLOR[arm],
            alpha=0.2,
            linewidth=0,
        )
        ax2.plot(
            xs,
            [p[1][0] for p in pts],
            color=COLOR[arm],
            marker="o",
            markersize=4,
            linewidth=1.7,
            label=arm,
        )
        ax2.text(
            xs[-1],
            pts[-1][1][0],
            "  %s, %.1f×" % (arm, pts[-1][1][0]),
            fontsize=8,
            va="center",
            color=COLOR[arm],
            clip_on=True,
        )
    ax2.axhline(1.0, color=INK, linestyle=":", linewidth=1)
    ax2.text(
        qs[0] if qs else 0,
        1.0,
        " the pair, scout included",
        fontsize=8,
        color=INK,
        va="bottom",
    )
    ax2.set_xlabel("device queue (s per circuit)")
    ax2.set_ylabel("core seconds, relative to the pair")
    ax2.set_title("What a baseline costs against the pair")
    ax2.set_xticks(qs)
    ax2.set_ylim(bottom=0)
    ax2.margins(x=0.2)
    fig.tight_layout()
    save(fig, outdir, "fig5-cores")
    plt.close(fig)


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+", help="the experiment CSVs")
    ap.add_argument("--out", default="plots", help="directory for the figures")
    args = ap.parse_args(argv)
    paths = [p for p in args.paths if not p.endswith("e4.csv")]
    rows = load(paths)
    if not rows:
        sys.exit("no rows, pass the experiment CSVs")
    # a stale figure next to a fresh one is worse than none
    if os.path.isdir(args.out):
        for old in sorted(os.listdir(args.out)):
            if old.startswith("fig") and old.endswith((".png", ".pdf")):
                os.remove(os.path.join(args.out, old))
    os.makedirs(args.out, exist_ok=True)
    plt = style()
    fig_anatomy(rows, paths, plt, args.out)
    fig_first(rows, plt, args.out)
    fig_pairs(rows, plt, args.out)
    fig_circuits(rows, plt, args.out)
    fig_cores(rows, plt, args.out)
    print("\n%d rows from %d files" % (len(rows), len(paths)))


if __name__ == "__main__":
    main(sys.argv[1:])
