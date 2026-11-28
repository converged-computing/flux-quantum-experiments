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
the cheapest device per vendor; the classical job is a 2-second sleep that
echoes its session id, so the quantum side is a session open/close with no
real workload.

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
