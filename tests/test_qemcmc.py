"""The quantum-enhanced MCMC app: the simulator, the wire formats, the
proposal's symmetry, the chain's target, and the runner's sampler with a
scripted path in place of a vendor.
"""

import importlib.util
import math
import os
import random
import sys

import pytest

HERE = os.path.join(os.path.dirname(__file__), "..", "forte")
# the flux-quantum SOFTWARE checkout (separate repo): enclosing checkout
# when this repo is nested inside one, sibling checkout otherwise
_UP = os.path.join(os.path.dirname(__file__), "..", "..")
ROOT = next(
    r
    for r in (os.environ.get("FLUX_QUANTUM_ROOT"), _UP, os.path.join(_UP, "flux-quantum"))
    if r and os.path.exists(os.path.join(r, "examples", "apps", "qemcmc.py"))
)


def _load(name, path=None):
    spec = importlib.util.spec_from_file_location(
        name, path or os.path.join(HERE, name + ".py")
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# the app is an example workload, loaded by path like the runner loads it
Q = _load("qemcmc_app", os.path.join(ROOT, "examples", "apps", "qemcmc.py"))


@pytest.fixture
def runner():
    sys.path.insert(0, HERE)
    try:
        yield _load("qemcmc")
    finally:
        sys.path.remove(HERE)


# ---- the simulator ---------------------------------------------------------


def test_x_prepares_the_state():
    probs = Q.probabilities(Q.simulate(3, [("x", 0), ("x", 2)]), 3)
    assert probs == {"101": pytest.approx(1.0)}


def test_hadamard_is_a_fair_coin():
    probs = Q.probabilities(Q.simulate(1, [("h", 0)]), 1)
    assert probs["0"] == pytest.approx(0.5)
    assert probs["1"] == pytest.approx(0.5)


def test_lowered_rzz_is_the_same_circuit():
    ops = [("h", 0), ("h", 1), ("rzz", 0, 1, 0.7), ("rx", 0, 0.3)]
    a = Q.simulate(2, ops)
    b = Q.simulate(2, Q.lower(ops))
    assert a == pytest.approx(b)


def test_ionq_roundtrip_is_the_same_circuit():
    model = Q.Ising.random(3, seed=5)
    ops = Q.proposal_ops(model, "010", gamma=0.4, t=5.0)
    n, back = Q.from_ionq(Q.to_ionq(3, ops))
    assert n == 3
    assert Q.simulate(3, ops) == pytest.approx(Q.simulate(3, back))


def test_qasm_has_the_measure_and_no_rzz():
    model = Q.Ising.random(2, seed=1)
    qasm = Q.to_qasm3(2, Q.proposal_ops(model, "10", gamma=0.3, t=4.0))
    assert qasm.startswith("OPENQASM 3;")
    assert "qubit[2] q;" in qasm
    assert "c = measure q;" in qasm
    assert "rzz" not in qasm
    assert "cnot q[0], q[1];" in qasm


# ---- the proposal ----------------------------------------------------------


def test_proposal_is_symmetric():
    """Q(s'|s) = Q(s|s'), which is what spares the chain a ratio of
    proposal probabilities it cannot compute."""
    model = Q.Ising.random(3, seed=2)
    rng = random.Random(7)
    for _ in range(5):
        s = Q.int_to_state(rng.randrange(8), 3)
        sp = Q.int_to_state(rng.randrange(8), 3)
        gamma, t = rng.uniform(0.25, 0.6), rng.uniform(2.0, 20.0)
        forward = Q.simulate(3, Q.proposal_ops(model, s, gamma, t))
        backward = Q.simulate(3, Q.proposal_ops(model, sp, gamma, t))
        p_f = abs(forward[Q.state_to_int(sp)]) ** 2
        p_b = abs(backward[Q.state_to_int(s)]) ** 2
        assert p_f == pytest.approx(p_b, abs=1e-9)


def test_chain_samples_the_boltzmann_distribution():
    model = Q.Ising.random(3, seed=0)
    chain = Q.Chain(model, temperature=1.5, seed=0)
    chain.run(Q.LocalSampler(rng=random.Random(1)), 4000)
    seen = Q.empirical(chain.history)
    target = model.boltzmann(1.5)
    tv = 0.5 * sum(abs(seen.get(s, 0.0) - target[s]) for s in target)
    assert tv < 0.08
    assert 0.1 < chain.accepted / chain.steps < 0.95


def test_energy_matches_by_hand():
    model = Q.Ising({(0, 1): 2.0}, [0.5, -1.0])
    # spins (+1, +1): E = -2*1*1 - 0.5*1 + 1.0*1
    assert model.energy("00") == pytest.approx(-2.0 - 0.5 + 1.0)
    # spins (-1, +1): E = +2 + 0.5 + 1.0... spin0 = -1: -0.5*-1 = +0.5
    assert model.energy("10") == pytest.approx(2.0 + 0.5 + 1.0)


# ---- the runner's sampler --------------------------------------------------


class ScriptedResultPath:
    """A vendor whose every task completes at once, answering with the
    exact simulation of what was submitted."""

    name = "ionq"
    device = "fake"
    hold_mode = "probe"

    def __init__(self):
        self.submitted = []
        self.cancelled = []
        self._programs = {}

    def cancel(self, task_id):
        self.cancelled.append(task_id)
        return False  # already completed, like a real path would answer

    def submit(self, shots, credential=None, label="t", program=None):
        tid = "t{}".format(len(self.submitted))
        self.submitted.append((tid, shots, credential, label))
        self._programs[tid] = program
        return tid

    def describe(self, task_id):
        return {
            "status": "completed",
            "in_hold": False,
            "hold_id": None,
            "queue": "public",
            "position": None,
            "priority": None,
            "created": 100.0,
            "started": 101.0,
            "ended": 102.0,
            "terminal": True,
            "cost": 0.1,
        }

    def result(self, task_id, qubits=1):
        n, ops = Q.from_ionq(self._programs[task_id])
        return Q.probabilities(Q.simulate(n, ops), n)


def test_vendor_sampler_drives_the_chain(runner):
    path = ScriptedResultPath()
    sampler = runner.VendorSampler(path, shots=100, credential="cred", timeout=5)
    model = Q.Ising.random(3, seed=3)
    chain = Q.Chain(model, temperature=1.0, seed=3)
    for _ in range(4):
        chain.step(sampler)
    assert len(sampler.tasks) == 4
    assert [s[2] for s in path.submitted] == ["cred"] * 4
    assert [s[3] for s in path.submitted] == ["mcmc-0", "mcmc-1", "mcmc-2", "mcmc-3"]
    for task in sampler.tasks:
        assert task["status"] == "completed"
        assert task["turnaround_s"] == pytest.approx(2.0)
    assert chain.steps == 4
    assert all(len(r["proposal"]) == 3 for r in chain.history)


def test_vendor_sampler_raises_on_a_failed_task(runner):
    class FailingPath(ScriptedResultPath):
        def describe(self, task_id):
            view = ScriptedResultPath.describe(self, task_id)
            view["status"] = "failed"
            return view

    sampler = runner.VendorSampler(FailingPath(), 100, None, timeout=5)
    model = Q.Ising.random(2, seed=0)
    chain = Q.Chain(model, temperature=1.0, seed=0)
    with pytest.raises(RuntimeError):
        chain.step(sampler)


def test_filler_pool_tops_up_and_drains(runner):
    """Every filler completes at once in the scripted path, so each top_up
    replaces the whole target, and drain cancels them all."""
    path = ScriptedResultPath()
    pool = runner.FillerPool(path, shots=10, target=3)
    assert pool.top_up() == 3
    assert pool.top_up() == 3  # all served already, refill the lot
    assert pool.submitted == 6
    assert [s[3] for s in path.submitted][:2] == ["filler-1", "filler-2"]
    drained = pool.drain()
    assert len(drained) == 6
    assert all(f["status"] == "completed" for f in drained)


def test_filler_pool_retires_terminal_fillers(runner):
    """alive() must not re-describe the whole submission history: that
    bookkeeping cost one API round-trip per filler EVER submitted, per
    step, and drowned the queue effect in C2f/C3f attempt 1."""

    class CountingPath(ScriptedResultPath):
        def __init__(self):
            ScriptedResultPath.__init__(self)
            self.describes = 0

        def describe(self, task_id):
            self.describes += 1
            return ScriptedResultPath.describe(self, task_id)

    path = CountingPath()
    pool = runner.FillerPool(path, shots=10, target=2)
    pool.top_up()  # submits 2, nothing to describe yet
    pool.top_up()  # describes 2, retires both, submits 2
    before = path.describes
    pool.top_up()  # must describe ONLY the 2 live ones, not all 4 ever made
    assert path.describes - before == 2
    drained = pool.drain()
    assert len(drained) == 6  # retired fillers still accounted for
    assert pool.submitted == 6


def test_fake_service_results_simulate_the_circuit():
    from flux_quantum.backends.ionq import fake

    state = fake.State()
    _, created = state.create_job(
        {
            "type": "ionq.circuit.v1",
            "shots": 100,
            "backend": "simulator",
            "input": Q.to_ionq(2, [("x", 1)]),
        }
    )
    code, histogram = state.job_results(created["id"])
    assert code == 200
    # qubit 1 set and qubit 0 clear is the state "01", decimal 2
    assert histogram == {"2": pytest.approx(1.0)}


def test_fake_service_results_fall_back_without_a_circuit():
    from flux_quantum.backends.ionq import fake

    state = fake.State()
    _, created = state.create_job({"shots": 1, "backend": "simulator"})
    code, histogram = state.job_results(created["id"])
    assert code == 200
    assert histogram == {"0": 0.5, "1": 0.5}
