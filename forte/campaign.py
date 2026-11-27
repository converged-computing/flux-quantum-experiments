#!/usr/bin/env python3
"""Run a grid of trials, resumably, with the total cost shown first.

    campaign.py campaign.json           # estimate, confirm, run what is missing
    campaign.py campaign.json --dry     # the estimate only

The campaign file names the cells and how many trials each should have:

    {
      "paths": ["braket", "ionq"],
      "arms": ["baseline", "coschedule"],
      "fillers": [0, 3],
      "trials": 2,
      "tasks": 3, "shots": 100, "filler_shots": 100,
      "max_seconds": 1800, "pause_s": 120,
      "hold": {"braket": "session", "ionq": "probe"},
      "device": {"braket": null, "ionq": null}
    }

Trials already in runs.jsonl count, so a campaign that stopped can be run
again and only does what is left. Cells run baseline then coschedule, one
path at a time, with a pause between trials so one trial's fillers are gone
before the next begins.
"""

import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import report  # noqa: E402

TRIAL = os.path.join(HERE, "trial.py")


def planned(spec):
    for path in spec["paths"]:
        for fillers in spec.get("fillers", [0]):
            for arm in spec["arms"]:
                hold = (
                    (spec.get("hold") or {}).get(path) if arm == "coschedule" else None
                )
                if arm == "coschedule" and hold is None:
                    hold = "session" if path == "braket" else "probe"
                yield (path, arm, hold or "-", int(fillers))


def have(record):
    if not os.path.exists(record):
        return {}
    counts = {}
    for r in report.load(record):
        counts[report.key(r)] = counts.get(report.key(r), 0) + 1
    return counts


def trial_args(spec, cell, record):
    path, arm, hold, fillers = cell
    args = [
        "--path",
        path,
        "--arm",
        arm,
        "--tasks",
        str(spec.get("tasks", 3)),
        "--shots",
        str(spec.get("shots", 100)),
        "--fillers",
        str(fillers),
        "--filler-shots",
        str(spec.get("filler_shots", 100)),
        "--max-seconds",
        str(spec.get("max_seconds", 1800)),
        "--record",
        record,
    ]
    if arm == "coschedule":
        args += ["--hold", hold]
    device = (spec.get("device") or {}).get(path)
    if device:
        args += ["--device", device]
    if spec.get("allow_ionq_session"):
        args.append("--allow-ionq-session")
    return args


def estimate(spec, cell, record):
    out = subprocess.run(
        [sys.executable, TRIAL, "estimate"] + trial_args(spec, cell, record),
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        return None, out.stderr.strip() or out.stdout.strip()
    for line in out.stdout.splitlines():
        if line.strip().startswith("total"):
            try:
                return float(line.split("$")[1].split()[0]), None
            except (IndexError, ValueError):
                pass
    return None, out.stdout.strip()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("campaign")
    ap.add_argument("--record", default=os.path.join(HERE, "runs.jsonl"))
    ap.add_argument("--dry", action="store_true", help="estimate and stop")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args(argv)
    with open(args.campaign) as f:
        spec = json.load(f)
    trials = int(spec.get("trials", 1))
    done = have(args.record)

    todo, total = [], 0.0
    print(
        "{:<7} {:<11} {:<8} {:>7} {:>5} {:>5} {:>10}".format(
            "path", "arm", "hold", "fillers", "have", "need", "each"
        )
    )
    for cell in planned(spec):
        need = max(0, trials - done.get(cell, 0))
        cost, err = estimate(spec, cell, args.record)
        print(
            "{:<7} {:<11} {:<8} {:>7} {:>5} {:>5} {:>10}".format(
                *cell,
                done.get(cell, 0),
                need,
                "${:.2f}".format(cost) if cost is not None else "?",
            )
        )
        if err:
            print("    {}".format(err.splitlines()[-1]))
        if cost is not None:
            total += need * cost
        todo += [cell] * need
    print("{} trials to run, about ${:.2f}".format(len(todo), total))
    if args.dry or not todo:
        return
    if not args.yes:
        if input("continue? [y/N] ").strip().lower() not in ("y", "yes"):
            return

    pause = float(spec.get("pause_s", 120))
    for n, cell in enumerate(todo):
        print(
            "\n==== trial {} of {}: {} ====".format(
                n + 1, len(todo), " ".join(map(str, cell))
            ),
            flush=True,
        )
        rc = subprocess.call(
            [
                sys.executable,
                TRIAL,
                "run",
                "--yes",
                "--max-usd",
                str(spec.get("max_usd_per_trial", 40)),
            ]
            + trial_args(spec, cell, args.record)
        )
        if rc != 0:
            print("trial failed with rc={}, stopping. Run again to resume".format(rc))
            sys.exit(rc)
        if n + 1 < len(todo):
            print("pausing {:.0f}s".format(pause), flush=True)
            time.sleep(pause)
    print()
    report.main(["--record", args.record])


if __name__ == "__main__":
    main()
