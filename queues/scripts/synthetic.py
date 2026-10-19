#!/usr/bin/env python3
"""A made up weekend of samples, so the analysis and the figures can be run
before the real ones are in.

    synthetic.py --out results-synthetic/queues.jsonl --days 3

The shape is a guess at what a real device does: a queue that swells in
US daytime and empties at night, Braket's count and IonQ's average lagging
each other by a few minutes, and windows the device is shut. Nothing here is a
measurement.
"""

import argparse
import json
import math
import os
import random

PAIRS = {
    "forte-1": ("arn:aws:braket:us-east-1::device/qpu/ionq/Forte-1", "qpu.forte-1"),
    "aria-1": ("arn:aws:braket:us-east-1::device/qpu/ionq/Aria-1", "qpu.aria-1"),
}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="results-synthetic/queues.jsonl")
    ap.add_argument("--days", type=float, default=3)
    ap.add_argument("--interval", type=float, default=60)
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args(argv)
    rng = random.Random(args.seed)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    t0 = 1760572800.0  # a Thursday, 2026-10-16 00:00 UTC
    n = int(args.days * 86400 / args.interval)
    service = {"forte-1": 240.0, "aria-1": 90.0}  # seconds per task ahead
    lag = {"forte-1": 3, "aria-1": 1}  # ticks IonQ's average trails the count
    hist = {k: [] for k in PAIRS}
    with open(args.out, "w") as f:
        for tick in range(1, n + 1):
            t = t0 + tick * args.interval
            hour = (t % 86400) / 3600.0
            day = int((t - t0) // 86400)
            for name, (arn, ionq) in PAIRS.items():
                # daytime swell, with a weekend lull and noise
                base = 4 + 6 * max(0.0, math.sin((hour - 6) / 12 * math.pi))
                if day in (2, 3):
                    base *= 0.5
                depth = max(0, int(rng.gauss(base, 2)))
                shut = 2 <= hour < 4
                hist[name].append(depth)
                lagged = (
                    hist[name][-1 - lag[name]] if len(hist[name]) > lag[name] else depth
                )
                f.write(
                    json.dumps(
                        {
                            "kind": "sample",
                            "vendor": "braket",
                            "device": arn,
                            "tick": tick,
                            "t": t,
                            "normal": depth,
                            "priority": rng.choice([0, 0, 0, 1]),
                            "jobs": 0,
                            "status": "ONLINE",
                            "open": not shut,
                        }
                    )
                    + "\n"
                )
                f.write(
                    json.dumps(
                        {
                            "kind": "sample",
                            "vendor": "ionq",
                            "device": ionq,
                            "tick": tick,
                            "t": t,
                            "status": "unavailable" if shut else "available",
                            "degraded": False,
                            "average_queue_time": 1000.0
                            * lagged
                            * service[name]
                            * rng.uniform(0.8, 1.2),
                        }
                    )
                    + "\n"
                )
            f.write(
                json.dumps(
                    {
                        "kind": "sample",
                        "vendor": "ibm",
                        "device": "ibm_kingston",
                        "tick": tick,
                        "t": t,
                        "pending_jobs": max(
                            0,
                            int(
                                rng.gauss(
                                    30 + 20 * math.sin(hour / 24 * 2 * math.pi), 8
                                )
                            ),
                        ),
                        "operational": True,
                        "status_msg": "active",
                    }
                )
                + "\n"
            )
    print("{} ticks over {} days to {}".format(n, args.days, args.out))


if __name__ == "__main__":
    main()
