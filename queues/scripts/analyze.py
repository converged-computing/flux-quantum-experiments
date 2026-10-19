#!/usr/bin/env python3
"""Tables from the queue samples, and the distribution the mock will draw from.

    ./analyze.py results/queues.jsonl
    ./analyze.py results/queues.jsonl --tz -7          hours in local time
    ./analyze.py results/queues.jsonl --distribution results/distribution.json

Three questions.

    What does each device's queue look like over the days sampled: the
    quantiles, how often the device was taking work, and the median by hour
    of day and by weekday, which is when to run an experiment.

    Do Braket and IonQ report the same machine the same way: for each paired
    machine, the rank correlation of Braket's count against IonQ's average
    at the same tick and at lags either way, and how often they agree the
    machine is up.

    How the queue moves, from the series alone: how often it rises, falls
    and sits still, and the steepest sustained fall, which is a lower bound
    on how fast the device clears tasks. What the mock should wait comes
    from the quantiles, by hour, in the distribution it writes.
"""

import argparse
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from metrics import (  # noqa: E402
    changes,
    MEASURE,
    available,
    by_device,
    errors,
    hour_of,
    lagged,
    load,
    measure,
    paired,
    quantiles,
    spearman,
)

HERE = os.path.dirname(os.path.abspath(__file__))


def fmt(v, d=0):
    if v is None:
        return "-"
    return "{:.{}f}".format(v, d)


def device_table(rows, tz):
    out = [
        "== each device ==",
        "{:<7} {:<30} {:>5} {:>6} {:>5} {:>7} {:>7} {:>7} {:>7} {:>7}".format(
            "vendor", "device", "n", "days", "up", "p10", "p50", "p90", "max", "unit"
        ),
    ]
    units = {"braket": "tasks", "ionq": "s", "ibm": "jobs"}
    for (vendor, device), rs in sorted(by_device(rows).items()):
        xs = [measure(r) for r in rs]
        q = quantiles(xs)
        ups = [available(r) for r in rs]
        up = sum(1 for u in ups if u) / len(ups) if ups else None
        span = (rs[-1]["t"] - rs[0]["t"]) / 86400 if len(rs) > 1 else 0
        out.append(
            "{:<7} {:<30} {:>5} {:>6.2f} {:>5} {:>7} {:>7} {:>7} {:>7} {:>7}".format(
                vendor,
                device.split("/")[-1][:30],
                len(rs),
                span,
                fmt(up * 100 if up is not None else None) + "%",
                fmt(q.get("0.1")),
                fmt(q.get("0.5")),
                fmt(q.get("0.9")),
                fmt(q.get("max")),
                units.get(vendor, ""),
            )
        )
    return "\n".join(out)


def hourly(rows, tz):
    out = ["== median by hour of day (tz {:+d}) ==".format(tz)]
    for (vendor, device), rs in sorted(by_device(rows).items()):
        by_h = defaultdict(list)
        for r in rs:
            h, _ = hour_of(r["t"], tz)
            by_h[h].append(measure(r))
        line = "{:<7} {:<22}".format(vendor, device.split("/")[-1][:22])
        line += " ".join(
            (
                "{:>4}".format(fmt(quantiles(by_h[h]).get("0.5")))
                if by_h.get(h)
                else "   -"
            )
            for h in range(24)
        )
        out.append(line)
    out.append("{:<30}".format("hour") + " ".join("{:>4}".format(h) for h in range(24)))
    return "\n".join(out)


def weekday(rows, tz):
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    out = [
        "== median by weekday ==",
        "{:<7} {:<22} ".format("vendor", "device")
        + " ".join("{:>5}".format(n) for n in names),
    ]
    for (vendor, device), rs in sorted(by_device(rows).items()):
        by_d = defaultdict(list)
        for r in rs:
            _, d = hour_of(r["t"], tz)
            by_d[d].append(measure(r))
        out.append(
            "{:<7} {:<22} ".format(vendor, device.split("/")[-1][:22])
            + " ".join(
                (
                    "{:>5}".format(fmt(quantiles(by_d[d]).get("0.5")))
                    if by_d.get(d)
                    else "    -"
                )
                for d in range(7)
            )
        )
    return "\n".join(out)


def agreement(rows, pairs, interval):
    out = ["== the same machine through Braket and through IonQ =="]
    for name, trip in paired(rows, pairs).items():
        if len(trip) < 3:
            out.append("{}: {} paired ticks, not enough".format(name, len(trip)))
            continue
        bx = [measure(b) for _, b, _ in trip]
        iy = [measure(i) for _, _, i in trip]
        keep = [k for k in range(len(trip)) if bx[k] is not None and iy[k] is not None]
        bx, iy = [bx[k] for k in keep], [iy[k] for k in keep]
        rho = spearman(bx, iy)
        both_up = sum(1 for _, b, i in trip if available(b) and available(i))
        either = sum(1 for _, b, i in trip if available(b) or available(i))
        lags = lagged(bx, iy, [-10, -5, -3, -1, 0, 1, 3, 5, 10])
        scored = [(v, k) for k, v in lags.items() if v is not None]
        if not scored:
            out.append(
                "{}: {} paired ticks, but one side never varies, so no rank correlation".format(
                    name, len(trip)
                )
            )
            continue
        best = max(scored)
        out.append(
            "{}: {} paired ticks over {:.1f} days".format(
                name, len(trip), (trip[-1][0] - trip[0][0]) / 86400
            )
        )
        out.append(
            "    rank correlation of Braket's count with IonQ's average at the same tick: {}".format(
                fmt(rho, 2)
            )
        )
        out.append(
            "    strongest at lag {:+d} ticks ({:+.0f} min): {} (positive lag, IonQ trails Braket)".format(
                best[1], best[1] * interval / 60, fmt(best[0], 2)
            )
        )
        out.append(
            "    both say up on {:.0%} of ticks where either does".format(
                both_up / either if either else 0
            )
        )
        zb = sum(1 for x in bx if x == 0) / len(bx) if bx else 0
        zi = sum(1 for y in iy if y == 0) / len(iy) if iy else 0
        out.append(
            "    empty queue: Braket {:.0%} of ticks, IonQ {:.0%}".format(zb, zi)
        )
    return "\n".join(out)


def open_closed(rows):
    """The queue while the machine was taking work against while it was
    not. An empty queue on a shut machine is not an idle machine, and a
    queue that builds while it is shut is waiting for it to open."""
    from metrics import split_by_open

    out = [
        "== the queue while taking work, against while not ==",
        "{:<7} {:<22} {:>7} {:>9} {:>9} {:>8} {:>8} {:>8}".format(
            "vendor", "device", "up", "empty|up", "empty|dn", "mean|up", "mean|dn", "p90|up"
        ),
    ]
    for (vendor, device), rs in sorted(by_device(rows).items()):
        if vendor == "ionq":
            continue
        sp = split_by_open(rs, available)
        up, dn = sp["open"], sp["closed"]
        if not up:
            continue
        out.append(
            "{:<7} {:<22} {:>6.0%} {:>8.0%} {:>9} {:>8.1f} {:>8} {:>8}".format(
                vendor,
                device.split("/")[-1][:22],
                len(up) / (len(up) + len(dn)),
                sum(1 for x in up if x == 0) / len(up),
                "{:.0%}".format(sum(1 for x in dn if x == 0) / len(dn)) if dn else "-",
                sum(up) / len(up),
                "{:.1f}".format(sum(dn) / len(dn)) if dn else "-",
                fmt(quantiles(up).get("0.9")),
            )
        )
    out.append("up: taking work. IonQ is left out, its average is not a queue at a minute.")
    return "\n".join(out)


def movement(rows, interval):
    """How the depth moves, from the series alone."""
    out = [
        "== how the queue moves ==",
        "{:<7} {:<22} {:>7} {:>7} {:>7} {:>9} {:>22}".format(
            "vendor", "device", "rising", "falling", "flat", "mean|d|", "steepest fall"
        ),
    ]
    for (vendor, device), rs in sorted(by_device(rows).items()):
        c = changes(rs)
        if not c:
            continue
        best = c["steepest_fall"]
        fall = (
            "{:.2f}/min ({} to {})".format(
                best["per_tick"] * 60.0 / interval, fmt(best["from"]), fmt(best["to"])
            )
            if best
            else "-"
        )
        out.append(
            "{:<7} {:<22} {:>6.0%} {:>7.0%} {:>7.0%} {:>9.2f} {:>22}".format(
                vendor,
                device.split("/")[-1][:22],
                c["rising"],
                c["falling"],
                c["flat"],
                c["mean_abs_delta"],
                fall,
            )
        )
    out.append(
        "a fall is departures outrunning arrivals, so the steepest fall is a lower bound on the\n"
        "rate the device clears its queue. Arrivals hide the rest."
    )
    return "\n".join(out)


def distribution(rows, tz):
    """Per device: quantiles of the measure overall and by hour, for the mock
    and for choosing when to run."""
    out = {"measure": MEASURE, "devices": {}}
    for (vendor, device), rs in by_device(rows).items():
        xs = [measure(r) for r in rs]
        by_h = defaultdict(list)
        for r in rs:
            h, _ = hour_of(r["t"], tz)
            by_h[h].append(measure(r))
        out["devices"]["{}:{}".format(vendor, device)] = {
            "quantiles": quantiles(xs),
            "values": sorted(x for x in xs if x is not None),
            "by_hour": {str(h): quantiles(v) for h, v in by_h.items()},
            "availability": sum(1 for r in rs if available(r)) / len(rs),
            "movement": changes(rs),
        }
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("files", nargs="+")
    ap.add_argument("--devices", default=os.path.join(HERE, "..", "devices.json"))
    ap.add_argument(
        "--tz", type=int, default=0, help="hours from UTC for the hour of day table"
    )
    ap.add_argument(
        "--interval",
        type=float,
        default=60.0,
        help="the sampler's interval, for lags in minutes",
    )
    ap.add_argument(
        "--distribution", help="write the per device distribution here, as JSON"
    )
    args = ap.parse_args(argv)
    rows = load(args.files)
    with open(args.devices) as f:
        pairs = json.load(f).get("pairs", {})
    print(
        "{} rows, {} samples, {} errors".format(
            len(rows),
            len([r for r in rows if r.get("kind", "sample") == "sample"]),
            len(errors(rows)),
        )
    )
    print()
    print(device_table(rows, args.tz))
    print()
    print(hourly(rows, args.tz))
    print()
    print(weekday(rows, args.tz))
    print()
    print(agreement(rows, pairs, args.interval))
    print()
    print(open_closed(rows))
    print()
    print(movement(rows, args.interval))
    if args.distribution:
        with open(args.distribution, "w") as f:
            json.dump(distribution(rows, args.tz), f)
        print("\ndistribution written to {}".format(args.distribution))


if __name__ == "__main__":
    main()
