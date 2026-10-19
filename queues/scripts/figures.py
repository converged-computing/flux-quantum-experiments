#!/usr/bin/env python3
"""The figures from the queue samples, as SVG, with nothing to install.

    ./figures.py results/queues.jsonl --out plots

    cal-status          a calendar of every machine: taking work, online but
                        outside its window, offline, in maintenance
    queues-timeline     every device's queue over the whole run, one panel
                        each, with the hours it was not taking work shaded
    forte1-braket-vs-ionq  Forte-1 over the four days as Braket reports it
                        and as IonQ reports it, and both ranked against
                        their own series on one axis
    forte1-agreement    Braket's count against IonQ's average, same minute
    forte1-sunday       the Sunday queue on Forte-1, hour by hour
    closed-hours        fraction of each hour the machine was not taking
                        work, by hour of day
    queue-hours         each machine's median queue by hour of day, scaled to
                        its own busiest hour
    queue-ranges        each machine's queue as p10 to p90, median and max
    empty-fraction      how often each queue was empty
    drain               the steepest sustained fall per machine, a lower
                        bound on how fast it clears tasks
    calibration         when each machine's properties changed

One y axis per panel. plot.py draws the matplotlib versions where that is
installed; these are the ones the README shows.
"""

import argparse
import datetime
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import svg  # noqa: E402
from metrics import available, by_device, changes, hour_of, load, measure, quantiles  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR = {"braket": "#D55E00", "ionq": "#0072B2", "ibm": "#009E73"}
UNIT = {"braket": "tasks waiting", "ionq": "average queue time (h)", "ibm": "pending jobs"}
# states a machine can be in, and one colour each, the same everywhere
STATE = {
    "taking work": "#009E73",
    "online, outside its window": "#56B4E9",
    "maintenance": "#E69F00",
    "offline or unavailable": "#D55E00",
    "degraded": "#CC79A7",
}
ORDER = ["braket", "ionq", "ibm"]


def short(device):
    return device.split("/")[-1]


def value(r):
    """The measure in the unit the figures show: IonQ in hours."""
    v = measure(r)
    if v is not None and r["vendor"] == "ionq":
        v = v / 3600.0
    return v


def state(r):
    v = r["vendor"]
    if v == "braket":
        if r.get("status") != "ONLINE":
            return "offline or unavailable"
        return "taking work" if r.get("open") else "online, outside its window"
    if v == "ionq":
        if r.get("status") != "available":
            return "offline or unavailable"
        return "degraded" if r.get("degraded") else "taking work"
    if v == "ibm":
        if not r.get("operational"):
            return "offline or unavailable"
        return "maintenance" if str(r.get("status_msg")) != "active" else "taking work"
    return "offline or unavailable"


def devices_in_order(dev):
    out = []
    for v in ORDER:
        out += sorted(k for k in dev if k[0] == v)
    return out


def day_ticks(t0, t1):
    """Midnights between t0 and t1, with their weekday names."""
    d = datetime.datetime.fromtimestamp(t0, datetime.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    out = []
    while d.timestamp() <= t1:
        if d.timestamp() >= t0:
            out.append((d.timestamp(), d.strftime("%a")))
        d += datetime.timedelta(days=1)
    return out


def hours_since(t, t0):
    return (t - t0) / 3600.0


# ------------------------------------------------------------------- 1 --
def cal_status(rows, out):
    dev = by_device(rows)
    keys = devices_in_order(dev)
    t0 = min(r["t"] for rs in dev.values() for r in rs)
    t1 = max(r["t"] for rs in dev.values() for r in rs)
    row_h, left, top, width = 22, 170, 40, 760
    c = svg.Canvas(left + width + 30, top + row_h * len(keys) + 90, "When each machine was taking work, 14 to 18 October 2026, UTC")
    p = c.panel(left, top, width, row_h * len(keys), (0, hours_since(t1, t0)), (0, 1))
    for i, (v, d) in enumerate(keys):
        rs = dev[(v, d)]
        # runs of one state, drawn as one cell each, so the file stays small
        start, cur = rs[0]["t"], state(rs[0])
        for a, b in zip(rs, rs[1:]):
            s = state(b)
            if s != cur:
                p.cell(hours_since(start, t0), hours_since(b["t"], t0), i, row_h, STATE[cur], title="{} {}".format(short(d), cur))
                start, cur = b["t"], s
        p.cell(hours_since(start, t0), hours_since(rs[-1]["t"], t0) + 1 / 60, i, row_h, STATE[cur], title="{} {}".format(short(d), cur))
        c.text(left - 6, top + i * row_h + row_h / 2, short(d), size=9, anchor="end", baseline="middle")
        c.text(left - 6, top + i * row_h + row_h / 2 + 9, v, size=7, anchor="end", color=svg.MUTED, baseline="middle")
    for i in range(len(keys) + 1):
        c.line(left, top + i * row_h, left + width, top + i * row_h, "white", 1.5)
    for t, name in day_ticks(t0, t1):
        x = p.px(hours_since(t, t0))
        c.line(x, top, x, top + row_h * len(keys), svg.INK, 0.8, "2,2")
        c.text(x + 3, top - 6, name + " 00:00", size=8, color=svg.INK)
    # legend, only the states that occurred
    seen = {state(r) for rs in dev.values() for r in rs}
    y = top + row_h * len(keys) + 22
    x = left
    for name, col in STATE.items():
        if name not in seen:
            continue
        c.rect(x, y - 9, 12, 12, col)
        c.text(x + 16, y, name, size=9, baseline="middle")
        x += 16 + 7 * len(name) + 24
    c.text(left, y + 24, "A sample every minute from Wed 14 Oct 19:10 to Sun 18 Oct 19:09 UTC. Braket: ONLINE and inside an execution window. IonQ: available. IBM: operational and active.", size=8, color=svg.MUTED)
    c.text(left, y + 36, "Braket says ONLINE for a machine that is between windows and will not run anything, so the light blue is as closed as the red.", size=8, color=svg.MUTED)
    return c.save(os.path.join(out, "cal-status.svg"))


# ------------------------------------------------------------------- 2 --
def queues_timeline(rows, out):
    dev = by_device(rows)
    keys = devices_in_order(dev)
    t0 = min(r["t"] for rs in dev.values() for r in rs)
    t1 = max(r["t"] for rs in dev.values() for r in rs)
    cols, pw, ph, gx, gy, left, top = 3, 250, 95, 60, 48, 70, 40
    nrows = (len(keys) + cols - 1) // cols
    c = svg.Canvas(left + cols * (pw + gx), top + nrows * (ph + gy) + 20, "Every queue over the four days")
    for i, (v, d) in enumerate(keys):
        rs = dev[(v, d)]
        xs = [hours_since(r["t"], t0) for r in rs]
        ys = [value(r) for r in rs]
        top_y = max([y for y in ys if y is not None] + [1])
        px = left + (i % cols) * (pw + gx)
        py = top + (i // cols) * (ph + gy)
        p = c.panel(px, py, pw, ph, (0, hours_since(t1, t0)), (0, top_y * 1.08))
        # hours not taking work, shaded
        start, cur = rs[0]["t"], state(rs[0]) == "taking work"
        for a, b in zip(rs, rs[1:]):
            now_up = state(b) == "taking work"
            if now_up != cur:
                if not cur:
                    c.rect(p.px(hours_since(start, t0)), py, p.px(hours_since(b["t"], t0)) - p.px(hours_since(start, t0)), ph, svg.FAINT)
                start, cur = b["t"], now_up
        if not cur:
            c.rect(p.px(hours_since(start, t0)), py, p.px(hours_since(rs[-1]["t"], t0)) - p.px(hours_since(start, t0)), ph, svg.FAINT)
        days = day_ticks(t0, t1)
        p.frame(xticks=[hours_since(t, t0) for t, _ in days], xfmt=lambda h: dict((hours_since(t, t0), n) for t, n in days).get(h, ""), yticks=svg.nice_ticks(0, top_y, 3))
        p.step(xs, ys, VENDOR[v])
        c.text(px + 4, py - 6, short(d), size=9, color=svg.INK, weight="bold")
        c.text(px + pw, py - 6, UNIT[v], size=8, anchor="end", color=svg.MUTED)
    c.text(left, top + nrows * (ph + gy) + 6, "Grey: the machine was not taking work. IonQ's number is its own average, in hours, which it updates every 15 minutes.", size=8, color=svg.MUTED)
    return c.save(os.path.join(out, "queues-timeline.svg"))


# ------------------------------------------------------------------- 3 --
def percentiles(ys):
    """Each value as the percentile of the whole series, 0 to 100, ties at
    their middle rank. The one scale a count and a time can share."""
    vals = sorted(y for y in ys if y is not None)
    n = len(vals)
    if not n:
        return [None for _ in ys]
    import bisect

    out = []
    for y in ys:
        if y is None:
            out.append(None)
            continue
        lo = bisect.bisect_left(vals, y)
        hi = bisect.bisect_right(vals, y)
        out.append(100.0 * (lo + hi) / 2.0 / n)
    return out


def forte1_braket_vs_ionq(rows, out):
    dev = by_device(rows)
    b = dev.get(("braket", "arn:aws:braket:us-east-1::device/qpu/ionq/Forte-1"), [])
    i = dev.get(("ionq", "qpu.forte-1"), [])
    if not b or not i:
        return None
    t0 = min(b[0]["t"], i[0]["t"])
    t1 = max(b[-1]["t"], i[-1]["t"])
    left, top, pw, ph, gap = 70, 44, 780, 120, 46
    c = svg.Canvas(left + pw + 40, top + 3 * (ph + gap) + 40, "Forte-1 over four days, as Braket reports it and as IonQ reports it")
    days = day_ticks(t0, t1)
    xt = [hours_since(t, t0) for t, _ in days]
    xf = lambda h: dict((hours_since(t, t0), n) for t, n in days).get(h, "")
    bx, by = [hours_since(r["t"], t0) for r in b], [measure(r) for r in b]
    ix, iy = [hours_since(r["t"], t0) for r in i], [measure(r) / 3600.0 if measure(r) is not None else None for r in i]

    p = c.panel(left, top, pw, ph, (0, hours_since(t1, t0)), (0, max(y for y in by if y is not None) * 1.1))
    p.frame(xticks=xt, xfmt=xf, ylabel="tasks waiting")
    p.step(bx, by, VENDOR["braket"])
    c.text(left + 4, top + 12, "Braket: tasks waiting in the Normal queue. A count, read every minute.", size=9)

    lo, hi = min(y for y in iy if y is not None), max(y for y in iy if y is not None)
    p2 = c.panel(left, top + ph + gap, pw, ph, (0, hours_since(t1, t0)), (0, hi * 1.1))
    p2.frame(xticks=xt, xfmt=xf, ylabel="average queue time (h)", yfmt=lambda v: "{:.0f}".format(v))
    p2.step(ix, iy, VENDOR["ionq"])
    c.text(left + 4, top + ph + gap + 12, "IonQ: average_queue_time, in hours. An average over past jobs, updated every 15 minutes. Between {:.0f} and {:.0f} hours all week.".format(lo, hi), size=9)

    p3 = c.panel(left, top + 2 * (ph + gap), pw, ph, (0, hours_since(t1, t0)), (0, 105))
    p3.frame(xticks=xt, xfmt=xf, ylabel="rank within its own series", yticks=[0, 25, 50, 75, 100], xlabel="UTC, midnights marked")
    p3.step(bx, percentiles(by), VENDOR["braket"])
    p3.step(ix, percentiles(iy), VENDOR["ionq"], 1.2)
    c.text(left + 4, top + 2 * (ph + gap) + 12, "Both on one scale. Each minute ranked against the rest of its own series, 0 the emptiest that series showed, 100 the busiest.", size=9)
    # legend
    y = top + 2 * (ph + gap) + ph + 34
    c.rect(left, y - 8, 14, 3, VENDOR["braket"])
    c.text(left + 18, y, "Braket count", size=9, baseline="middle")
    c.rect(left + 110, y - 8, 14, 3, VENDOR["ionq"])
    c.text(left + 128, y, "IonQ average", size=9, baseline="middle")
    c.text(left + 240, y, "If the two measured the same queue, the lines here would rise and fall together. They do not. Rank correlation 0.05.", size=8, color=svg.MUTED, baseline="middle")
    return c.save(os.path.join(out, "forte1-braket-vs-ionq.svg"))


def forte1_agreement(rows, out):
    from metrics import paired, spearman

    pairs = {"forte-1": {"braket": "arn:aws:braket:us-east-1::device/qpu/ionq/Forte-1", "ionq": "qpu.forte-1"}}
    trip = paired(rows, pairs).get("forte-1", [])
    pts = [(measure(b), measure(i) / 3600.0) for _, b, i in trip if measure(b) is not None and measure(i) is not None]
    if len(pts) < 3:
        return None
    rho = spearman([x for x, _ in pts], [y for _, y in pts])
    left, top, pw, ph = 80, 44, 520, 300
    c = svg.Canvas(left + pw + 40, top + ph + 80, "Forte-1: Braket's queue count against IonQ's average queue time, same minute")
    xs, ys = [x for x, _ in pts], [y for _, y in pts]
    p = c.panel(left, top, pw, ph, (-0.5, max(xs) + 1), (min(ys) - 1, max(ys) + 1))
    p.frame(xlabel="Braket: tasks waiting (one dot per minute, {} minutes)".format(len(pts)), ylabel="IonQ: average queue time (h)", yfmt=lambda v: "{:.0f}".format(v))
    # minutes pile up on the same spot, so one dot per spot, darker for more
    from collections import Counter

    spots = Counter((int(round(x)), round(y, 1)) for x, y in pts)
    top_n = max(spots.values())
    for (x, y), n in spots.items():
        c.circle(p.px(x), p.py(y), 2.4, VENDOR["ionq"], 0.15 + 0.85 * (n / top_n) ** 0.5)
    c.text(left + 4, top + 12, "rank correlation {:.2f}".format(rho), size=10, weight="bold")
    c.text(left, top + ph + 46, "Every minute of the four days is a dot, darker where minutes pile up. If both services reported the", size=8, color=svg.MUTED)
    c.text(left, top + ph + 58, "same queue the dots would climb a line. IonQ's average takes a few slowly changing values whatever Braket counts.", size=8, color=svg.MUTED)
    return c.save(os.path.join(out, "forte1-agreement.svg"))


# ------------------------------------------------------------------- 4 --
def forte1_sunday(rows, out):
    dev = by_device(rows)
    b = dev.get(("braket", "arn:aws:braket:us-east-1::device/qpu/ionq/Forte-1"), [])
    i = dev.get(("ionq", "qpu.forte-1"), [])
    if not b:
        return None
    suns = [t for t, n in day_ticks(b[0]["t"], b[-1]["t"]) if n == "Sun"]
    if not suns:
        return None
    d0 = suns[0]
    bb = [r for r in b if r["t"] >= d0]
    ii = [r for r in i if r["t"] >= d0]
    end = hours_since(bb[-1]["t"], d0)
    left, top, pw, ph = 70, 40, 760, 150
    c = svg.Canvas(left + pw + 150, top + ph + 95, "Sunday 18 October on Forte-1: the one long queue of the run, as Braket counted it")
    p = c.panel(left, top, pw, ph, (0, end), (0, max([measure(r) or 0 for r in bb] + [1]) * 1.12))
    # bands where Braket said closed, and where IonQ said unavailable
    for rs, col, row in ((bb, STATE["online, outside its window"], 0), (ii, STATE["offline or unavailable"], 1)):
        start, cur = None, False
        for r in rs:
            down = state(r) != "taking work"
            if down and start is None:
                start = r["t"]
            if not down and start is not None:
                c.rect(p.px(hours_since(start, d0)), top + ph + 4 + row * 10, p.px(hours_since(r["t"], d0)) - p.px(hours_since(start, d0)), 8, col)
                start = None
        if start is not None:
            c.rect(p.px(hours_since(start, d0)), top + ph + 4 + row * 10, p.px(hours_since(rs[-1]["t"], d0)) - p.px(hours_since(start, d0)), 8, col)
    p.frame(xticks=list(range(0, int(end) + 1, 2)), xfmt=lambda h: "{:02d}:00".format(int(h)), ylabel="tasks waiting")
    p.step([hours_since(r["t"], d0) for r in bb], [measure(r) for r in bb], VENDOR["braket"], 1.8)
    c.text(left + pw + 4, top + ph + 12, "Braket: outside window", size=7, color=svg.MUTED, baseline="middle")
    c.text(left + pw + 4, top + ph + 22, "IonQ: unavailable", size=7, color=svg.MUTED, baseline="middle")
    c.text(left + pw / 2, top + ph + 46, "hour of day, UTC", size=9, anchor="middle")
    c.text(left, top + ph + 70, "The queue built from 08:00 and stood at 9 to 19 tasks for the rest of the day. IonQ reported the machine unavailable in short spells through the morning while Braket's count kept growing.", size=8, color=svg.MUTED)
    return c.save(os.path.join(out, "forte1-sunday.svg"))


# ------------------------------------------------------------------- 5 --
def heatmap(rows, out, name, title, cell_value, legend, hue=(0, 114, 178), note=None):
    dev = by_device(rows)
    keys = devices_in_order(dev)
    left, top, cw, rh = 170, 48, 30, 22
    c = svg.Canvas(left + 24 * cw + 90, top + rh * len(keys) + 80, title)
    for i, (v, d) in enumerate(keys):
        rs = dev[(v, d)]
        by_h = defaultdict(list)
        for r in rs:
            by_h[hour_of(r["t"])[0]].append(r)
        vals = {h: cell_value(by_h[h]) for h in range(24) if by_h.get(h)}
        scale = max([x for x in vals.values() if x is not None] + [1e-9])
        for h in range(24):
            x = left + h * cw
            val = vals.get(h)
            frac = None if val is None else (val / scale if legend == "relative" else val)
            c.rect(x + 1, top + i * rh + 1, cw - 2, rh - 2, svg.sequential(frac, hue) if val is not None else "white", title="{} {:02d}:00 {}".format(short(d), h, "-" if val is None else "{:.2f}".format(val)), rx=2)
        c.text(left - 6, top + i * rh + rh / 2, short(d), size=9, anchor="end", baseline="middle")
        c.text(left - 6, top + i * rh + rh / 2 + 9, v, size=7, anchor="end", color=svg.MUTED, baseline="middle")
        if legend == "relative":
            c.text(left + 24 * cw + 6, top + i * rh + rh / 2, "max {}".format(svg.fmt_num(round(scale, 1))), size=8, baseline="middle", color=svg.INK)
    for h in range(0, 24, 3):
        c.text(left + h * cw + cw / 2, top - 6, "{:02d}".format(h), size=8, anchor="middle")
    c.text(left + 12 * cw, top + rh * len(keys) + 16, "hour of day, UTC", size=9, anchor="middle")
    # a ramp
    y = top + rh * len(keys) + 32
    for k in range(20):
        c.rect(left + k * 8, y, 8, 10, svg.sequential(k / 19, hue))
    c.text(left - 6, y + 9, "0%" if legend != "relative" else "0", size=8, anchor="end", color=svg.INK)
    c.text(left + 20 * 8 + 6, y + 9, "100% of the hour" if legend != "relative" else "the machine's busiest hour", size=8, color=svg.INK)
    if note:
        c.text(left, y + 30, note, size=8, color=svg.MUTED)
    return c.save(os.path.join(out, name + ".svg"))


def closed_hours(rows, out):
    return heatmap(
        rows, out, "closed-hours", "Fraction of each hour the machine was not taking work: white never, dark the whole hour",
        lambda rs: sum(1 for r in rs if state(r) != "taking work") / len(rs), "absolute", hue=(213, 94, 0),
        note="Each cell is four hours of one machine's life. Cepheus and Garnet close on a schedule, Forte-1 at 18:00 UTC, Garnet all weekend.",
    )


def queue_hours(rows, out):
    return heatmap(
        rows, out, "queue-hours", "Median queue by hour of day, each machine scaled to its own busiest hour",
        lambda rs: quantiles([value(r) for r in rs]).get("0.5"), "relative",
        note="Dark is that machine's busiest hour, whatever its scale; the number at the right is the busiest hour's median. Four days is too few to call this a daily pattern.",
    )


# ------------------------------------------------------------------ 5b --
def queue_ranges(rows, out):
    """p10 to p90 as a bar, the median as a tick, the maximum as a dot. One
    panel per vendor, since a count and a time do not share an axis."""
    dev = by_device(rows)
    panels = []
    for v in ORDER:
        ks = sorted(k for k in dev if k[0] == v)
        if ks:
            panels.append((v, ks))
    rh, left, pw, gap, top = 24, 170, 560, 56, 44
    height = top + sum(rh * len(ks) + gap for _, ks in panels) + 30
    c = svg.Canvas(left + pw + 110, height, "What each queue looked like while the machine was taking work, p10 to p90, the median, and the worst minute")
    y = top
    for v, ks in panels:
        # over the minutes the machine was taking work, so a shut machine's
        # idle queue does not flatten its range
        stats = {k: quantiles([value(r) for r in dev[k] if state(r) == "taking work"] or [value(r) for r in dev[k]]) for k in ks}
        xmax = max(q["max"] for q in stats.values()) * 1.12
        p = c.panel(left, y, pw, rh * len(ks), (0, xmax), (0, 1))
        for i, k in enumerate(ks):
            q = stats[k]
            yc = y + i * rh + rh / 2
            c.rect(p.px(q["0.1"]), yc - 6, max(p.px(q["0.9"]) - p.px(q["0.1"]), 1.5), 12, VENDOR[v], opacity=0.45, rx=2, title="{} p10 {} p90 {}".format(short(k[1]), svg.fmt_num(round(q["0.1"], 1)), svg.fmt_num(round(q["0.9"], 1))))
            c.rect(p.px(q["0.5"]) - 1.2, yc - 8, 2.4, 16, VENDOR[v], title="{} median {}".format(short(k[1]), svg.fmt_num(round(q["0.5"], 1))))
            c.circle(p.px(q["max"]), yc, 3.2, svg.INK, 0.9)
            c.text(left - 6, yc, short(k[1]), size=9, anchor="end", baseline="middle")
            c.text(p.px(q["max"]) + 7, yc, "max {}".format(svg.fmt_num(round(q["max"], 1))), size=8, baseline="middle", color=svg.INK)
        for t in svg.nice_ticks(0, xmax, 5):
            c.line(p.px(t), y, p.px(t), y + rh * len(ks), svg.GRID, 0.6)
            c.text(p.px(t), y + rh * len(ks) + 11, svg.fmt_num(t), size=8, anchor="middle")
        c.text(left + pw / 2, y + rh * len(ks) + 26, "{}: {}".format(v, UNIT[v]), size=9, anchor="middle")
        y += rh * len(ks) + gap
    c.rect(left, y - 6, 24, 12, VENDOR["ibm"], opacity=0.45, rx=2)
    c.text(left + 28, y, "p10 to p90", size=9, baseline="middle")
    c.rect(left + 110, y - 8, 2.4, 16, VENDOR["ibm"])
    c.text(left + 118, y, "median", size=9, baseline="middle")
    c.circle(left + 190, y, 3.2, svg.INK)
    c.text(left + 198, y, "worst minute", size=9, baseline="middle")
    c.text(left + 300, y, "A bar that is only a tick: the same value nine samples in ten, usually zero.", size=8, color=svg.MUTED, baseline="middle")
    return c.save(os.path.join(out, "queue-ranges.svg"))


# ------------------------------------------------------------------- 6 --
def bars(rows, out, name, title, valuer, xlabel, fmt, note=None):
    dev = by_device(rows)
    keys = [k for k in devices_in_order(dev) if valuer(dev[k]) is not None]
    vals = {k: valuer(dev[k]) for k in keys}
    left, top, rh, pw = 170, 44, 22, 560
    c = svg.Canvas(left + pw + 90, top + rh * len(keys) + 70, title)
    p = c.panel(left, top, pw, rh * len(keys), (0, max(vals.values()) * 1.15), (0, 1))
    for i, k in enumerate(keys):
        p.hbar(i, vals[k], VENDOR[k[0]], rh, title=short(k[1]), label=fmt(vals[k]))
        c.text(left - 6, top + i * rh + rh / 2, short(k[1]), size=9, anchor="end", baseline="middle")
        c.text(left - 6, top + i * rh + rh / 2 + 9, k[0], size=7, anchor="end", color=svg.MUTED, baseline="middle")
    for t in svg.nice_ticks(0, max(vals.values()) * 1.15, 5):
        c.line(p.px(t), top, p.px(t), top + rh * len(keys), svg.GRID, 0.6)
        c.text(p.px(t), top + rh * len(keys) + 11, fmt(t), size=8, anchor="middle")
    c.text(left + pw / 2, top + rh * len(keys) + 26, xlabel, size=9, anchor="middle")
    if note:
        c.text(left, top + rh * len(keys) + 46, note, size=8, color=svg.MUTED)
    return c.save(os.path.join(out, name + ".svg"))


def empty_fraction(rows, out):
    """Two bars per machine: how often the queue was empty while the machine
    was taking work, and while it was not. An empty queue on a shut machine
    says nothing about demand."""
    from metrics import split_by_open

    dev = by_device(rows)
    keys = [k for k in devices_in_order(dev) if k[0] != "ionq"]
    left, top, rh, pw = 170, 44, 34, 560
    c = svg.Canvas(left + pw + 230, top + rh * len(keys) + 76, "How often the queue was empty, while the machine was taking work and while it was not")
    p = c.panel(left, top, pw, rh * len(keys), (0, 1.0), (0, 1))
    for i, k in enumerate(keys):
        sp = split_by_open(dev[k], lambda r: state(r) == "taking work")
        y = top + i * rh
        for j, (name, xs, op) in enumerate((("taking work", sp["open"], 1.0), ("not taking work", sp["closed"], 0.35))):
            if not xs:
                c.text(left + 4, y + 4 + j * 14 + 9, "never " + name, size=8, color=svg.MUTED, baseline="middle")
                continue
            frac = sum(1 for x in xs if x == 0) / len(xs)
            c.rect(left, y + 4 + j * 14, frac * pw, 11, VENDOR[k[0]], opacity=op, rx=2, title="{} {}: empty {:.0%} of {} minutes".format(short(k[1]), name, frac, len(xs)))
            c.text(left + frac * pw + 4, y + 4 + j * 14 + 6, "{:.0%} of {:,} min {}".format(frac, len(xs), name), size=8, baseline="middle")
        c.text(left - 6, y + rh / 2, short(k[1]), size=9, anchor="end", baseline="middle")
        c.text(left - 6, y + rh / 2 + 9, k[0], size=7, anchor="end", color=svg.MUTED, baseline="middle")
    for t in (0, 0.25, 0.5, 0.75, 1.0):
        c.line(p.px(t), top, p.px(t), top + rh * len(keys), svg.GRID, 0.6)
        c.text(p.px(t), top + rh * len(keys) + 11, "{:.0%}".format(t), size=8, anchor="middle")
    c.text(left + pw / 2, top + rh * len(keys) + 26, "fraction of minutes with nothing waiting", size=9, anchor="middle")
    c.text(left, top + rh * len(keys) + 46, "Dark: while taking work. Light: while closed, in maintenance or offline, when a queue can only build.", size=8, color=svg.MUTED)
    c.text(left, top + rh * len(keys) + 58, "IonQ is left out, its average is never zero and is not a queue at a minute.", size=8, color=svg.MUTED)
    return c.save(os.path.join(out, "empty-fraction.svg"))


def drain(rows, out):
    def v(rs):
        c = changes(rs)
        if not c or not c["steepest_fall"] or rs[0]["vendor"] == "ionq":
            return None
        return c["steepest_fall"]["per_tick"]

    return bars(rows, out, "drain", "Steepest sustained fall over ten minutes, a lower bound on how fast the machine clears its queue", v, "tasks or jobs cleared per minute, at least", lambda x: "{:.1f}".format(x),
                note="A fall is departures outrunning arrivals, so the rate the machine actually clears tasks is at least this, and arrivals hide the rest.")


# ------------------------------------------------------------------- 7 --
CHANGE_KINDS = ["timestamps", "pointer", "few numbers", "many numbers", "calibration"]
CHANGE_LABELS = ["only dates", "new calibration file", "a few numbers", "many numbers", "full calibration"]
CHANGE_COLOR = dict(zip(CHANGE_KINDS, ["#C8CDD2", "#56B4E9", "#009E73", "#0072B2", "#D55E00"]))
KINDS = None


def load_kinds(path):
    """(vendor, device) to [(t, kind)] from changes.py, properties only."""
    if not os.path.exists(path):
        return None
    out = defaultdict(list)
    for ch in json.load(open(path)):
        if ch["record"] == "properties":
            out[(ch["vendor"], ch["device"])].append((ch["t"], ch["kind"]))
    return out


def calibration(rows, out):
    dev = by_device(rows)
    keys = [k for k in devices_in_order(dev) if any("properties" in r for r in dev[k])]
    t0 = min(r["t"] for rs in dev.values() for r in rs)
    t1 = max(r["t"] for rs in dev.values() for r in rs)
    left, top, rh, pw = 170, 40, 22, 760
    c = svg.Canvas(left + pw + 110, top + rh * len(keys) + 60, "When each machine's published numbers changed, and when it was fully recalibrated")
    p = c.panel(left, top, pw, rh * len(keys), (0, hours_since(t1, t0)), (0, 1))
    for i, k in enumerate(keys):
        ts = [r["t"] for r in dev[k] if "properties" in r]
        marks = KINDS.get(k) if KINDS else None
        if marks is None:
            marks = [(t, "many numbers") for t in ts[1:]]
        for t, kind in sorted(marks, key=lambda m: m[1] == "calibration"):
            x = p.px(hours_since(t, t0))
            big = kind == "calibration"
            c.line(x, top + i * rh + (2 if big else 5), x, top + (i + 1) * rh - (2 if big else 5), CHANGE_COLOR[kind], 2.4 if big else 1.0)
        c.text(left - 6, top + i * rh + rh / 2, short(k[1]), size=9, anchor="end", baseline="middle")
        c.text(left - 6, top + i * rh + rh / 2 + 9, k[0], size=7, anchor="end", color=svg.MUTED, baseline="middle")
        full = sum(1 for _, kd in marks if kd == "calibration")
        c.text(left + pw + 6, top + i * rh + rh / 2, "{} changes, {} full".format(len(marks), full), size=8, baseline="middle")
        c.line(left, top + (i + 1) * rh, left + pw, top + (i + 1) * rh, svg.GRID, 0.6)
    for t, name in day_ticks(t0, t1):
        x = p.px(hours_since(t, t0))
        c.line(x, top, x, top + rh * len(keys), svg.INK, 0.8, "2,2")
        c.text(x + 3, top - 6, name, size=8)
    x = left
    for kind, label in zip(CHANGE_KINDS, CHANGE_LABELS):
        c.rect(x, top + rh * len(keys) + 12, 10, 10, CHANGE_COLOR[kind])
        c.text(x + 14, top + rh * len(keys) + 21, label, size=8)
        x += 24 + 5 * len(label)
    c.text(left, top + rh * len(keys) + 38, "Red is a full calibration, most of the machine's numbers changing at once. Most changes are only new dates or a few re-measured numbers.", size=8, color=svg.MUTED)
    return c.save(os.path.join(out, "calibration.svg"))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("files", nargs="+")
    ap.add_argument("--out", default=os.path.join(HERE, "..", "plots"))
    args = ap.parse_args(argv)
    rows = load(args.files)
    os.makedirs(args.out, exist_ok=True)
    global KINDS
    KINDS = load_kinds(os.path.join(HERE, "..", "results", "changes.json"))
    for fn in (cal_status, queues_timeline, forte1_braket_vs_ionq, forte1_agreement, forte1_sunday, closed_hours, queue_hours, queue_ranges, empty_fraction, drain, calibration):
        try:
            path = fn(rows, args.out)
            print("  {}".format(path or "{}: nothing to draw".format(fn.__name__)))
        except Exception as e:  # one figure failing must not take the rest
            print("  {} failed: {!r}".format(fn.__name__, e))


if __name__ == "__main__":
    main()
