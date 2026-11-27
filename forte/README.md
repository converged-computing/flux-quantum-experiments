# What coscheduling buys on IonQ Forte, through Braket and through IonQ

The same classical work, done with and without a hold on the device, on the
same machine reached two ways. Two numbers per trial say what the hold
bought:

- **cores held.** How long the classical job's cores were allocated. Without
  the hold they are held from the first quantum submit to the last result,
  queue included. With it they are held only once the device is ours.
- **task wait.** How long each of the classical's requests sat before the
  device served it, and whether the service says it was served inside the
  hold. With the hold the requests should be served as if the public queue
  were not there.

The classical submits K one qubit tasks back to back and waits for all of
them. Contention is N filler tasks put in the public queue before the
trial, without any hold, the same for both arms.

| arm          | what happens                                                                 | cores held from  |
|--------------|------------------------------------------------------------------------------|------------------|
| `baseline`   | the classical starts, submits K tasks to the public queue, waits              | first submit     |
| `coschedule` | the hold is opened first with cores free; once ready the classical submits K tasks into it | hold ready |

| path     | device                                              | hold `session`                                   | hold `probe`                       |
|----------|-----------------------------------------------------|--------------------------------------------------|------------------------------------|
| `braket` | `arn:aws:braket:us-east-1::device/qpu/ionq/Forte-1` | a hybrid job; its token puts a task in the Priority queue | one task waited to the queue head |
| `ionq`   | `qpu.forte-1`                                        | an IonQ session; refused unless `--allow-ionq-session` | one warm-up job waited to start |

Both are Forte-1, the same machine. A probe holds nothing a task can be
submitted into, so after a probe the K tasks go to the public queue like the
baseline. That arm says what being released at the queue head is worth on
its own, which on IonQ is the only hold that does not cost an hour of Forte.

The hold is opened and closed with the backend classes, with the same calls
the scout makes, so the standalone trial measures what the pipeline does.
Through flux the same script is the classical half of a pair and the job's
own eventlog gives the timings.

## Files

| file            | purpose                                                              |
|-----------------|----------------------------------------------------------------------|
| `trial.py`      | one trial, standalone or as the classical half of a flux job          |
| `paths.py`      | the two ways to the device behind one interface                       |
| `campaign.py`   | a grid of cells with repeats, resumable, total cost first             |
| `campaign.json` | the default grid: 2 paths x 2 arms x {0, 3} fillers x 2 trials         |
| `report.py`     | the table and the baseline against coschedule comparison              |
| `flux-run.sh`   | one cell through flux pairs on the cluster                           |
| `runs.jsonl`    | one JSON line per trial, appended, read by the report                 |

## Cost, before anything else

Every trial prints its estimate and asks. `--max-usd` refuses a trial over
a limit, 40 by default, and `estimate` prints the number without creating
anything.

```bash
python3 forte/trial.py estimate --path braket --arm coschedule --fillers 3
python3 forte/campaign.py forte/campaign.json --dry
```

What the numbers come from:

- **Braket Forte** bills $0.30 a task plus $0.03 a shot, read live from the
  device when it answers. A hybrid job's instance is $0.115 an hour. Fillers
  are cancelled once the trial's tasks are done, so queued shots are not
  paid for, but a filler that already ran is paid for.
- **IonQ direct** quotes about $25.79 for a small job on Forte, read live
  from `GET /jobs/estimate`. Every job counts, fillers and the probe
  included, so contention on IonQ is expensive and the default grid keeps
  it at 3 fillers.
- **An IonQ session** that runs a job bills an hour of Forte when it ends.
  The harness refuses one without `--allow-ionq-session`, and the campaign
  file leaves it off. Braket's hybrid job is the cheap way to hold Forte.

With the default grid, 100 shots, 3 tasks and 3 fillers, a Braket trial is
about $10 without contention and $20 with it. An IonQ trial is about 3
jobs, or 6 with fillers, at the quoted minimum each.

## Running standalone

Credentials in the environment: AWS credentials and a default Braket bucket
for the Braket path, `IONQ_API_KEY` for the IonQ path. flux is not needed.

```bash
python3 forte/trial.py run --path braket --arm baseline
python3 forte/trial.py run --path braket --arm coschedule --fillers 3
python3 forte/trial.py run --path ionq --arm baseline
python3 forte/trial.py run --path ionq --arm coschedule --hold probe --fillers 3
```

Options: `--tasks K` (3), `--shots` (100), `--fillers N`, `--filler-shots`
(100), `--max-seconds` (1800, the hold's limit and the wait for results),
`--device` to point at another device, `--record` for another file.

The whole grid, with repeats, resumable:

```bash
python3 forte/campaign.py forte/campaign.json
```

Cells already in `runs.jsonl` count toward the repeats, so a campaign that
stopped picks up where it left off. There is a pause between trials so one
trial's fillers are gone before the next begins.

## Running through flux on the cluster

Same cells, but the baseline is a plain `flux submit` and the coschedule arm
is a quantum pair, with `trial.py classical` as the classical half. The
fillers go in before the submit, since the hold has to find them queued, and
the record is annotated afterwards with the job's eventlog: when its cores
were allocated and freed, and when the scout started and handed the session
over. That is the measurement in flux mode.

```bash
export FLUX_CLI_PLUGINPATH=/opt/flux-quantum/cli-plugins
bash forte/flux-run.sh braket baseline
bash forte/flux-run.sh braket coschedule 3
bash forte/flux-run.sh ionq coschedule 3 probe
```

The record's `mode` is `flux` and `flux` holds the timings. The report
reads both kinds the same way.

## Reading the result

```bash
python3 forte/report.py
```

One row per cell, then baseline against coschedule per path and contention
level: cores held, device wait with cores free, task wait, and the fraction
of tasks the service says ran inside the hold. On Braket that is the task's
`jobArn`, and its queue, Priority or Normal, is recorded too. On IonQ it is
the job's `session_id`. A probe arm shows no tasks in the hold, which is
right.

Each line in `runs.jsonl` keeps everything: the device's queue before the
trial, the hold's open and ready times, every task's submit latency,
created, started and ended times with a timeline of what the service
showed, the fillers and what became of them, and the cost estimated and,
where the service reports it, charged.

## A real application as the workload

qemcmc.py runs quantum-enhanced MCMC (Layden et al., Nature 619, 282-287,
2023) through the same paths and arms: a sampling chain whose proposal is a
quantum circuit, one task per step, a classical Metropolis accept between.
It is the tightest hybrid loop there is, so the hold's worth shows up as
iteration rate, and every second a step queues is a second the classical
allocation idles.

```bash
python3 forte/qemcmc.py local --qubits 6 --steps 2000   # exact, free
python3 forte/qemcmc.py estimate --path braket --steps 20
python3 forte/qemcmc.py run --path ionq --arm coschedule --steps 20 --qubits 6
```

The app itself, the model, the circuits in both wire formats and an exact
statevector simulator, is examples/apps/qemcmc.py, standard library only;
it is a workload, so it lives with the example apps (the vendor-named
example directories hold per-backend integration examples), not in the
plugin. Runs append to runs-qemcmc.jsonl. The fake IonQ service simulates
whatever circuit it is sent, so a chain rehearsed against it does real
physics while the queues are modelled. Mind the per-step price on hardware:
a chain is steps x task_cost(shots), and the runner refuses estimates over
--max-usd without --yes.

## Rehearsing without a device

The IonQ path runs against the fake service, which models the public and
session queues:

```bash
FAKE_IONQ_QUEUE=30-60 FAKE_IONQ_SESSION_QUEUE=1-2 flux python -m flux_quantum.backends.ionq.fake --port 0
export IONQ_API_KEY=fake IONQ_API_URL=http://127.0.0.1:<port>
python3 forte/trial.py run --yes --max-usd 500 --path ionq --arm coschedule --hold probe --fillers 2 --tasks 2 --shots 10
```

The Braket path can be rehearsed on the SV1 simulator with
`--device arn:aws:braket:::device/quantum-simulator/amazon/sv1`, for cents,
which checks the plumbing and the token but not the queue, since a
simulator has none.

## What to expect, and what would be a finding

- **Braket session, with fillers.** Tasks with the token in the Priority
  queue, served ahead of the fillers, cores held for the work alone. The
  earlier probe runs showed the token travels and associates. This grid
  puts a number on the time it saves.
- **Braket probe.** Cores free while the probe waits, but the K tasks then
  queue behind whatever arrived, so the saving depends on the gap.
- **IonQ probe.** The same, through the other door. The interesting number
  is how the two paths' task waits compare under the same contention, since
  it is one machine with two queues in front of it.
- **IonQ session.** Only if the hour is worth it. The harness can run it.
