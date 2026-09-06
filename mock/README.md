# Coscheduling experiments

    ./run.sh              e1 e2 e3 e4, then analysis and figures
    ./run.sh e2 e4        just those two

## Before running

Run the gate. It takes about three minutes, costs no vendor money, and exits
non-zero if anything is wrong:

    bash preflight.sh 2>&1 | tee preflight-$(date +%Y%m%d-%H%M).log

Keep the log beside the results. It records the state the machine was in when
the data was taken, which is the thing you cannot reconstruct afterwards.

It checks, among other things, that the instance is idle, that the installed
qmanager really has durable release rather than just the source tree being on
the right branch, that the plugin's own log line says preemption is on rather
than the toml claiming it, that the live queue policy is easy, hybrid or
conservative, and that every vendor has a qdevice with at least one qpu under
it. A qdevice with no qpu can never match a scout, and it passes a naive check.

Do not run the experiments inside `flux alloc`. A subinstance gets its own job
manager with no quantum jobtap plugin and no config, so admission control, the
protection flag and preemption are all absent and fluxion falls back to fcfs,
the one policy where a held job reserves nothing. Every arm still produces
numbers. They just do not mean anything.

The two things the gate is checking that used to be done by hand:

    flux module stats sched-fluxion-qmanager | grep policy
    flux python -m flux_quantum.populate mock

FLUX_QUANTUM_MOCK is exported by run.sh, but if you invoke the scripts by hand
inside a subinstance it has to be set before flux start. The CLIPlugin validate
hook is also called by the job ingest validator, a separate process that starts
with the broker.

## The experiments

    e1   what the allocation is held for, against vendor queue depth
    e2   the arms under classical contention, where the cost shows up
    e3   core seconds consumed, against allocation size
    e4   does the knee track allocation size, swept over spare cores
    e5   what contention costs, against how long the ordinary work has left to
         run. Run it twice, once with preempt_after set on the jobtap plugin and
         once without, and compare. Without preemption the cost is somebody
         else's job length, with it the cost is bounded by preempt_after.

## The arms

    baseline      an ordinary job that takes its nodes, then opens the session
                  and waits in the vendor queue while holding them
    coscheduled   the classical is held, the scout waits, the classical starts
                  once the QPU is ours
    nowarmup      currently sets queue depth 0, which measures an empty vendor
                  queue rather than skipping the priority wait. Misnamed, and in
                  no dataset. Implement it against --quantum-ibm-skip-warmup or
                  delete it.

## Two open questions, settle these before the knee figures are cited

Run `bash diagnose-knee.sh`. About four minutes, mock only.

**The 2.2s release latency may be a parameter artifact.** preempt_after is 5.0s
and e4's vendor wait is about 2.75s. 5.0 - 2.75 = 2.25, and every allocation
size measured 2.200. That is consistent with the preemption timer running
concurrently with the vendor wait rather than starting when the classical is
released. If so the number is a function of DEPTH and preempt_after and moves
when either does, which is not what a figure captioned "every size breaks the
same way" implies.

**The cliff is one core off from the model.** The scout takes one core
(`cli.py`, `scout_cores=1`), so a pair of size N needs N+1 and should stop
fitting at slack 0, where the spare equals N. It does not. slack 0 returns in
0.003s and only slack -1 breaks. Either the scout is not costing a core at
placement time, or admission control reserves one more than it needs. Until this
is settled fig5 shades what was measured rather than what was predicted.

## Reading resource counts

    total cores   flux resource list -s all  -no '{ncores}'
    free cores    flux resource list -s free -no '{ncores}'
    per state     flux resource list -no '{state} {ncores}'

Not the bare `-no '{ncores}'`. A format string with no state field in it makes
flux resource list merge the free, allocated and down rows onto one line, so the
bare form returns the total while reading like the free count, and it counts
down cores as capacity. A whole session was spent chasing a scheduler bug that
was this.

## Censored trials

An arm that never starts inside TIMEOUT is written as a row with blank timings
and `timedout=1`, not dropped. It contributes to no mean, and analyze.py lists
it under "conditions where an arm could not run".

This matters because the dropped rows were never random. inline needs `size`
cores and has no preemption path, so at `slack = -1`, one core short of fitting,
it can only be freed by the background load expiring, which by design it never
does. That cell timed out at every size in the first pilot and vanished from the
CSV, which is the single point the knee sweep exists to measure. The arms were
being compared over different condition sets, with the difference concentrated
exactly at the knee.

TIMEOUT defaults to 120s for e2 and e4 and 600s elsewhere. A pair rescued by
preemption resolves in about preempt_after seconds, so past that the harness is
only waiting on load expiry. At 600s the first pilot spent 50 of e4's 56 minutes
and 30 of e2's 48 sitting in timeouts.

## Each run writes a .meta

Beside the CSV, recording cores, queue policy, loaded jobtap plugins, whether
preemption is on, and the timing parameters. Read it before trusting a CSV. An
entire campaign was once run with preemption off without anyone noticing.

Resume is matched field by field against the existing CSV, so a file written by
an older version of experiment.sh is refused rather than half matched. Note that
resume keeps the old rows: if the plugin config changed between runs, in
particular preempt_after, use FRESH=1 rather than mixing two campaigns in one
file under a .meta that describes only the second.


## Reading the numbers

A vendor bills from when the first task runs, not from when the session opened,
so the queue wait is free in every arm and quantum cost comes out about equal on
an idle cluster. Coscheduling does not save quantum money. What it saves is
classical core seconds, which is e3.

Raw CSVs hold timestamps only. Everything else is derived in analyze.py and
plot.py, so the model can change without another run.

The scout holds a core of its own for the whole vendor wait and the classical
run, and that core is counted. `node_seconds` is the classical allocation alone,
`scout_node_s` is the scout, and `total_node_s` is what a site actually pays.
Coscheduling only wins once the cores freed during the wait outweigh the scout
core, which is `size > (wait + work) / wait`. Below that it loses, and at size 1
it loses clearly.

## bg_drain_s is not measuring anything yet

BG_JOBS defaults to 1, so the competing stream is one job of one second, against
a stated requirement of BG_JOBS > CORES. In the first pilot bg_drain_s came out
0.104 to 0.108 across every load and every arm. It is a dead column.

Turning the stream up is not sufficient. It is submitted before the trial job,
so it fills the cluster and delays the scout rather than the classical, which
masks the effect e2 is looking for. The trial job needs a higher urgency so it
allocates first, and that does not exist yet. Until it does, e2 measures release
latency and idle node seconds correctly and measures displacement not at all.
