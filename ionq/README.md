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

The instance shares nothing between its nodes, and the pair runs where the
scheduler puts it. So this directory has to be at the same path on every
node, and the service has to be reachable from every node. The fake listens
on the loopback unless told otherwise, so on more than one node start it
with `--bind 0.0.0.0` and use the URL it prints, which is this node's
address on the instance's network:

    flux python -m flux_quantum.backends.ionq.fake --port 8765 --bind 0.0.0.0 &
    export IONQ_API_KEY=x IONQ_API_URL=http://<the address it printed>:8765

Preflight checks both from every node before it runs anything. `FAKE=1
run.sh` starts the fake itself and binds it this way when the instance has
more than one node.

`FAKE_IONQ_SECONDS=2` makes each job take two seconds per state, so the
timings have a shape. `FAKE_IONQ_NO_SESSIONS=1` models an account without
the beta. Sessions expire at their duration limit, as the service's do.

`FAKE_IONQ_QUEUE=30-90` gives the fake a device queue: every job waits that
many seconds, drawn per job from the range, before it is served, except a
job in a session that has already started, which is served on arrival.
That is the one thing a session buys, and the one thing the simulator
cannot show. The queue can be changed while the fake runs, which is how e5
sweeps it. Production queues on a QPU are minutes to hours, so the sweep
is the same shape at a scale that fits in an hour.

## Running

    nohup bash run.sh > results/run.log 2>&1 &        # against IonQ
    FAKE=1 nohup bash run.sh > results/run.log 2>&1 & # against the fake service

run.sh runs preflight, then e1 e3 e2 e4, e5 when `FAKE=1`, and the
analysis. One experiment by hand:

    OUT=results/e1.csv scripts/experiment.sh e1
    python3 scripts/analyze.py results/*.csv
    python3 scripts/plot.py results/*.csv          # figures into plots/, needs matplotlib

Settings are environment variables: `TRIALS` (5), `SIZE` cores for the
classical job (4), `ITERS` circuits per job (1), `SHOTS` (100), `TARGET`
(qpu.forte-1), `DRY_RUN` (1), `HOLD` session or probe, `TIMEOUT` (600),
`PAIRS` for e2 (1 2 4 8), `ITERS_SWEEP` for e3 (1 5 20), `QUEUE_SWEEP` for
e5 (0 10 30 60), and from run.sh `ITERS5` (5) and `TRIALS5` (3) for e5. A
run resumes from its CSV. The `.meta` beside it records what the run used.

## The experiments

    e1   overhead of the pair, one at a time. Every milestone from submit to
         the first result, against the two baselines.
    e2   pairs submitted at once. The qpu count under qdevice_ionq caps how
         many hold a device together and the plugin's budget caps admission,
         so rows per batch say how many were created and the timestamps say
         how they were served. A refused pair leaves no row.
    e3   the three arms with more circuits per job. What a session buys an
         iterative workload is in the per circuit latency, and on a QPU in
         whether later circuits queue again.
    e4   the hold is released whatever happens to the pair. The classical job
         cancelled, the scout cancelled, the hold limit expiring under the
         workload, and the scout giving up before the hold is active. Each
         check records what the service says about the session afterwards.
         A session left started is billed and blocks the device.
    e5   the three arms against a device queue, fake only. A plain job holds
         its cores through the queue for every circuit, a session job for
         its first circuit only, and the pair for none, since the scout
         waits the queue out on one core. The queue is swept and each row
         records it, so the figure is core seconds per job against the
         queue. This is the utilisation case for coscheduling, and the
         simulator cannot make it since it has no queue.

Each CSV holds flux's timestamps from the eventlogs: submit, scout start,
session posted, allocated, started, finished. Beside it, a directory of the
workload's output per trial holds IonQ's timestamps for every circuit:
`submitted_at`, `started_at`, `completed_at` and `execution_duration_ms`.
`metrics.py` derives everything from the two. e4 has its own columns.

The figures, from `plot.py`:

    fig1-anatomy    one pair on a timeline against the plain job, every
                    interval from the submit on one clock
    fig2-first      time to the first result by arm, as the sum of its parts
    fig3-pairs      e2: when each pair of a batch got its cores, and the
                    makespan against the batch
    fig4-circuits   e3: job time against circuits per job, and the per
                    circuit latency inside and outside a session
    fig5-cores      e5: core seconds per job against the device's queue,
                    per arm, and the share of them spent in the queue

## Running on Forte

Everything above was against the fake. On the device every circuit bills,
so the campaign is not run as it stands, and run.sh is left as it ran
against the fake so those results can be reproduced. The device has its
own path, pilot.sh, with small stages, each priced before it runs:

    export IONQ_API_KEY=...
    bash pilot.sh estimate    prices every stage at the account's rates, bills nothing
    bash pilot.sh smoke       one plain job and one pair, one circuit each
    bash pilot.sh bill        what the service billed, job by job

Each stage asks the service's estimate endpoint what a circuit and a
warm-up cost at the account's rates, with the per job minimum, multiplies
by what the stage submits, and refuses when that is over `BUDGET`, 100 USD
by default. Every session it opens carries the same amount as a cost
limit, which the service enforces, so a session nobody is watching cannot
bill past it. Jobs are one task, `TASKS=1`, so one copy of each circuit is
sent; the fake campaign ran `SIZE` tasks, each submitting its own, which
the simulator did not charge for.

`smoke` is the first thing to run: a plain job and a pair with one circuit
each, three billed jobs. Afterwards `bill` reads every job back from the
service and shows the backend it ran on, the session it ran in, its status
and what was charged, and flags anything that ran on the simulator or
outside a session. That, and not our own logs, is the check that it works.
Then the stages in order, each under the budget:

    bash pilot.sh e1          the three arms, 2 trials, about 8 jobs
    bash pilot.sh e3          the arms at 1 and 5 circuits, 2 trials, about 40 jobs
    bash pilot.sh e4          the four hold checks once, with 3 circuit jobs

e3 at 1 and 5 circuits is the question the simulator could not answer,
whether a session's later circuits skip the queue. e2 serialises pairs on
the one qpu in the graph and e5 needs the fake, so neither runs on the
device. `TIMEOUT` is per job and a Forte queue is minutes to hours, so it
is 4 hours here. Sessions are in beta and the account needs them enabled,
which preflight section 5 reports from a dry run on the simulator. Run
preflight with the real key first: its section 1b prints the account's
price for one circuit, and the public rate cards put a 100 shot Forte
circuit between about $8 and $26, so `smoke` is $25 to $80 and the pilot
as a whole a few hundred to about $1,500.

## What the fake service shows

A run against the fake, `FAKE=1 run.sh` with a second per job state, is a
check of the pipeline and a measure of our own overhead, not of IonQ. Its
numbers:

    plain or session job, submit to first result    3.1 s
    coscheduled pair, submit to first result       13.7 s
    of which the scout getting the hold            10.4 s
    handover, release to allocation                 3 ms
    allocation to running                           2 ms
    per circuit, every arm                          3.0 s

The pair costs one thing, the time the scout takes to see the session
active, and nothing on the flux side. In the fake the warm-up takes three
seconds and the scout polls the service every five, so ten of the 10.4
seconds are polling granularity. On a device the queue wait is the whole
point and the poll interval is noise under it. On the simulator, which
serves on arrival, a session buys nothing per circuit, and the three arms
are the same there.

With one qpu under `qdevice_ionq`, pairs submitted at once take the device
one at a time: the k-th pair is allocated after k-1 pair lifetimes and the
makespan is k times one pair. That is the hold doing its job, the same way
a session excludes other sessions on the device.

e4 found one leak, since fixed. Cancelling the classical job, the hold
expiring under it, and the scout giving up all ended the session.
Cancelling the scout did not: the scout blocked in the eventlog wait, where
its SIGTERM handler never ran, flux killed it, and the classical job kept
submitting into the open session for the five minutes it had left. On a
device that is a billed session with nothing holding it. The wait now runs
in the reactor with a signal watcher beside it, and a cancelled scout
closes its session in milliseconds. The first run's CSV said `none` for
every check because the session id was parsed from the eventlog's JSON form
and flux prints the text form, also fixed.

The CSV gained a `queue` column with e5, so a results directory from before
it will not resume. Move it aside and start fresh.

e5, with 4 cores and 5 circuits per job, three trials per level:

    queue per circuit        0 s     10 s     30 s     60 s
    plain, core seconds       61      241      641     1242
    session, core seconds     61       97      177      297
    pair, classical cores     61       61       61       61
    pair, with the scout      87       92      112      142

A plain job grows by 4 cores × 5 circuits, 20 core seconds per second of
queue. A session job grows by 4, since only its first circuit queues. The
pair's classical cores do not grow at all, and the scout's one core grows
by 1. Below a queue of about 1.4 s the plain job is cheaper than the pair,
below about 9 s a self-opened session is, and at 60 s the pair costs an
eighth of plain and half of session. The pair's fixed cost is the scout's
10 s to see the session active, polling granularity in the fake. The
slopes are what carry to a device: the pair's cost in a queue scales with
the scout's one core, the baselines' with the whole allocation.

## The workload

`scripts/workload.py` is the classical half. It submits GHZ circuits one
after another, the way an iterative algorithm does, and prints a JSON line per
circuit with the clock on both sides. Under the scout it submits into
`QUANTUM_SESSION_ID`. With `--own-session` it opens a session first, which is
the session baseline. With `--no-session` it submits plain jobs.

## On the QPU

`DRY_RUN=0 TARGET=qpu.forte-1` runs the same experiments on hardware, where
every circuit bills. The questions change: the queue is real, so e1's
`scout_lead_s` becomes the wait for the hold, and e3 shows whether a session
keeps later circuits from queueing again. Start with `TRIALS=1 ITERS=1` and
read the `.meta` and the cost before going further.
