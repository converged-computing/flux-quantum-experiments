# Coscheduling experiments on the mock vendor

The mock vendor simulates a queue whose depth is a parameter, which no real
vendor offers, and it costs nothing. These experiments compare two ways of
running a hybrid job on a Flux cluster:

    baseline      one job. It takes its nodes, opens the vendor session, and
                  waits in the vendor queue while holding them.
    coscheduled   a pair. The classical job is submitted held, a one core
                  scout opens the session and waits, and the classical job is
                  released once the QPU is ours.

They need a flux-quantum with the common submit options, and the queue policy
has to be `coschedule`. Under any other policy a held job starts immediately
and both arms measure the same thing. Do not run inside `flux alloc`: a
subinstance has no quantum jobtap plugin, so admission, protection and
preemption are all absent and the numbers mean nothing.

## Setup

    bash preflight.sh 2>&1 | tee preflight.log

Checks the live instance, not the config files: an idle cluster, a qmanager
with durable release, the plugin loaded with preemption on and a core budget
matching the machine, the `coschedule` policy, a qdevice with at least one
qpu per vendor, and that a pair strands on a full machine and is rescued by
preemption. About three minutes. `inspect.sh` reads the graph, the scout
jobspec and the allocation and checks they agree, for when preflight fails and
you want to know what is actually there.

## Running

    nohup bash run.sh > results/run.log 2>&1 &

run.sh checks the setup, runs one pair end to end, then e1 e2 e3 e5 e4 e6, the
analysis and the figures. About six and a half hours, nearly all of it e4.
`E4_TRIALS=5` halves that. e6 needs a core budget too small for every pair to
be admitted, so run.sh reloads the plugin with `E6_BUDGET` around it and puts
the real budget back afterwards.

One experiment by hand:

    OUT=results/e1.csv scripts/experiment.sh e1
    python3 scripts/analyze.py results/*.csv
    python3 scripts/plot.py results/*.csv

A run resumes from its CSV, skipping rows already measured. If the plugin
config changed since, especially `preempt_after`, use `FRESH=1` rather than
mixing two campaigns in one file.

## The experiments

    e1   how long the allocation is held, against vendor queue depth
    e2   the arms under classical contention, where the cost shows up
    e3   core seconds consumed, against allocation size
    e4   does the knee track allocation size, swept over spare cores
    e5   what contention costs, against how long the other work has left
    e6   does admission mean the pair will run. Several pairs at once against
         a budget too small for all of them

Each CSV holds timestamps only. `analyze.py` and `plot.py` derive the metrics
from them, so the model can change without another run. A `.meta` beside each
CSV records the cores, queue policy, plugin config and timing parameters the
run used, read back from the live plugin. Read it before trusting the CSV.

An arm that never starts inside `TIMEOUT` is kept as a row with `timedout=1`.
It contributes to no mean, and analyze.py lists it separately. These rows are
not noise: the baseline needs `size` cores and nothing preempts for it, so one
core short of fitting it never starts, which is the point of the knee sweeps.

## Results

n=10 per cell. From `results/summary.txt`.

Release latency does not grow with the vendor queue. Submit to classical
allocated is 20 to 23 ms from depth 0 to 491, while the baseline's wait climbs
to 26.7 s. The scout absorbs the queue on one core.

Past the point where the pair fits, preemption bounds the cost. Release latency
is 16 to 28 ms while it fits and 2.1 s once it does not, so the billed session
goes from $8.37 to $11.83. The baseline does not merely cost more there, it
never starts inside 120 s. Nothing preempts for an unprotected request.

Coscheduling loses on one node. Counting the scout's core, total core seconds
are 0.69x at size 1, 1.80x at 8 and 2.25x at 96. Break even is size > 1.7.

One grace period, landing in whichever half has to wait. When the pair is one
core short, the scout waits out `preempt_after` and the wait lands in
`vendor_wait_s`. Two cores short, the scout gets in and the classical waits,
so it lands in `release_lat_s`. Same timer, different column, which is why e4
plots submit to allocated. Identical at all four sizes. The first guess, that
the victim's granularity explained the shape, was wrong: chunking the load did
not change it.

With preemption, the cost of contention no longer depends on other people's
jobs. In e5 the pair is allocated 5.2 s after submit whether the other work had
15 or 120 s left, while the baseline waits the whole remainder, 11 to 116 s.

Admission saturates at what the cores can reach. With 20 cores and pairs
needing 9, two are admitted however many are asked for, and the rest are
refused at submit with no job and no session.

## Caveats

A vendor bills from the first task, not from the session opening, so the
queue wait is free in every arm and quantum cost is about equal on an idle
cluster. What coscheduling saves is classical core seconds, which is e3.

The scout holds a core for the whole vendor wait and the classical run, and
that core is counted: `node_seconds` is the classical allocation,
`scout_node_s` the scout, `total_node_s` the sum.

Read core counts with a state: `flux resource list -s all -no '{ncores}'` for
the total, `-s free` for free. With no state field the free, allocated and
down rows merge onto one line and the bare form returns the total.
