#!/usr/bin/env python3
"""Read runs.jsonl and say what coscheduling bought, per path.

    report.py                   # the table
    report.py --json            # the same numbers as JSON
    report.py --record other.jsonl

Cells are path x arm x hold x fillers. For each: how long the classical's
cores were held, how long the device was waited for with the cores free,
how long each task waited and turned around, how many were served inside
the hold, and what it cost. Then baseline against coschedule for each path
and contention level, which is the comparison the experiment is for.
"""

import argparse
import json
import os
import statistics

RECORD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runs.jsonl")


def load(where):
    rows = []
    with open(where) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def key(r):
    c = r["cell"]
    return (c["path"], c["arm"], c.get("hold") or "-", int(c.get("fillers") or 0))


def mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.mean(xs) if xs else None


def cells(rows):
    out = {}
    for r in rows:
        s = r.get("summary") or {}
        c = out.setdefault(
            key(r),
            {
                "n": 0,
                "cores_held_s": [],
                "device_wait_s": [],
                "wait_s": [],
                "turnaround_s": [],
                "makespan_s": [],
                "in_hold": [],
                "done": [],
                "charged": [],
                "estimated": [],
                "hold_usd": [],
            },
        )
        c["n"] += 1
        c["cores_held_s"].append(s.get("cores_held_s"))
        c["device_wait_s"].append(s.get("device_wait_s"))
        c["wait_s"].append(s.get("mean_wait_s"))
        c["turnaround_s"].append(s.get("mean_turnaround_s"))
        c["makespan_s"].append(s.get("makespan_s"))
        tasks = r.get("tasks") or []
        c["in_hold"].append(
            (s.get("tasks_in_hold") or 0) / len(tasks) if tasks else None
        )
        c["done"].append((s.get("tasks_done") or 0) / len(tasks) if tasks else None)
        cost = r.get("cost") or {}
        c["charged"].append(cost.get("charged_usd"))
        c["estimated"].append(cost.get("estimated_usd"))
        c["hold_usd"].append(cost.get("hold_usd"))
    summary = {}
    for k, c in out.items():
        summary[k] = {
            "n": c["n"],
            "cores_held_s": mean(c["cores_held_s"]),
            "device_wait_s": mean(c["device_wait_s"]),
            "task_wait_s": mean(c["wait_s"]),
            "task_turnaround_s": mean(c["turnaround_s"]),
            "makespan_s": mean(c["makespan_s"]),
            "served_in_hold": mean(c["in_hold"]),
            "completed": mean(c["done"]),
            "charged_usd": mean(c["charged"]),
            "estimated_usd": mean(c["estimated"]),
            "hold_usd": mean(c["hold_usd"]),
        }
    return summary


def fmt(v, unit=""):
    if v is None:
        return "-"
    if isinstance(v, float):
        return "{:.0f}{}".format(v, unit) if unit == "s" else "{:.2f}{}".format(v, unit)
    return str(v)


def table(summary):
    head = "{:<7} {:<11} {:<8} {:>7} {:>3} {:>10} {:>10} {:>9} {:>9} {:>8} {:>8} {:>9}".format(
        "path",
        "arm",
        "hold",
        "fillers",
        "n",
        "cores held",
        "dev wait",
        "task wait",
        "turnarnd",
        "in hold",
        "done",
        "cost",
    )
    lines = [head, "-" * len(head)]
    for k in sorted(summary):
        s = summary[k]
        cost = s["charged_usd"] if s["charged_usd"] is not None else s["estimated_usd"]
        cost_s = ("$" + fmt(cost)) if cost is not None else "-"
        if s["charged_usd"] is None and cost is not None:
            cost_s += "*"
        lines.append(
            "{:<7} {:<11} {:<8} {:>7} {:>3} {:>10} {:>10} {:>9} {:>9} {:>8} {:>8} {:>9}".format(
                k[0],
                k[1],
                k[2],
                k[3],
                s["n"],
                fmt(s["cores_held_s"], "s"),
                fmt(s["device_wait_s"], "s"),
                fmt(s["task_wait_s"], "s"),
                fmt(s["task_turnaround_s"], "s"),
                fmt(s["served_in_hold"]),
                fmt(s["completed"]),
                cost_s,
            )
        )
    lines.append("")
    lines.append(
        "cores held: seconds the classical's cores were allocated. dev wait: seconds the device was"
    )
    lines.append(
        "waited for with the cores free, coschedule only. task wait: created to started, mean per task."
    )
    lines.append(
        "in hold: fraction of tasks the service says ran inside the hold. cost*: estimated, not billed."
    )
    return "\n".join(lines)


def comparison(summary):
    """Baseline against coschedule, per path and contention level."""
    lines = []
    paths = sorted({k[0] for k in summary})
    for path in paths:
        for fillers in sorted({k[3] for k in summary if k[0] == path}):
            base = summary.get((path, "baseline", "-", fillers))
            holds = [
                (k, s)
                for k, s in summary.items()
                if k[0] == path and k[1] == "coschedule" and k[3] == fillers
            ]
            if not base or not holds:
                continue
            for k, s in sorted(holds):
                if base["cores_held_s"] is None or s["cores_held_s"] is None:
                    continue
                saved = base["cores_held_s"] - s["cores_held_s"]
                lines.append(
                    "{} with {} fillers, {} hold: cores held {:.0f}s against {:.0f}s baseline, "
                    "{:.0f}s {} per trial, device waited for {:.0f}s with cores free".format(
                        path,
                        fillers,
                        k[2],
                        s["cores_held_s"],
                        base["cores_held_s"],
                        abs(saved),
                        "saved" if saved >= 0 else "MORE",
                        s["device_wait_s"] or 0,
                    )
                )
                if s["task_wait_s"] is not None and base["task_wait_s"] is not None:
                    lines.append(
                        "    tasks waited {:.0f}s each against {:.0f}s baseline, {:.0%} served in the hold".format(
                            s["task_wait_s"],
                            base["task_wait_s"],
                            s["served_in_hold"] or 0,
                        )
                    )
    return (
        "\n".join(lines)
        if lines
        else "no path has both a baseline and a coschedule cell at the same contention yet"
    )


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--record", default=RECORD)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    rows = load(args.record)
    summary = cells(rows)
    if args.json:
        print(
            json.dumps({"|".join(map(str, k)): v for k, v in summary.items()}, indent=2)
        )
        return
    print("{} trials in {}".format(len(rows), args.record))
    print()
    print(table(summary))
    print()
    print("== what coscheduling bought ==")
    print(comparison(summary))


if __name__ == "__main__":
    main()
