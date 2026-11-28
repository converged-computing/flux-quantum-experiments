#!/usr/bin/env python3
"""Quantum-enhanced MCMC as the workload, instead of the one qubit circuit.

The application is Layden et al.'s quantum-enhanced Markov chain Monte
Carlo (Nature 619, 282-287, 2023): a chain whose proposal is a quantum
circuit, one task per step, with a classical accept between. It is the
tightest hybrid loop there is, so what the hold buys shows up directly as
iteration rate: every second a step waits in a queue is a second the whole
chain, and whatever classical allocation carries it, stands still.

Arms, as in trial.py:

    baseline    every proposal goes to the device's public queue
    coschedule  the hold is opened first with the backend's own calls, and
                every proposal is submitted into it

    qemcmc.py local --qubits 6 --steps 2000            # exact, free
    qemcmc.py estimate --path braket --steps 20
    qemcmc.py run --path ionq --arm coschedule --steps 20 --qubits 6
    qemcmc.py run --path braket --arm baseline --steps 20 --yes
    qemcmc.py run --path ibm --arm classical --steps 1  # inside a flux pair

local runs the chain on the built-in simulator and writes the same record,
so mock and real runs read the same. Every run appends one JSON line to
runs-qemcmc.jsonl.

A caution before a long run on a new device: the decoding of IonQ's result
histogram assumes qubit 0 is the least significant bit of the key. Submit
one state preparation circuit first (--steps 1 on a known seed) and check
the proposal comes back shaped like the prepared state; pass --msb if the
device reads the other way.
"""

import argparse
import importlib.util
import json
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def _software_root():
    """The flux-quantum SOFTWARE checkout, which this experiments repo is
    deliberately separate from. It supplies the example app (and, when the
    package is not pip-installed, flux_quantum itself). Candidates:
    FLUX_QUANTUM_ROOT; the enclosing checkout when this repo is cloned
    inside one (the workstation layout); the sibling checkout (the cluster
    layout, /opt/flux-quantum next to /opt/flux-quantum-experiments)."""
    candidates = [
        os.environ.get("FLUX_QUANTUM_ROOT"),
        os.path.join(HERE, "..", ".."),
        os.path.join(HERE, "..", "..", "flux-quantum"),
    ]
    for root in candidates:
        if root and os.path.exists(os.path.join(root, "examples", "apps", "qemcmc.py")):
            return os.path.abspath(root)
    raise SystemExit(
        "qemcmc: cannot find the flux-quantum checkout (looked for "
        "examples/apps/qemcmc.py). Set FLUX_QUANTUM_ROOT."
    )


SOFTWARE = _software_root()
sys.path.insert(0, SOFTWARE)

import paths as P  # noqa: E402
import trial as T  # noqa: E402


def _load_app():
    """The application is an example workload, not part of the plugin, so
    it is loaded from the repository checkout by path. (Not by name: this
    runner is also called qemcmc.py, and HERE is first on sys.path.)"""
    if "qemcmc_app" in sys.modules:
        return sys.modules["qemcmc_app"]
    path = os.path.join(SOFTWARE, "examples", "apps", "qemcmc.py")
    spec = importlib.util.spec_from_file_location("qemcmc_app", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["qemcmc_app"] = module
    spec.loader.exec_module(module)
    return module


Q = _load_app()

RECORD = os.path.join(HERE, "runs-qemcmc.jsonl")


class VendorSampler(object):
    """A proposal sampler that submits one task per step through a path.

    Each call builds the step's circuit, submits it with the credential,
    polls it to the end with trial.follow, reads the measured weights back
    and draws the proposal. The task records pile up in .tasks.
    """

    def __init__(self, path, shots, credential, timeout, seed=0, msb=False):
        self.path = path
        self.shots = shots
        self.credential = credential
        self.timeout = timeout
        self.rng = random.Random(seed)
        self.msb = msb
        self.tasks = []

    def program(self, n, ops):
        if self.path.name == "ionq":
            return Q.to_ionq(n, ops)
        if self.path.name == "ibm":
            # IBM takes only ISA circuits; the path transpiles to its target
            return self.path.program_from_ops(n, Q.lower(ops))
        return Q.to_qasm3(n, ops)

    def __call__(self, n, ops, meta):
        label = "mcmc-{}".format(meta["step"])
        submitted_at = time.time()
        task_id = self.path.submit(
            self.shots, self.credential, label, program=self.program(n, ops)
        )
        submit_latency = time.time() - submitted_at
        views = T.follow(self.path, [task_id], self.timeout)
        view = views[task_id]
        record = T.task_record(view, submitted_at, submit_latency)
        self.tasks.append(record)
        if str(view.get("status", "")).upper() not in ("COMPLETED",):
            raise RuntimeError(
                "step {} task {} ended {}".format(
                    meta["step"], T.short(task_id), view.get("status")
                )
            )
        weights = self.path.result(task_id, qubits=n)
        if self.msb:
            weights = {state[::-1]: w for state, w in weights.items()}
        return Q.draw(weights, self.rng)


class FillerPool(object):
    """Contention made to order, so a run means the same whenever it runs.

    Keeps target tasks waiting in the device's public queue: top_up submits
    however many have been served since last time. drain cancels whatever is
    still queued at the end, and a cancelled task is not billed, so the run
    pays only for the fillers the device actually executed during it.
    """

    def __init__(self, path, shots, target):
        self.path = path
        self.shots = shots
        self.target = target
        self.ids = []   # fillers that may still be in the queue
        self.done = []  # final records of fillers already terminal
        self.submitted = 0

    def alive(self):
        # Describe only what can still be queued. Describing the full
        # history every step costs one API round-trip per filler EVER
        # submitted, which grows with exactly the knob the sweep turns:
        # C2f/C3f attempt 1 spent ~500 s of this bookkeeping on a ~100 s
        # chain at 10 fillers, in BOTH arms, drowning the queue effect.
        still = []
        for i in self.ids:
            view = self.path.describe(i)
            if view.get("terminal"):
                self.done.append(
                    {
                        "id": i,
                        "cancelled": False,
                        "status": view.get("status"),
                        "cost_usd": view.get("cost"),
                    }
                )
            else:
                still.append(i)
        self.ids = still
        return len(still)

    def top_up(self):
        need = self.target - self.alive()
        if need <= 0:
            return 0
        # Submit the batch CONCURRENTLY. An idle device executes a 10-shot
        # task in about a second - faster than tasks can be submitted one at
        # a time - so serial submission can never build standing depth:
        # in C2f/C3f attempt 2 the baseline's chain tasks entered the queue
        # at position 2-3 behind 10 nominally queued fillers. A concurrent
        # burst is in the queue all at once, which is what this class's
        # docstring promises.
        import concurrent.futures

        first = self.submitted
        self.submitted += need
        labels = ["filler-{}".format(first + k + 1) for k in range(need)]
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(need, 16)
        ) as pool:
            futures = [
                pool.submit(self.path.submit, self.shots, None, label)
                for label in labels
            ]
            self.ids.extend(f.result() for f in futures)
        return need

    def drain(self):
        out = list(self.done)
        for i in self.ids:
            cancelled = self.path.cancel(i)
            view = self.path.describe(i)
            out.append(
                {
                    "id": i,
                    "cancelled": cancelled,
                    "status": view.get("status"),
                    "cost_usd": view.get("cost"),
                }
            )
        return out


def chain_summary(chain, tasks, started, ended):
    waits = [t["wait_s"] for t in tasks if t.get("wait_s") is not None]
    turns = [t["turnaround_s"] for t in tasks if t.get("turnaround_s") is not None]
    makespan = (ended - started) if (started and ended) else None
    out = chain.summary()
    out.update(
        {
            "mean_wait_s": (sum(waits) / len(waits)) if waits else None,
            "mean_turnaround_s": (sum(turns) / len(turns)) if turns else None,
            "makespan_s": makespan,
            "steps_per_hour": ((chain.steps / makespan * 3600.0) if makespan else None),
            "cost_usd": sum(t["cost_usd"] or 0.0 for t in tasks) or None,
        }
    )
    return out


def write(record, out=RECORD):
    with open(out, "a") as f:
        f.write(json.dumps(record) + "\n")
    T.say("recorded to {}".format(out))


def base_record(args, model):
    return {
        "schema": "qemcmc-1",
        "started": time.time(),
        "path": getattr(args, "path", "local"),
        "arm": getattr(args, "arm", "local"),
        "device": getattr(args, "device", None),
        "hold_mode": getattr(args, "hold", None),
        "qubits": args.qubits,
        "steps_requested": args.steps,
        "shots": getattr(args, "shots", None),
        "fillers_requested": getattr(args, "fillers", 0),
        "filler_shots": getattr(args, "filler_shots", None),
        "temperature": args.temperature,
        "seed": args.seed,
        "trotter": args.trotter,
        "model_alpha": model.alpha,
        "notes": [],
    }


def estimate(path, steps, shots, max_seconds, fillers=0, filler_shots=10):
    per_task = path.task_cost(shots)
    hold = path.hold_cost(max_seconds)
    # the worst case is every filler executing: the pool refills what the
    # device serves, up to once per step, and drained fillers cost nothing
    filler_worst = path.task_cost(filler_shots) * fillers * (steps + 1)
    return {
        "per_task_usd": per_task,
        "tasks_usd": per_task * steps,
        "hold_usd": hold,
        "fillers_worst_usd": filler_worst,
        "total_usd": per_task * steps + hold + filler_worst,
    }


def cmd_local(args):
    model = Q.Ising.random(args.qubits, seed=args.seed)
    chain = Q.Chain(model, args.temperature, seed=args.seed, trotter=args.trotter)
    record = base_record(args, model)
    t0 = time.time()
    chain.run(Q.LocalSampler(rng=random.Random(args.seed + 1)), args.steps)
    record["steps"] = chain.history
    record["summary"] = chain_summary(chain, [], t0, time.time())
    if args.qubits <= 10:
        target = model.boltzmann(args.temperature)
        seen = Q.empirical(chain.history)
        record["summary"]["total_variation_to_boltzmann"] = 0.5 * sum(
            abs(seen.get(s, 0.0) - target[s]) for s in target
        )
    record["ended"] = time.time()
    T.say(json.dumps(record["summary"], indent=2))
    write(record, args.out)


def cmd_estimate(args):
    path = P.make_path(args.path, args.device, args.hold, args.allow_ionq_session)
    e = estimate(
        path, args.steps, args.shots, args.max_seconds, args.fillers, args.filler_shots
    )
    T.say(json.dumps(e, indent=2))


def cmd_run(args):
    path = P.make_path(args.path, args.device, args.hold, args.allow_ionq_session)
    e = estimate(
        path, args.steps, args.shots, args.max_seconds, args.fillers, args.filler_shots
    )
    T.say("estimated cost, fillers at their worst: ${:.2f}".format(e["total_usd"]))
    if e["total_usd"] > args.max_usd and not args.yes:
        T.say(
            "that is over --max-usd {}; pass --yes to run anyway, or fewer "
            "--steps".format(args.max_usd)
        )
        return 2
    model = Q.Ising.random(args.qubits, seed=args.seed)
    chain = Q.Chain(model, args.temperature, seed=args.seed, trotter=args.trotter)
    record = base_record(args, model)
    record["estimate"] = e
    record["device_before"] = path.device_state()

    hold = None
    credential = None
    if args.arm == "coschedule":
        hold = path.hold(args.max_seconds, args.max_seconds)
        T.say("opening the hold ({})".format(path.hold_mode))
        hold.open()
        credential = hold.wait()
        T.say("hold ready after {:.1f}s".format(hold.wait_s))
    elif args.arm == "classical":
        # inside a flux pair: the scout opened the hold and handed its
        # credential over in the environment
        credential = path.credential_from_environment()
        T.say(
            "credential from the scout: {}".format(
                "yes" if credential else "none, submitting to the public queue"
            )
        )

    pool = None
    if args.fillers:
        pool = FillerPool(path, args.filler_shots, args.fillers)
        T.say("{} fillers into the public queue".format(pool.top_up()))

    sampler = VendorSampler(
        path, args.shots, credential, args.timeout, seed=args.seed + 1, msb=args.msb
    )
    t0 = time.time()
    try:
        for _ in range(args.steps):
            if pool is not None:
                added = pool.top_up()
                if added:
                    T.say("topped the queue up with {} fillers".format(added))
            rec = chain.step(sampler)
            T.say(
                "step {:>3} {} -> {} {} E={:.3f}".format(
                    rec["step"],
                    rec["state"],
                    rec["proposal"],
                    "accept" if rec["accepted"] else "reject",
                    chain.energy,
                )
            )
    except (RuntimeError, KeyboardInterrupt) as e:
        record["notes"].append("stopped early: {}".format(e))
        T.say("stopped early: {}".format(e))
    finally:
        if pool is not None:
            record["fillers"] = pool.drain()
            record["fillers_submitted"] = pool.submitted
            T.say(
                "drained the fillers: {} submitted, {} executed".format(
                    pool.submitted,
                    sum(1 for f in record["fillers"] if not f["cancelled"]),
                )
            )
        if hold is not None:
            hold.close()
            record["hold"] = hold.record()
    ended = time.time()
    record["steps"] = chain.history
    record["tasks"] = sampler.tasks
    record["summary"] = chain_summary(chain, sampler.tasks, t0, ended)
    record["ended"] = ended
    T.say(json.dumps(record["summary"], indent=2))
    write(record, args.out)
    return 0


def common(sub):
    sub.add_argument("--qubits", type=int, default=6)
    sub.add_argument("--steps", type=int, default=20)
    sub.add_argument("--temperature", type=float, default=1.0)
    sub.add_argument("--seed", type=int, default=0)
    sub.add_argument("--trotter", type=int, default=2)
    sub.add_argument("--out", default=RECORD)


def vendor(sub):
    sub.add_argument("--path", required=True, choices=("braket", "ionq", "ibm"))
    sub.add_argument("--device", default=None)
    sub.add_argument("--hold", default=None, choices=(None, "session", "probe"))
    sub.add_argument("--allow-ionq-session", action="store_true")
    sub.add_argument("--shots", type=int, default=100)
    sub.add_argument(
        "--fillers",
        type=int,
        default=0,
        help="keep this many tasks waiting in the public queue, topped up "
        "before every step, cancelled at the end",
    )
    sub.add_argument("--filler-shots", type=int, default=10)
    sub.add_argument("--max-seconds", type=int, default=1800)
    sub.add_argument("--timeout", type=int, default=600, help="per task, seconds")
    sub.add_argument("--max-usd", type=float, default=25.0)
    sub.add_argument("--msb", action="store_true", help="flip result bit order")
    sub.add_argument("--yes", action="store_true")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    subs = ap.add_subparsers(dest="cmd", required=True)

    local = subs.add_parser("local", help="the chain on the exact simulator")
    common(local)
    local.set_defaults(fn=cmd_local)

    est = subs.add_parser("estimate", help="cost, nothing created")
    common(est)
    vendor(est)
    est.set_defaults(fn=cmd_estimate)

    run = subs.add_parser("run", help="the chain through a vendor")
    common(run)
    vendor(run)
    run.add_argument(
        "--arm", required=True, choices=("baseline", "coschedule", "classical")
    )
    run.set_defaults(fn=cmd_run)

    args = ap.parse_args(argv)
    # The chain is serial. Launched with more than one task (flux submit -nN,
    # as in the vendors/run.sh pair), rank 0 drives it; any other rank would
    # run a second chain against the same vendor bill.
    try:
        rank = int(os.environ.get("FLUX_TASK_RANK") or 0)
    except ValueError:
        rank = 0
    if rank:
        T.say("task rank {}: rank 0 drives the chain, nothing to do".format(rank))
        return 0
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
