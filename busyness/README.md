# busyness/ - coscheduling under sampled busy-ness distributions

The paid campaign (forte/) established that today's devices are often
idle: three attempts could not manufacture queue contention against a
live Rigetti machine, because it serves small tasks faster than any
client can queue them. This experiment makes the forward-looking
argument instead: **queues have been observed busy, they will be busy
again, and coscheduling's value scales with how busy they are.** The
device here is the mock vendor, so every run is free, repeatable, and
statistically sized.

## Where busy-ness comes from

Not single values. Each level is a **distribution over queue depth**,
and every trial draws its depth from that distribution:

- The anchor is `queues/results/distribution.json`: 42 days, ~52,000
  one-minute observations per device, of real vendor queues. IonQ
  Forte-1 through Braket is the default anchor because its observed
  distribution already has the shape of the argument - median 2 tasks,
  p90 = 246, max = 593. Busy is not hypothetical; it was measured.
- Levels scale that empirical distribution: `idle:0` (control),
  `today:1` (the 42 days as observed), `x2`, `x5`, `x10` (the same
  shape, denser - the "at some point there WILL be contention" axis).
  The shape comes from data; only the density is extrapolated.
- `scripts/sample.py` draws the plans with a required `--seed`, records
  full provenance in `plans/meta.json`, and the plans are committed
  before the runs: a pre-registered sweep, not depths chosen to flatter
  a figure.

## Design, for robustness

- **Paired draws.** Both arms of a trial replay the *same* sampled
  depth, so each trial yields one paired difference and the
  between-draw variance (which is the distribution's tail, i.e. huge)
  cancels out of the contrast.
- **Interleaved arms.** Arm order is shuffled per trial (inherited from
  mock/scripts/experiment.sh) so drift cannot bias an arm.
- **20 trials per level** by default; free, so raise it at will.
- **Medians with 95% bootstrap CIs** in the analysis; censored
  (timed-out) trials are counted, never dropped silently.
- **Service time is an assumption, stated and sweepable.** The queues
  data gives depth, not seconds per queued task (drain rates only bound
  it below). The default SERVICE=0.05 s/task keeps wall-clock small;
  the held-core-seconds contrast is linear in it, so report results
  against the vendor wait in seconds and rerun with SERVICE=0.1/0.2 as
  a sensitivity check.

## Arms (same as mock/)

- **baseline**: one job takes its cores, opens the session, and waits
  out the vendor queue *while holding them*.
- **coscheduled**: the classical job is submitted held, a one-core
  scout opens the session and waits, and the classical is released the
  moment the QPU is ours.

Measured per trial, from eventlog timestamps only: held core-seconds
per arm (scout included), release latency, vendor wait. The headline
figure is **core-seconds returned to the cluster vs busy-ness level**,
with the paired CI.

## Run it

```bash
# on a coschedule-policy instance with the flux-quantum CLI plugin
bash run.sh                       # sample plans, all levels, analyze; ~2 h
TRIALS=5 bash run.sh              # quick pass
DEVICE=Cepheus LEVELS=today:1 TRIALS=50 bash run.sh   # variations
```

Results land in `results/<level>.csv` (raw timestamps),
`results/summary.txt` (the paired table), with the plans and
`plans/meta.json` beside them.
