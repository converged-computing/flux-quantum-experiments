# RUNBOOK: the remaining campaign on a fresh cluster, start to finish

The record of what happened and why is PROTOCOL.md; this file is only the
procedure. Every step is a checked-in script with its own gates - nothing
here asks for loose commands to be composed by hand. Steps marked GATE
refuse to continue on failure; do not override a gate.

Remaining cells: C2f/C3f attempt 2 (`fluxcells.sh run` with the
FillerPool fix pulled onto the cluster, ~$150), C6 (optional). COMPLETE:
C4 (attempt 4, 06:46 UTC, OK 4/4), C5 (17:46 UTC, OK 4/4, replicates
C4). C2f/C3f attempt 1 ran 6/6 but measured pool bookkeeping, not
contention - see PROTOCOL.md. C5a needs the step 9 output from a boot
pasted into PROTOCOL.md if not yet done.

C2f/C3f and C5 can share one boot: run `fluxcells.sh run` after C5's
`fluxbatch.sh run`, inside a device window (the cells take ~1.5-2 h and
the contended baselines are the slow part). Both scripts gate
themselves; the order between them does not matter scientifically, but
fluxbatch first means the expensive cells only run on a boot that has
already proven itself end to end.
Everything below is one boot's worth; C5 is this same runbook a second
time at a different time of day.

## 0. Before terraform, on the workstation

Push the four repos so the boot clones the fixed code (the boot script
clones flux-quantum `braket-hybrid`, this experiments repo, and rebuilds
flux-sched `hold` from the fork). This repo holds the campaign; the
flux-quantum repo is software only.

```bash
git -C /workspaces/flux-quantum push origin braket-hybrid
git -C /workspaces/flux-quantum/flux-quantum-experiments push origin main
git -C /workspaces/flux-quantum/flux-sched push origin hold
git -C /workspaces/flux-quantum/flux-tutorials push origin flux-quantum
```

The `main.tf` Braket IAM attachment (AmazonBraketFullAccess) is
uncommitted in flux-tutorials and is yours to commit and apply; the boot
does not depend on it being in git, only on it being applied.

Pick the window. Cepheus executes 00:00-02:00, 04:00-14:00 and
16:00-24:00 UTC; outside a window it reports PAUSED, queues submissions,
and chains die by timeout. `fluxbatch.sh check` refuses a non-ONLINE
device, so starting inside a window just avoids a wasted boot. Budget:
~$30 for both arms plus ~$1.50 canary, from live pricing re-read at run
time.

The AWS quota on concurrently ACTIVE hybrid jobs is 3 (QUEUED and
CANCELLING count; this is what degraded C4 attempt 3 to 3/4). The scout
now waits a quota refusal out, so nothing more is required - but an
increase to >=5 (Service Quotas console, Braket, "Concurrent hybrid
jobs") removes the wait entirely if AWS grants it in time.

## 1. Deploy

```bash
cd /workspaces/flux-quantum/flux-tutorials/tutorial/tf-quantum
make init && make build     # terraform apply; 2x hpc7g.16xlarge us-east-1
```

Boot is unattended (user data = start-script.sh). It writes
`/etc/flux/system/R`, generates the static scheduling graph from it
(`flux python -m flux_quantum.schedjgf ibm braket mock ionq --qpus 4`)
into `/etc/flux/system/scheduling.json`, points the broker at it with
`[resource] scheduling`, rebuilds flux-sched `hold`, installs
flux-quantum, clones this experiments repo to
`/opt/flux-quantum-experiments`, and starts flux. There is no populate step anywhere: the
graph is complete before fluxion loads and is never mutated. The
feasibility module stays loaded and bootstraps itself from notify.

## 2. GATE: the boot verdict, on each node

```bash
grep BOOT-VERDICT /var/log/cloud-init-output.log
```

`BOOT-VERDICT: OK` on the lead or the boot failed; the line names what
broke (`scheduling-graph` means the generated graph did not reach
fluxion). Do not continue past a failed verdict.

## 3. GATE: the system-instance test, on the lead

```bash
export FLUX_URI=local:///home/ubuntu/run/flux/local
bash /opt/flux-quantum/tests/integration/test-system-instance.sh
```

Must exit 0 with step 8 clean and step 9 PASS both ways. Step 9 passing
discharges C5a on this boot - paste its output into PROTOCOL.md under
C5a. A step 9 SKIP means feasibility is not loaded: that is a boot
failure on this design, not a pass.

## 4. GATE: the batch preflight, on the lead

```bash
bash /opt/flux-quantum-experiments/forte/fluxbatch.sh check
```

Must end `state check: OK` with `device status: ONLINE`. This checks the
broker, fluxion, the jobtap and CLI plugins, feasibility, the qdevices
reachable in the graph, a trivial job reaching clean, a clean dmesg,
Braket credentials, and the device status.

## 5. C4 attempt 4, in tmux on the lead

```bash
tmux new -s c4
bash /opt/flux-quantum-experiments/forte/fluxbatch.sh run
```

1-2 h, ~$30. The script gates in spending order: free state check, free
4-pair mock rehearsal, free rehearsal of the real app entrypoint on
EVERY rank, live cost printed and confirmed with a typed `yes`, a ~$1.50
one-step canary pair that must run its chain inside its hold, then arm A
(4 plain flux jobs), then arm B (4 quantum pairs), then evidence. On any
failure the EXIT trap cancels everything still running (no orphaned
scout bills its hold) and salvages the evidence collected so far.

If it fails after arm A succeeded, resume with
`fluxbatch.sh armb` - it re-earns the rehearsal and the canary before
spending arm B's money.

During arm B, scouts beyond the hybrid-jobs quota print
`braket: the hybrid-job quota is full, waiting for a slot` and wait
(up to 1 h). That is normal serialization, not a failure.

## 6. Evidence, then destroy

Everything lands in `$HOME/fluxbatch-<timestamp>/` on the lead,
INCLUDING the per-node step records (`runs-flux-rank*.tar`, pulled from
every rank through flux exec), the eventlogs, dmesg and the scheduling
graph. Copy that one directory off the cluster:

```bash
scp -r ubuntu@<lead>:fluxbatch-<timestamp> ./evidence/
```

then

```bash
cd /workspaces/flux-quantum/flux-tutorials/tutorial/tf-quantum
make destroy
```

Record the cell's outcome in PROTOCOL.md while the transcript is fresh.

## 7. C5: do this runbook again

Fresh boot at a different time of day (inside a different execution
window), same steps 1-6. The second boot also re-proves the boot script
itself. C6 (IonQ Forte-1, 5 steps, ~$8.30/task) is optional, budget
permitting, and only if the device leaves PAUSED.

## If the scheduler ever wedges again

The known cause (growing a live graph) is unreachable now; a new wedge
is new information. Save `flux dmesg` first, then:

```bash
bash /opt/flux-quantum-experiments/forte/repair.sh pre    # lead
sudo systemctl restart flux.service                       # EVERY node, followers first
bash /opt/flux-quantum-experiments/forte/repair.sh post   # lead
```

`post` verifies the rebuilt instance (idle, dmesg clean, qdevices back
from the static graph) and reopens the queue; then rerun steps 3-4
before spending anything.
