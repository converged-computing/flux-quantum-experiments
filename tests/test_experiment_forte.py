"""The Forte experiment's logic, with a scripted path in place of a vendor.

The paths talk to AWS and IonQ. Everything above them, following tasks,
the per task record, the estimate, the summary and the report, is what
these drive.
"""

import importlib.util
import json
import os
import sys

import pytest

HERE = os.path.join(os.path.dirname(__file__), "..", "forte")


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(HERE, name + ".py")
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mods():
    sys.path.insert(0, HERE)
    try:
        yield _load("paths"), _load("trial"), _load("report")
    finally:
        sys.path.remove(HERE)


class ScriptedPath:
    """A vendor whose tasks run through a script of states per poll."""

    name = "braket"  # the estimate prices a session hold by path
    device = "dev"
    hold_mode = "session"

    def __init__(self, script):
        self.script = script  # id -> list of describe() answers, last repeats
        self.calls = {i: 0 for i in script}
        self.submitted = []
        self.cancelled = []

    def submit(self, shots, credential=None, label="t"):
        i = "t{}".format(len(self.submitted))
        self.submitted.append((i, shots, credential, label))
        return i

    def describe(self, i):
        answers = self.script[i]
        n = min(self.calls[i], len(answers) - 1)
        self.calls[i] += 1
        return answers[n]

    def cancel(self, i):
        self.cancelled.append(i)
        return True

    def task_cost(self, shots):
        return 1.0 + 0.01 * shots

    def hold_cost(self, seconds):
        return 0.1 * seconds / 3600


def _view(
    status,
    created=100.0,
    started=None,
    ended=None,
    queue=None,
    position=None,
    in_hold=False,
):
    return {
        "status": status,
        "in_hold": in_hold,
        "hold_id": "job" if in_hold else None,
        "queue": queue,
        "position": position,
        "priority": None,
        "created": created,
        "started": started,
        "ended": ended,
        "terminal": status.upper() in ("COMPLETED", "FAILED", "CANCELLED"),
        "cost": None,
    }


def test_follow_keeps_the_first_queue_sighting_and_estimates_a_start(mods):
    _, trial, _ = mods
    path = ScriptedPath(
        {
            "t0": [
                _view("QUEUED", queue="Priority", position=1, in_hold=True),
                _view("RUNNING", in_hold=True),
                _view("COMPLETED", ended=130.0, in_hold=True),
            ]
        }
    )
    clock = iter([100.0, 110.0, 120.0, 130.0, 140.0, 150.0])
    views = trial.follow(
        path,
        ["t0"],
        timeout=60,
        interval=1,
        clock=lambda: next(clock),
        sleep=lambda s: None,
        quiet=True,
    )
    v = views["t0"]
    assert v["queue"] == "Priority" and v["position"] == 1 and v["in_hold"]
    assert v["started_seen"] is not None and v["ended"] == 130.0
    rec = trial.task_record(v, submitted_at=99.0, submit_latency=0.2)
    assert rec["started_estimated"] and rec["turnaround_s"] == 30.0
    assert rec["wait_s"] == v["started_seen"] - 100.0


def test_follow_prefers_the_services_start_time(mods):
    _, trial, _ = mods
    path = ScriptedPath(
        {
            "t0": [
                _view("started", started=105.0),
                _view("completed", started=105.0, ended=120.0),
            ]
        }
    )
    views = trial.follow(
        path, ["t0"], timeout=60, interval=0, sleep=lambda s: None, quiet=True
    )
    rec = trial.task_record(views["t0"], 99.0, 0.1)
    assert rec["wait_s"] == 5.0 and not rec["started_estimated"]


def test_follow_gives_up_at_the_timeout(mods):
    _, trial, _ = mods
    path = ScriptedPath({"t0": [_view("QUEUED", queue="Normal", position=9)]})
    clock = iter(range(0, 1000, 10))
    views = trial.follow(
        path,
        ["t0"],
        timeout=30,
        interval=10,
        clock=lambda: float(next(clock)),
        sleep=lambda s: None,
        quiet=True,
    )
    assert not views["t0"]["terminal"] and views["t0"]["position_last"] == 9


def test_estimate_counts_tasks_fillers_and_the_hold(mods):
    _, trial, _ = mods
    path = ScriptedPath({})
    rows, total = trial.estimate(
        path,
        "coschedule",
        tasks=3,
        fillers=2,
        shots=100,
        filler_shots=50,
        max_seconds=3600,
    )
    assert total == pytest.approx(3 * 2.0 + 2 * 1.5 + 0.1)
    rows, total = trial.estimate(
        path,
        "baseline",
        tasks=3,
        fillers=0,
        shots=100,
        filler_shots=50,
        max_seconds=3600,
    )
    assert total == pytest.approx(6.0) and len(rows) == 1


def test_run_tasks_submits_with_the_credential_and_drain_cancels(mods):
    _, trial, _ = mods
    path = ScriptedPath(
        {
            "t0": [_view("COMPLETED", ended=110.0, in_hold=True)],
            "t1": [_view("COMPLETED", ended=111.0, in_hold=True)],
            "t2": [_view("QUEUED", queue="Normal")],
        }
    )
    records, phase = trial.run_tasks(path, 2, 10, "tok", timeout=5, quiet=True)
    assert [s[2] for s in path.submitted] == ["tok", "tok"]
    assert phase["all_done"] and all(r["in_hold"] for r in records)
    fillers = trial.fill(path, 1, 5)
    assert fillers == ["t2"] and path.submitted[-1][2] is None
    drained = trial.drain(path, fillers)
    assert drained[0]["cancelled"] and path.cancelled == ["t2"]


def test_summary_and_report_compare_the_arms(mods, tmp_path):
    _, trial, report = mods
    path = ScriptedPath({})

    def rec(arm, hold, held_from, ended, wait, in_hold):
        tasks = [
            {
                "id": "x",
                "status": "completed",
                "in_hold": in_hold,
                "wait_s": wait,
                "turnaround_s": wait + 5,
                "cost_usd": None,
            }
        ]
        phase = {
            "started_at": held_from if not hold else held_from - 50,
            "all_submitted_at": None,
            "submit_s": 0.1,
            "ended_at": ended,
            "makespan_s": ended - held_from,
            "all_done": True,
        }
        h = (
            {"opened_at": held_from - 60, "ready_at": held_from, "wait_s": 60.0}
            if hold
            else None
        )
        r = {
            "cell": {"path": "braket", "arm": arm, "hold": hold, "fillers": 3},
            "notes": [],
        }
        trial.finish_record(r, tasks, phase, h, [], path, [("t", 2.0)])
        return r

    rows = [
        rec("baseline", None, 0.0, 300.0, 200.0, False),
        rec("coschedule", "session", 0.0, 60.0, 5.0, True),
    ]
    assert rows[0]["summary"]["cores_held_s"] == 300.0
    assert (
        rows[1]["summary"]["cores_held_s"] == 60.0
        and rows[1]["summary"]["device_wait_s"] == 60.0
    )
    where = tmp_path / "runs.jsonl"
    with open(where, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    summary = report.cells(report.load(str(where)))
    assert summary[("braket", "baseline", "-", 3)]["cores_held_s"] == 300.0
    text = report.comparison(summary)
    assert "240s saved" in text and "100% served in the hold" in text
