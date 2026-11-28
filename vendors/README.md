# vendors/ - one coscheduled pair per vendor

The smallest experiment that answers "does the handoff work against each
real vendor API": a single pair per backend. The scout opens the session,
waits out whatever queue exists, memos the session id onto the held
classical job, and releases it. One row per vendor in `results/pairs.csv`:

    vendor, device, eventlog timestamps, vendor_wait_s, release_lat_s, session

plus the scout's full log (`results/scout-<vendor>.log`, the queue-position
series and session id) and the classical job's eventlog. This is mechanism
verification, not performance: no contention is needed, an idle device
proves the handoff fine.

## Run it

```bash
bash run.sh                        # mock only, free, the rehearsal
VENDORS="mock braket" bash run.sh  # braket defaults to the SV1 simulator (cents)
VENDORS="braket ionq ibm" bash run.sh
```

Paid vendors run only when named in `VENDORS`, one pair each. Defaults are
the cheapest device per vendor. On braket, ionq and ibm the released
classical runs one real QeMCMC step (`forte/qemcmc.py run --arm classical`)
with the scout's credential, so the row shows a circuit executing inside
the handoff, not a sleep; the step's record lands in
`results/qemcmc-<vendor>.jsonl`. Cost per step at the default 100 shots:
braket->Forte-1 $8.30, ionq direct $25.79, ibm ~$3.30, braket->SV1 cents.

Knobs: `STEPS` (default 1), `SHOTS` (100), `QUBITS` (6), and `MAX_USD`
(30) - raise `MAX_USD` along with the others or qemcmc refuses the run
after the session was already paid for. On mock the classical stays the
2-second sleep (there is no qemcmc path for it).

## The two-doors comparison (optional, costs real money)

IonQ Forte-1 is reachable directly and through Braket. The same pair
through each door, same physical machine:

```bash
VENDORS="braket ionq" \
  BRAKET_DEVICE="arn:aws:braket:us-east-1::device/qpu/ionq/Forte-1" \
  IONQ_DEVICE="qpu.forte-1" bash run.sh
```

Compare `vendor_wait_s` against what `../queues/` was advertising for each
door at that minute. Check both vendors' pricing for a single session
before running this one.

## Notes

- IBM sessions are a paid-plan feature; with an open-plan token the ibm row
  records whatever the account allows (and says so in the scout log).
- Credentials are read by the backends as the submitting user, nothing is
  configured system-wide: AWS credentials for braket, `IONQ_API_KEY` for
  ionq, the QRMI/IBM variables for ibm (see the flux-quantum README).
- The ibm step needs `pip install 'qrmi[ibm]'` (python 3.11+) wherever the
  classical job runs: the circuit is transpiled client side against the
  target QRMI hands over, because IBM takes only ISA circuits. Keep the
  session mode at its default (batch, billed for hardware time only);
  dedicated bills the wall clock including the vendor queue wait.
- `MAX_USD` guards each step run, but IBM bills by runtime second with
  periodic reporting, so also set a cost limit on the PAYG instance itself.
