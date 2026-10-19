#!/usr/bin/env python3
"""Sort every change in a machine's published record by what it touched.

    ./changes.py results/queues.jsonl --out results/changes.json

Each time a record changed we compare it with the version before and count
the measured numbers that moved, leaving out timestamps. Then:

    timestamps      only dates changed, nothing measured
    pointer         only a reference to a calibration file changed
    few numbers     a handful of numbers changed, a partial re-measure
    many numbers    a large part of the record changed
    calibration     most of the machine's numbers changed at once, which
                    is what a full recalibration looks like from outside

We cannot see a calibration happen. This is the best sign the published
data gives of one.
"""

import argparse
import json
import os

# Share of measured numbers that moved for a full calibration. In the
# October run every machine's changes fell under 15% or between 30 and 45%,
# with nothing between, so the line sits in the gap.
FULL = 0.25
MANY = 20     # numbers that moved for many rather than few


def flat(d, p="", out=None):
    out = {} if out is None else out
    if isinstance(d, dict):
        for k, v in d.items():
            flat(v, p + "/" + str(k), out)
    elif isinstance(d, list):
        for i, v in enumerate(d):
            flat(v, p + "[%d]" % i, out)
    else:
        out[p] = d
    return out


def is_stamp(k):
    last = k.rsplit("/", 1)[-1].lower()
    return last == "date" or "updat" in last or last.endswith("at") and last != "format"


def kind(prev, cur):
    keys = set(prev) | set(cur)
    moved = [k for k in keys if prev.get(k) != cur.get(k) and not is_stamp(k)]
    measured = [k for k in cur if not is_stamp(k) and isinstance(cur[k], (int, float)) and not isinstance(cur[k], bool)]
    if not moved:
        return "timestamps", 0, 0.0
    if all("calibrationsref" in k.lower() or k.lower().endswith("ref") for k in moved):
        return "pointer", len(moved), 0.0
    nums = [k for k in moved if isinstance(cur.get(k), (int, float)) and not isinstance(cur.get(k), bool)]
    share = len(nums) / len(measured) if measured else 0.0
    if share >= FULL:
        return "calibration", len(moved), share
    return ("many numbers" if len(moved) >= MANY else "few numbers"), len(moved), share


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("files", nargs="+")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "results", "changes.json"))
    args = ap.parse_args(argv)
    last, out, repeat = {}, [], {}
    for path in args.files:
        with open(path) as f:
            for line in f:
                if '"properties"' not in line and '"configuration"' not in line:
                    continue
                r = json.loads(line)
                for field in ("properties", "configuration"):
                    if field not in r:
                        continue
                    key = (r["vendor"], r["device"], field)
                    cur = flat(r[field])
                    if key in last:
                        k, n, share = kind(last[key], cur)
                        row = {"vendor": r["vendor"], "device": r["device"], "record": field, "t": r["t"], "kind": k, "moved": n, "share": round(share, 3)}
                        # a service sometimes serves the old and new record
                        # in turn for a few minutes, so the same change shows
                        # up again. Keep the first.
                        prev = repeat.get(key)
                        if prev and prev["moved"] == n and prev["kind"] == k and r["t"] - prev["t"] <= 600 and n:
                            prev["t"] = r["t"]
                        else:
                            out.append(row)
                            repeat[key] = dict(row)
                    last[key] = cur
    with open(args.out, "w") as f:
        json.dump(out, f)
    from collections import Counter
    c = Counter((o["device"].split("/")[-1], o["kind"]) for o in out)
    for dev in sorted({d for d, _ in c}):
        print("  {:<20} {}".format(dev, ", ".join("{} {}".format(n, k) for (d, k), n in sorted(c.items()) if d == dev)))
    print("  {} changes to {}".format(len(out), args.out))


if __name__ == "__main__":
    main()
