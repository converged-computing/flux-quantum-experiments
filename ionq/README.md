# Coscheduling experiments against IonQ

The IonQ simulator is free and serves every job on arrival, so it cannot show
a queue or priority. What it can show, at any scale and at no cost, is
everything on our side of the API with a real vendor behind it: the pair, the
scout, the hold, the handover, and a classical job that submits circuits into
the session it was handed. The same scripts run against a QPU with `DRY_RUN=0`,
where the queue becomes real and the runs bill.

Three arms:

    plain        one job, no session. Submits its circuits as anyone would.
    session      one job. Takes its nodes, opens a session, runs inside it.
    coscheduled  the pair. The classical job is submitted held, the scout opens
                 the session and warms it up, the classical job is released
                 into it.

Needs a flux-quantum with the IonQ backend and the common submit options, the
`coschedule` queue policy, `ionq` in the jobtap plugin's vendors, and the
graph populated with `qdevice_ionq`. The key is `IONQ_API_KEY`.

## Setup

    export IONQ_API_KEY=...
    bash preflight.sh 2>&1 | tee preflight.log

Checks the key and the service, that flux-quantum knows the vendor, the
plugin and the policy, the qpu count under `qdevice_ionq`, and runs one
dry-run pair end to end. The scout's output says whether the account can open
sessions. Without them the scout holds with a probe job and says so, and
every session result is then a probe result.

Without a key, the fake service stands in for IonQ:

    flux python -m flux_quantum.backends.ionq.fake --port 8765 &
    export IONQ_API_KEY=x IONQ_API_URL=http://127.0.0.1:8765

`FAKE_IONQ_SECONDS=2` makes each job take two seconds per state, so the
timings have a shape. `FAKE_IONQ_NO_SESSIONS=1` models an account without
the beta.

## Running

    OUT=results/e1.csv scripts/experiment.sh e1
    python3 scripts/analyze.py results/*.csv

Settings are environment variables: `TRIALS` (5), `SIZE` cores for the
classical job (4), `ITERS` circuits per job (1), `SHOTS` (100), `TARGET`
(qpu.forte-1), `DRY_RUN` (1), `HOLD` session or probe, `TIMEOUT` (600). A
run resumes from its CSV. The `.meta` beside it records what the run used.

## The experiments

    e1   overhead of the pair, one at a time. Every milestone from submit to
         the first result, against the two baselines.

Each CSV holds flux's timestamps from the eventlogs: submit, scout start,
session posted, allocated, started, finished. Beside it, a directory of the
workload's output per trial holds IonQ's timestamps for every circuit:
`submitted_at`, `started_at`, `completed_at` and `execution_duration_ms`.
`metrics.py` derives everything from the two.

## The workload

`scripts/workload.py` is the classical half. It submits GHZ circuits one
after another, the way an iterative algorithm does, and prints a JSON line per
circuit with the clock on both sides. Under the scout it submits into
`QUANTUM_SESSION_ID`. With `--own-session` it opens a session first, which is
the session baseline. With `--no-session` it submits plain jobs.
