# QeMCMC campaign protocol and log

The authoritative record of what was run, on what, in what state, and what
remains. One entry per cell, appended as executed. Raw per-run data is in
`runs-qemcmc.jsonl` (standalone cells) and `runs-flux-*.jsonl` (flux cells);
this file is the campaign-level record those files do not carry.

## Apparatus

- Cluster: 2x hpc7g.16xlarge (64 cores each), us-east-1, terraform in
  flux-tutorials `tutorial/tf-quantum`, booted 2026-11-26.
- Device: Rigetti Cepheus-1-108Q, us-west-1 (the only ONLINE QPU on Braket
  during the campaign; IonQ Forte-1 was PAUSED).
- Device execution windows (read from deviceCapabilities 2026-11-27,
  UTC): Everyday 00:00-02:00, 04:00-14:00, 16:00-24:00. Outside those
  the device reports PAUSED and queues submissions instead of running
  them, so a run started in a gap stalls and dies by per-task timeout.
  Any cell claiming anytime-robustness means any time INSIDE a window.
- Workload: quantum-enhanced MCMC (Layden et al., Nature 619, 2023),
  `examples/apps/qemcmc.py`, run by `forte/qemcmc.py` (flux-quantum-experiments).
  6 qubits, trotter 2, temperature 1.0, 100 shots/step unless noted.
- Software state on the cluster, fixed for the whole campaign:
  - flux-sched branch `hold` (coschedule queue policy).
  - jobtap plugin quantum.so loaded; `preempt` NOT enabled (conf has no
    preempt keys; `preempt_after` was removed upstream and rejected at load).
  - `sched-fluxion-feasibility` UNLOADED. Its graph cannot learn populated
    qdevices on this fluxion build (no `feasibility.add_subgraph`), so it
    refused quantum submits as unsatisfiable. Consequence while unloaded:
    no early unsatisfiable rejection; placement is unaffected. The fix
    (handler in flux-sched 764927db + populate dual-send + smoke step 9)
    is committed locally and takes effect on the next boot.
  - Credentials: EC2 instance role with AmazonBraketFullAccess (attached
    live 2026-11-26; `main.tf` carries the attachment for future boots).

## Cells executed

### C0 - local exact chain (2026-11-26, record 0)

`qemcmc.py local --qubits 6 --steps 2000`. Free. Validates the model and
sampler: total variation to Boltzmann 0.0489, acceptance 34.75%.

### C1 - SV1 plumbing rehearsal (2026-11-26, record 1)

baseline arm, SV1 simulator, 4 qubits, 10 steps. Validates task submission,
result parsing, record writing. Loop overhead ~5.7 s/step (no queue on a
simulator). Cost: cents.

### C2 - floor pair, 0 fillers (2026-11-26, records 2-3)

STANDALONE MODE (see deviation note below). baseline then coschedule,
Cepheus, 20 steps. Measures the hold's fixed cost against an idle queue.
Hold open: 111.2 s (6 s queued as a hybrid job, ~105 s container start).

### C3 - contended pairs, 5 and 10 fillers (2026-11-26, records 4-7)

STANDALONE MODE. Fillers are 10-shot tasks kept topped up in the public
queue by the run itself; both arms see the same contention. Ran unattended
in tmux, verified: chain task observed queued at position 6 behind 5
fillers; fillers drained and paid only when executed.

| fillers | baseline steps/hr | coschedule steps/hr | speedup | hold open s |
|--------:|------------------:|--------------------:|--------:|------------:|
|       0 |             197.6 |               211.9 |    1.07 |       111.2 |
|       5 |             143.2 |               379.5 |    2.65 |       111.6 |
|      10 |             113.0 |               329.0 |    2.91 |       111.8 |

Interpretation: the hybrid-job hold neutralizes public-queue contention;
at zero contention the 111 s open roughly pays for itself over 20 steps.
Break-even: the hold wins when the queue it skips costs more than ~2 min.

### Deviation note - C2/C3 ran standalone, not through flux

The runner opened the hold itself with the same backend calls the scout
makes, so the device-side physics (queue waits, hold behavior, steps/hr)
transfers. What standalone does NOT measure is the scheduler: when cores
are allocated and freed. C2/C3 therefore serve as the mechanism ablation
(hold vs no hold, scheduler out of the loop); the demonstration that flux
delivers the win to an unmodified job is C4, which was always required and
should have been run first. Acknowledged and corrected by C4's design.
Superseded 2026-11-27: C2f/C3f (below) repeat these cells through flux
with the identical filler mechanism, closing the gap this note records.

## Cells remaining

### C4 attempt 1 - FAILED (2026-11-27, on the 2026-11-26 cluster)

fluxbatch.sh passed its state check, mock rehearsal and cost gate. Arm
A's four jobs completed AS FLUX JOBS but the chains inside never
reached Braket (no device activity observed; likely spend $0; the job
logs were lost with the cluster, so the in-job failure is undiagnosed -
the script treated "job finished" as "job succeeded", a gate hole now
closed). Arm B's first pair was then refused at submit: "flux quantum:
no usable quantum vendor. braket: not available". Root cause of the
refusal: the CLI's braket probe opens AwsDevice on
the regionless SV1 ARN; the submit environment carried no default AWS
region, the probe raised, and the selector reported "not available"
while DISCARDING the exception detail. The standalone runs never hit
this (paths.py pins regions explicitly); the mock rehearsal never hit
it (mock does not probe AWS); this was the first braket submit ever to
pass through the CLI, and the state check did not exercise it.

Fixes, all committed before C4 attempt 2: the selector now logs the
probe detail; fluxbatch.sh exports AWS_DEFAULT_REGION, its state check
runs the CLI's own vendor resolution and prints the reason on refusal,
a one-step canary pair (~$1.50) must run inside its hold before either
arm spends, the mock phase no longer risks leaking FLUX_QUANTUM_MOCK
into the real arms (bash keeps a VAR=x prefix on a function call), and
an `armb` resume mode exists so a half-failed batch is not repaid, and
every arm job is now validated (flux result COMPLETED and the chain's
summary present in its log) instead of merely waited for.

The cluster was destroyed after the failure. No usable data came out of
attempt 1; its product is the three gate holes it exposed (vendor probe
untested, mock-leak hazard, completion mistaken for success), all closed
before attempt 2, which runs complete on the fresh boot - the
configuration the paper describes anyway.

### Boot 2 - FAILED (2026-11-27), BOOT-VERDICT passed falsely

The fresh boot printed BOOT-VERDICT: OK while running the AMI's stale
fluxion: flux-sched's cmake configure died because a depth-1 clone has
no tags, git-describe yields a bare hash, and PROJECT_VERSION comes out
empty ("set_target_properties called with incorrect number of
arguments" at resource/CMakeLists.txt). make/install were then no-ops.
Separately, populate, the verify step and the feasibility probe all ran
before rc1 finished loading fluxion (the broker answers module list
before its modules are loaded), so populate failed lastingly and the
probe silently skipped. The verdict checks live state at the end, by
which time fluxion and the jobtap plugin had loaded, so it saw nothing.
Found by the smoke test (step 4 refused the pair as unsatisfiable).

Fixes: the boot writes flux-sched.ver before cmake; rank 0 waits for
sched-fluxion-qmanager to actually appear before anything scheduler-
dependent; the verdict now fails on a stale installed feasibility
module (no feasibility.add_subgraph symbol) and on a graph without
qdevice_braket; populate's feasibility warning moved to stderr (it
was polluting flux submit's stdout, which carries the jobid).

### Boot 3 - FAILED, root cause found and reproduced (2026-11-27)

Boot 3 printed BOOT-VERDICT: FAILED: populate (the hardened verdict
working), and the smoke test refused the pair: the scout was denied
alloc as "unsatisfiable" 23 ms after submit, while step 9 PASSED - the
feasibility module was loaded, correct, and agreed the mock scout was
feasible. Only qdevice_ibm was in the scheduler graph, and boot
populate had failed with EEXIST for all four vendors.

Root cause, reproduced locally in a throwaway 2-broker instance with
no AWS involved: EVERY node runs the boot script and populate RPCs to
rank 0, so two populates raced one graph. Both read it before either
wrote, both computed the same next vertex id, and the loser's add
failed with fluxion's "error inserting an edge to outedge metadata
map: cluster0 -> qdevice_X<id>" - after inserting the vertex, before
its containment edge. fluxion does not roll that back. The graph then
holds a qdevice that find reports and the traverser cannot reach, so
populate's own present check says there is nothing to do while every
scout for that vendor is refused as unsatisfiable. A second shape of
the same race leaves a vertex find cannot see, so populate retries it
forever and keeps failing EEXIST.

Fixes: the boot populates from rank 0 only (root cause); populate
refuses a graph with unreachable qdevices and names the repair, and
verifies after its own add instead of trusting the RPC's success (two
new unit tests, plus a real-fluxion check that healthy graphs are not
flagged - the first version of the check had a false positive because
fluxion's find writer emits edges with no metadata); and
grow_resource_db_rv1exec returned -1 on its already-loaded no-op path,
which would fail every later resource.acquire update carrying
resources and skip marking ranks up (flux-sched, one line).

Repair for a live cluster in this state, no rebuild: `repair.sh` in this
directory, in two phases because the restart happens per node.

    bash repair.sh pre                    # lead: stop+disable the queue,
                                          # cancel, drain, report idleness
    sudo systemctl restart flux.service   # EVERY node, followers first
    bash repair.sh post                   # lead: prove idle, populate
                                          # once, verify, reopen the queue

`pre` stops the queue before the restart on purpose: `flux queue stop` is
checkpointed, so the jobs flux restores from the KVS on the other side
(`-Scontent.restore=auto`) cannot allocate anything before `post` has
populated. `post` refuses to populate unless fluxion reports zero
allocated cores and zero active jobs, and calls populate a second time as
an independent check that the grown graph is still reachable.

The first repair attempt proved why the drain matters. After the
restart, flux restored the previously queued jobs from the KVS - the
leftovers from the failed smoke tests - they allocated cores, and
populate grew the graph while they held planner spans. Steps 4-7 of
the smoke test then PASSED (the pair ran, the scout opened the session
and the released=1 memo reached the classical's eventlog, so
coscheduling itself works), but step 8 caught the damage: "run_remove:
planner_multi_rem_span returned -1", "cancel_vertex failed on
cluster0", then "run_sched_loop: Invalid argument" - the corruption
populate's own docstring warns about, with the scheduler wedged after
it. populate's idle check only sees the instant it runs, so a job
allocating around it slips through; the operational answer is to drain
and stop the queue before populating. fluxbatch.sh check now also
submits a trivial job and greps for planner/sched-loop errors, so a
wedged scheduler is caught before any spending.

The gate earned itself on 2026-11-27 ~04:05 UTC. With Cepheus back
ONLINE, `fluxbatch.sh check` refused with `fluxion-planner-errors`: the
01:54 planner/sched-loop errors were still in the ring buffer, which
only clears on a broker restart, so the repair had not been done. The
trivial probe job PASSED in the same check - a job allocates and reaches
clean while fluxion's own free fails underneath, leaking planner spans -
so the dmesg grep, not the probe, is what catches this state. Repaired
with `repair.sh` before any spending.

### C4 attempt 2 (2026-11-27 04:10 UTC): FAILED in arm A, cluster destroyed

The repaired cluster passed every gate: state check OK (device ONLINE,
dmesg clean), 4/4 mock rehearsal, cost confirmed ($3.54/job read live),
and the paid canary ran its 1-step chain inside its hold end to end. Then
**baseline 1 - a job in the FREE-of-quantum-risk arm - failed instantly**:
`result=FAILED`, and its log did not exist on the lead
(`tail: cannot open .../qb1.log`). The EXIT trap cancelled the rest;
verdict DEGRADED (0/4). Money lost: the canary (~$1.50 + one task fee).

Root cause: `fluxbatch.sh` submitted arm A with
`--output=$EVID/qb$i.log`, and `$EVID` is `$HOME/fluxbatch-<timestamp>`
**on the lead only**. flux's shell opens `--output` on the node the job
runs on; baseline 1 was scheduled onto the second node, where that
directory does not exist, so the open failed and the job died before
running anything. The canary and arm B never see this because they read
output back from the KVS with `flux job attach`. The mock rehearsal never
sees it because it rehearses `sh -c echo`, not file paths and not the app.

Fixes, all in `fluxbatch.sh`:

1. Arm A writes stdout to the KVS like arm B; the script attaches after
   the wait. No job is ever handed a lead-local path again.
2. New free gate before any spending (phase 1b): the REAL app entrypoint
   (`qemcmc.py estimate`, which exercises the interpreter, the imports,
   boto3, the region and `$OUT` writability) runs pinned to EVERY rank
   via `--requires=ranks:N`. A node that cannot run the app fails the
   batch while it is still free.
3. Evidence collects the per-node `runs-flux-*.jsonl` from every rank
   through `flux exec` into `runs-flux-rank<N>.tar`, plus `flux dmesg`
   and the scheduling graph, so nothing has to be fetched by hand.

### C4 attempt 3 (2026-11-27 06:15 UTC): DEGRADED 3/4, AWS quota, not us

First run on a fresh boot with the static scheduling graph, and the
design held: every gate passed (state check, mock rehearsal, per-rank
app rehearsal, cost confirm, canary), all of arm A completed, and pairs
1-3 ran their chains inside their holds (`credential from the scout:
yes` in qc1-qc3). Pair 4's classical was CANCELED by its scout:

    opening braket session failed: An error occurred
    (ServiceQuotaExceededException) when calling the CreateJob operation:
    You have 3 active hybrid jobs. Maximum allowed concurrent hybrid
    jobs is 3.

Root cause: the AWS account quota on ACTIVE hybrid jobs is 3, and
QUEUED and CANCELLING jobs count, so with 4 scouts holding concurrently
(the graph advertises 4 qpus) the fourth CreateJob is refused outright
- the quota does not queue. Nothing on the cluster misbehaved; the
verdict DEGRADED (3/4) is the script working as designed. Evidence in
`fluxbatch-20261127-061540` on that lead.

Fixes, committed before attempt 4:

1. The scout now waits a quota refusal out, bounded (default 3600 s,
   `FLUX_QUANTUM_BRAKET_QUOTA_WAIT`), the same way it waits for device
   priority: a refused CreateJob is just the back of the line. Only
   `ServiceQuotaExceededException` is retried; everything else still
   aborts the pair immediately. Capping concurrency at 3 instead would
   not have been enough: a just-released hold lingers in CANCELLING and
   keeps its slot for a while.
2. Hold job names (and so their S3 token prefixes) carry a random
   suffix: two scouts starting the same second previously shared a
   prefix and could read each other's tokens.
3. `fluxbatch.sh check` reports the account's active hybrid jobs and
   warns when the quota is already full.

Optional, user-side: request a quota increase to >=5 at the Service
Quotas console (the error message links it); with the wait in place it
is a nicety, not a requirement.

### C4 attempt 4 (2026-11-27 06:46 UTC): OK 4/4 - C4 COMPLETE

The headline cell landed. Full transcript tail: canary OK (pair ran its
chain inside its hold end to end), arm A complete, arm B 4/4 with
`credential from the scout: yes` in qc1-qc4,

    BATCH VERDICT: OK (4/4 pairs ran inside their hold)

Evidence: `fluxbatch-20261127-064610` on that lead - job table, every
eventlog, dmesg, the scheduling graph, credential lines, and the
per-node step records (`runs-flux-rank*.tar`), self-contained. The run
followed the quota fix (committed 06:40 UTC as 6f3ec3a) by minutes.

[TODO user, one line: was this a fresh boot, or the attempt 3 cluster
with the fix pulled? C4 does not depend on the answer, but C5's
"second fresh boot" bookkeeping does.]

Measured (from the eventlogs and job summaries, 2026-11-27):

| | arm A (plain) | arm B (coscheduled) |
|---|---|---|
| steps/hr per job | 562, 519, 519, 563 (mean 541) | 555, 556, 553, 542 (mean 552) |
| classical core-s (alloc->free x4) | 280+300+300+279 = 1158 | 282+281+284+289 = 1136 |
| scout core-s | - | 3682 (182 s x3 + 375 s, x4 cores) |
| batch makespan | 80 s | 382 s |

Reading: the device queue was EMPTY at 06:50 UTC (arm A task
turnaround 3.2-3.8 s, self-contention only, max queue position 3), so
this cell measured the UNCONTENDED regime: identical per-job steps/hr,
near-identical classical core-seconds, and the coscheduling overhead
fully visible - scouts hold ~110 s of hybrid-job spin-up each plus the
serialized holds (arm B makespan 4.8x arm A). The expected signature
(baseline arm burning core-seconds waiting in a deep public queue)
needs a contended queue; C5 should target one (early execution-window
hours have been quiet; the September probes' busy periods are in
probes/probe-hold.log).

Pair 4's scout hit the hybrid-jobs quota and waited it out - nine
"quota is full, waiting for a slot" lines in scout4.log, then its slot,
then a normal pair. The quota fix is production-verified; its wait
(~180 s) is in pair 4's scout alloc time (375 s vs ~182 s), excluded
from the chain's steps/hr.

Note for C5's bookkeeping: the scouts were submitted -c4, so scout
core-seconds are 4x what a 1-core scout would hold; either submit
scouts -c1 next cell or report scout overhead per-core.

The per-node step records (runs-flux-*.jsonl) came back truncated in
the first evidence copy - flux exec is a text channel and cut the raw
tar at its first NUL byte. Fixed in fluxbatch (base64 + the tar must
list); the jsonl were refetched from the live cluster by hand.

The protocol that ran, for the record:

Protocol: `fluxbatch.sh` in this directory, run on the lead broker. One
script, gated: (0) cluster state check, refuses to proceed on any
mismatch; (1) free 4-pair mock rehearsal, gates on all classicals running
with a session; (1b) free rehearsal of the real app entrypoint on every
rank; (2) cost estimate shown, explicit confirmation; (2b) paid 1-step
canary pair that must run inside its hold; (3) arm A: 4 plain
`flux submit` jobs, baseline arm, mutual contention on the public
queue, cores held throughout; (4) arm B: 4 quantum pairs via
`flux submit --quantum-vendor braket`, scout opens the hold, classical
released into it; (5) evidence: every job eventlog, job table, credential
lines, per-node records, collected into one timestamped directory.

Parameters: 4 jobs/arm, 10 steps, 6 qubits, 100 shots, seeds 1-4, no
artificial fillers (the jobs contend with each other). Cost from the
device's live pricing, read by the script at phase 2: Cepheus
`per_task_usd` $0.3425 ($0.30 task fee + 100 shots at ~$0.000425), so
$3.54 per 10-step job and ~$30 for both arms - not the ~$115 estimated
before the price was read. Measured quantities: per-job steps/hr (from records),
core-seconds held per job and batch makespan (from flux eventlogs) - the
scheduler-side numbers standalone mode cannot produce.

Expected signature: coschedule arm holds far fewer core-seconds (cores
allocated only after each hold is ready) at similar or better per-job
steps/hr; `credential from the scout: yes` in all four pair logs.

## Design change (2026-11-27): the static scheduling graph

Every scheduler corruption this campaign hit came from mutating a live
fluxion graph with `add_subgraph` (Boot 3's racing populates; the wedge
from growing the graph while restored jobs held planner spans). The
mutation is now gone entirely:

- The full graph - real nodes and cores from R, plus the
  `qdevice_<vendor> -> qpu` vertices - is generated ONCE at boot by
  `flux python -m flux_quantum.schedjgf`, written to
  `/etc/flux/system/scheduling.json`, and handed to the broker with
  `[resource] scheduling = ...`. fluxion receives it whole through
  `resource.acquire` at startup and nothing ever mutates it.
- `populate`, the CLI plugin's populate-during-submit, the rank-0 boot
  guard, the idle check and repair.sh's populate phase are all deleted. A
  vendor not in the graph is refused at submit with a message, never
  added. repair.sh survives only as generic wedge triage.
- `sched-fluxion-feasibility` stays loaded: it bootstraps its own graph
  from the notify stream, which carries the initial graph, qdevices
  included. The `feasibility.add_subgraph` feeding existed only because
  notify never carries GROWN subgraphs, and nothing grows any more.
- NOT `[sched-fluxion-resource] load-file`: with it set the module never
  subscribes to resource.acquire (no rank up/down marking ever) and
  notify refuses with ENODATA.

Validated for free in a local 2-broker instance against the `hold` build,
`tests/integration/test-static-graph.sh` (all steps PASS, 2026-11-27):
fluxion's JGF reader accepts the generated graph with ranks matching the
broker's R; jobs allocate and free repeatedly on every rank with zero
planner errors; a mock pair runs end to end through `flux submit`; the
feasibility module bootstraps itself and answers both ways (configured
vendor feasible, unknown vendor refused). Those were exactly the two open
validations plus the feasibility payoff.

### C2f/C3f attempt 1 (2026-11-27 18:01 UTC): 6/6 ran, regime INVALID

All six cells completed (CELLS VERDICT OK) and the evidence is clean,
but the sweep did not measure queue contention. The device queue was
empty (`device_before` normal: 0 before every cell) and the device
drained each 5/10-filler top-up before the chain's next step, so no
standing depth ever existed. What both arms paid for instead was the
FillerPool's own bookkeeping: `alive()` re-described EVERY filler ever
submitted, every step - one API round-trip each - so non-task time grew
with the very knob the sweep turns (250 s at 5 fillers, ~500 s at 10,
on a ~100 s chain, equally in both arms). Speedups came out ~1.0x.

What attempt 1 DID establish, from the task records: **the hold works
through flux.** Every coscheduled chain task carries `in_hold: true`
with the scout's job ARN, sat at queue position 2, and turned around in
1-4 s while fillers were queued. The credential plumbing
(scout -> environment -> task submission) is verified under flux; only
the speedup table remains to re-earn.

Two corrections, committed:

1. FillerPool.alive() now retires terminal fillers instead of
   re-describing the full history (drain still accounts for all of
   them). Beyond removing the overhead, this restores the contention
   window itself: top_up is now fast enough that the chain's task
   arrives while its fillers are still queued.
2. For the paper: the standalone C3 speedups (2.65x/2.91x) are partly
   this same overhead ASYMMETRY - the baseline arms carried 100+
   described fillers per step, the coschedule arms ~15 - so those
   columns overstate the queue-jump. The C2f/C3f attempt 2 numbers,
   taken with the fixed pool in both arms, supersede them.

Attempt 2 is the same `fluxcells.sh run` with the fix pulled onto the
cluster. Same budget (~$150, baseline fillers dominate). Expected:
baseline steps/hr falls with filler depth (real waits, ~3 s of device
time per queued filler), coschedule stays near the 0-filler floor.

### C2f/C3f attempt 2 (2026-11-27 20:12 UTC): 6/6 ran, floor measured, NO queue effect - and why

With the fixed pool, 6/6 chains ran and the bookkeeping fell to
submission time alone (non-task time 172 s vs attempt 1's ~500 s at 10
fillers). The speedups are still ~1.0x at every level (0.99/0.98/0.97),
and this time the reason is physical, proven by the task records: the
baseline's chain tasks entered the queue at POSITION 2-3 behind 10
nominally queued fillers, with 0.7-1.0 s turnarounds. An idle Cepheus
executes a 10-shot task in about a second - faster than tasks can be
submitted one at a time - so serial submission cannot build standing
queue depth, with any pool. There was no queue for the hold to skip, in
either arm.

Floor numbers (valid, consistent with C4/C5): baseline 556.6 steps/hr,
coschedule 551.9 at 0 fillers - the hold costs nothing in steps/hr.
Evidence: `fluxcells-20261127-201228`.

Consequence for the record: the standalone C3 speedup table
(2.65x/2.91x) does not reproduce and is now attributed to the two
artifacts in the apparatus it was taken with - the pool's asymmetric
bookkeeping (attempt 1's finding) and whatever external queue depth
happened to exist that evening. Those columns must not be used in the
paper. The defensible claims as of attempt 2: the hold works through
flux (attempt 1's task records), it is free at zero contention (this
attempt, C4, C5), and the queue-jump itself is demonstrated by the
September priority race (probes/priority-race.jsonl).

Fix for attempt 3, committed: FillerPool.top_up now submits each batch
CONCURRENTLY, so a burst of N fillers is in the queue at the same time
when the chain's task arrives - transient real depth (~N seconds' worth
at ~1 s/task service) made to order, on any device state. Budget
unchanged (~$150); the baseline arm still executes most fillers.
Attempt 3 is the user's call: the quantitative speedup table is the
only thing it buys.

### C2f/C3f attempt 3 (2026-11-27 21:03 UTC): the gate REFUSED, $5 not $150

The depth-probe gate (new in this attempt) burst 10 fillers
concurrently and submitted a probe task: deepest observed position 4,
finished in 2 s. The run refused before the cells, having spent the
canary (~$1.50) and the probe (~$3.50). Conclusion, now measured three
ways at three times of day: an idle Cepheus absorbs small-task bursts
faster than any client-side mechanism can queue them, so the filler
dose-response sweep is UNRUNNABLE against this device unless real
external load exists. The sweep stays armed and gated: it is safe to
attempt for ~$5 whenever the device looks busy, and it will only
proceed when the premise actually holds.

For the paper this replaces the dose-response table with a finding:
hold-based coscheduling is free at zero contention (C4, C5, the 0-filler
floors), the hold mechanism works through flux (attempt 1's task
records), the queue-jump is real under genuine load (September priority
race), and contention could not be manufactured to order on an idle
108-qubit device in November 2026. Evidence: fluxcells-20261127-210338.

### C2f/C3f - the mechanism cells repeated THROUGH FLUX

C2/C3 ran standalone (deviation note above): the mechanism numbers are
sound but the scheduler was out of the loop. `fluxcells.sh` repeats them
with every measured job submitted through flux - baseline as a plain
`flux submit`, coschedule as a real scout+classical pair - while the
contention mechanism stays IDENTICAL to standalone: the app's own
FillerPool (`qemcmc.py --fillers N`), 10-shot public-queue tasks topped
up before every step, cancelled at the end. 20 steps, seed 1 everywhere,
levels 0/5/10, sequential cells with 60 s of clear between them, one
hybrid job open at a time (no quota contention). Gated like fluxbatch:
state check, free mock pair, free app rehearsal on every rank, per-level
cost estimate with a typed yes, 1-step canary.

New quantities this earns over standalone C2/C3: alloc->free
core-seconds and eventlogs per cell - under REAL device contention,
which C4 attempt 4's empty queue could not provide. Expected: the
standalone steps/hr table reproduces (same mechanism), and the
coschedule arm's classical core-seconds stay flat with filler depth
while the baseline arm's grow with its queue wait.

Budget, from the standalone records (runs-qemcmc.jsonl): in a BASELINE
arm every filler executes - the chain waits behind them, so each of the
20 step boundaries tops the pool back up (100 executed at level 5, 210
at level 10, ~$0.30 each). Coschedule arms execute only 10-20. Realistic
total ~$150: ~$41 of chain tasks, ~$95 of baseline-arm fillers, ~$10 of
coschedule fillers and holds, $1.50 canary. The per-level estimate is
printed at run time; the baseline filler bill IS the contention being
measured, not an overrun.

### C5 (2026-11-27 17:46 UTC): OK 4/4 - COMPLETE

Second fresh boot, evening window (C4 ran at 06:46). BATCH VERDICT OK,
4/4 pairs inside their holds, and the numbers replicate C4 within noise:

| | C4 (06:46) | C5 (17:46) |
|---|---|---|
| arm A steps/hr mean | 541 | 529 |
| arm B steps/hr mean | 552 | 535 |
| arm A core-s / makespan | 1158 / 80 s | 1179 / 78 s |
| arm B classical core-s / makespan | 1136 / 382 s | 1167 / 362 s |
| arm B scout core-s | 3682 | 3545 |

The queue was again near-empty (depth 1), so C5 replicates C4's
uncontended regime rather than adding a contended one; the contended
numbers come from C2f/C3f. Pair 4's scout again waited out the
hybrid-jobs quota (~180 s) and completed - second production exercise
of the quota wait. Evidence: `fluxbatch-20261127-174633`.

### C5a - feasibility module verification (OBLIGATION, carried from C3)

The module was unloaded on the 2026-11-26 cluster because its graph could
not learn the qdevices. With the static scheduling graph it bootstraps
itself from notify (verified locally, test-static-graph.sh step 6); what
remains is the on-cluster proof. On the next fresh boot:

1. Boot must leave sched-fluxion-feasibility LOADED (the start script no
   longer removes it; there is nothing to probe).
2. Run `tests/integration/test-system-instance.sh`. Step 9 must PASS both
   ways: a configured vendor's scout feasible, an unknown vendor refused
   (ENODEV/EINVAL). A SKIP on this boot is a FAILURE of the boot, not a
   pass. Paste the step 9 output into this file under this heading.
3. The quantum submits then exercise the check in production; any
   "Unsatisfiable" on a configured vendor reopens the bug.

Until step 9 has passed on a live cluster, the feasibility behavior is
considered unverified there, whatever the local tests say.

### C6 (optional) - IonQ Forte-1 short chain, if it leaves PAUSED

5 steps, no fillers, ~$8.30/task. Only if budget allows.

## Analysis plan

Per arm: steps/hr conditioned on recorded queue state (`device_before`),
core-seconds held (eventlog alloc->free), hold-open distribution. Figures:
steps/hr vs contention (C3 table + C4), core-seconds held per arm (C4).
Raw jsonl + eventlogs archived (Zenodo) at publication. Venue: ISC 2027
(abstracts 2026-12-14, papers 2026-12-21).
