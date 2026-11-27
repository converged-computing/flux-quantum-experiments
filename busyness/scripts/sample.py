#!/usr/bin/env python3
"""Sample queue-depth plans from the measured busy-ness distributions.

The queues/ campaign watched real vendor queues once a minute for 42 days;
queues/results/distribution.json holds every observation, sorted, per
device. This script draws depth PLANS from those observations: each level
is the empirical distribution of one device scaled by a factor, each trial
is one seeded draw from it, and both arms of a trial replay the same draw.
The plans are written before anything runs and committed with the results,
so the sweep is pre-registered: the depths were chosen by the 42 days of
data and a seed, not by what made a figure look good.

Levels like x2/x5/x10 are the "at some point there WILL be contention"
axis: the same observed shape, denser. Scale 0 is the idle control.

    python3 scripts/sample.py \
        --distribution ../queues/results/distribution.json \
        --device Forte-1 --levels today:1,x2:2,x5:5,x10:10,idle:0 \
        --trials 20 --seed 20261127 --out plans/
"""

import argparse
import bisect
import json
import os
import random
import sys


def pick_device(devices, needle):
    hits = [k for k in devices if needle.lower() in k.lower()]
    if len(hits) != 1:
        raise SystemExit(
            "--device {!r} matches {} keys: {}".format(
                needle, len(hits), ", ".join(sorted(hits)) or "(none)"
            )
        )
    return hits[0]


def parse_levels(spec):
    out = []
    for part in spec.split(","):
        name, _, scale = part.partition(":")
        if not name or not scale:
            raise SystemExit("bad level {!r}; want name:scale".format(part))
        out.append((name, float(scale)))
    return out


def quantile_of(sorted_values, x):
    """The empirical quantile of x in the source distribution."""
    return bisect.bisect_right(sorted_values, x) / len(sorted_values)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--distribution", required=True)
    ap.add_argument("--device", required=True, help="substring of one device key")
    ap.add_argument("--levels", default="idle:0,today:1,x2:2,x5:5,x10:10")
    ap.add_argument("--trials", type=int, default=20)
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--out", default="plans")
    args = ap.parse_args(argv)

    dist = json.load(open(args.distribution))
    key = pick_device(dist["devices"], args.device)
    values = sorted(dist["devices"][key]["values"])
    if not values:
        raise SystemExit("device {} has no observations".format(key))
    levels = parse_levels(args.levels)

    os.makedirs(args.out, exist_ok=True)
    meta = {
        "distribution": os.path.abspath(args.distribution),
        "device": key,
        "observations": len(values),
        "seed": args.seed,
        "trials": args.trials,
        "levels": {name: scale for name, scale in levels},
        "source_quantiles": dist["devices"][key]["quantiles"],
    }
    # one RNG for the whole plan set: adding a level later changes nothing
    # already drawn only if it is appended after the existing ones
    rng = random.Random(args.seed)
    for name, scale in levels:
        path = os.path.join(args.out, "{}.csv".format(name))
        with open(path, "w") as f:
            f.write("trial,depth,source_depth,source_quantile\n")
            for t in range(1, args.trials + 1):
                base = rng.choice(values)
                depth = int(round(base * scale))
                f.write(
                    "{},{},{},{:.4f}\n".format(
                        t, depth, base, quantile_of(values, base)
                    )
                )
        print("wrote {} ({} trials, scale {})".format(path, args.trials, scale))
    with open(os.path.join(args.out, "meta.json"), "w") as f:
        json.dump(meta, f, indent=2, sort_keys=True)
        f.write("\n")
    print("wrote {}/meta.json for {}".format(args.out, key))
    return 0


if __name__ == "__main__":
    sys.exit(main())
