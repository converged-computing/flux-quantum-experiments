"""Reading the sampler's JSON lines and lining the services up.

One line per device per tick. A tick is one pass over every vendor, so rows
with the same tick were taken within seconds of each other, and that is how
a machine's two views are paired. The fields each vendor gives:

    braket   normal, priority, jobs     counts, the service's queue depth
             open                       inside an execution window
             status                     ONLINE or not
    ionq     average_queue_time         the service's own average, which it
                                        reports in milliseconds. measure()
                                        turns it into seconds
             status, degraded           available or not
    ibm      pending_jobs               count
             operational, status_msg

Every row also carries raw, the whole response of the call it came from,
and elapsed_s, how long each call took. A device's full properties, which
Braket and IBM repeat every call, are kept in full in the row where they
first appeared or changed, under properties, and every row names the
version in force by its digest. Nothing a call returned is dropped.

The queue measure of each vendor, the one number the analysis uses:
braket normal, ionq average_queue_time, ibm pending_jobs.
"""

import json
import math
from collections import defaultdict

MEASURE = {"braket": "normal", "ionq": "average_queue_time", "ibm": "pending_jobs"}


def load(paths):
    rows = []
    for p in paths:
        with open(p) as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    return rows


def samples(rows):
    return [
        r for r in rows if r.get("kind", "sample") == "sample" and not r.get("error")
    ]


def errors(rows):
    return [r for r in rows if r.get("error")]


def by_device(rows):
    out = defaultdict(list)
    for r in samples(rows):
        out[(r["vendor"], r["device"])].append(r)
    for v in out.values():
        v.sort(key=lambda r: r["t"])
    return out


def measure(r):
    """The vendor's queue number for a row, or None. IonQ's average is in
    milliseconds and comes back here in seconds, so every vendor's number
    is a count or a time in seconds."""
    v = r.get(MEASURE.get(r["vendor"], ""))
    try:
        v = None if v is None else float(v)
    except (TypeError, ValueError):
        return None
    if v is not None and r["vendor"] == "ionq":
        v = v / 1000.0
    return v


def available(r):
    if r["vendor"] == "braket":
        return r.get("status") == "ONLINE" and bool(r.get("open", True))
    if r["vendor"] == "ionq":
        return r.get("status") == "available"
    if r["vendor"] == "ibm":
        # operational but in maintenance is not taking work
        msg = r.get("status_msg")
        return bool(r.get("operational")) and (msg is None or str(msg) == "active")
    return None


def paired(rows, pairs):
    """For each named machine, the ticks where both services reported it:
    [(t, braket row, ionq row)]."""
    dev = by_device(rows)
    out = {}
    for name, p in pairs.items():
        b = {r["tick"]: r for r in dev.get(("braket", p["braket"]), [])}
        i = {r["tick"]: r for r in dev.get(("ionq", p["ionq"]), [])}
        ticks = sorted(set(b) & set(i))
        out[name] = [(b[k]["t"], b[k], i[k]) for k in ticks]
    return out


def quantiles(xs, qs=(0.1, 0.25, 0.5, 0.75, 0.9, 0.99)):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return {}
    out = {}
    for q in qs:
        k = (len(xs) - 1) * q
        lo, hi = math.floor(k), math.ceil(k)
        out[str(q)] = xs[lo] + (xs[hi] - xs[lo]) * (k - lo)
    out["min"], out["max"], out["n"] = xs[0], xs[-1], len(xs)
    out["mean"] = sum(xs) / len(xs)
    return out


def ranks(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2.0 + 1
        i = j + 1
    return r


def pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / math.sqrt(sxx * syy)


def spearman(xs, ys):
    return pearson(ranks(xs), ranks(ys))


def lagged(xs, ys, lags):
    """Correlation of x against y shifted by each lag, in ticks. A positive
    lag means y follows x: y at tick k+lag is compared with x at tick k."""
    out = {}
    for lag in lags:
        if lag >= 0:
            a, b = xs[: len(xs) - lag], ys[lag:]
        else:
            a, b = xs[-lag:], ys[: len(ys) + lag]
        out[lag] = spearman(a, b) if len(a) >= 3 else None
    return out


def changes(rs, window=10):
    """How the depth moves, from the series alone.

    Per tick deltas, and the steepest sustained fall over a window of ticks.
    A fall is departures outrunning arrivals, so the steepest one is a lower
    bound on how fast the device clears tasks, in tasks per tick. Arrivals
    hide the rest, which is why it is a bound and not a rate.
    """
    xs = [measure(r) for r in rs]
    ts = [r["t"] for r in rs]
    deltas = [
        xs[k + 1] - xs[k]
        for k in range(len(xs) - 1)
        if xs[k] is not None and xs[k + 1] is not None
    ]
    if not deltas:
        return None
    best = None
    for k in range(len(xs) - window):
        a, b = xs[k], xs[k + window]
        if a is None or b is None:
            continue
        fall = (a - b) / window
        if best is None or fall > best["per_tick"]:
            best = {"per_tick": fall, "from": a, "to": b, "t": ts[k], "ticks": window}
    return {
        "n": len(deltas),
        "rising": sum(1 for d in deltas if d > 0) / len(deltas),
        "falling": sum(1 for d in deltas if d < 0) / len(deltas),
        "flat": sum(1 for d in deltas if d == 0) / len(deltas),
        "mean_abs_delta": sum(abs(d) for d in deltas) / len(deltas),
        "steepest_fall": best,
    }


def split_by_open(rs, is_open):
    """The queue measure split by whether the machine was taking work:
    {"open": [...], "closed": [...]}, so an empty queue on a shut machine
    is not mistaken for an idle one."""
    out = {"open": [], "closed": []}
    for r in rs:
        v = measure(r)
        if v is None:
            continue
        out["open" if is_open(r) else "closed"].append(v)
    return out


def hour_of(t, tz_offset_hours=0):
    import datetime

    d = datetime.datetime.fromtimestamp(t, datetime.timezone.utc)
    return (d.hour + tz_offset_hours) % 24, d.weekday()
