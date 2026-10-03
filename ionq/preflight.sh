#!/bin/bash
# Check the instance before an IonQ campaign. Reads the live state, and runs
# one dry-run pair end to end, which also answers whether this account can
# open sessions. Nothing here reaches a QPU.
#
#     bash preflight.sh 2>&1 | tee preflight.log
#
# Against the fake service instead of IonQ:
#
#     flux python -m flux_quantum.backends.ionq.fake --port 8765 &
#     IONQ_API_KEY=x IONQ_API_URL=http://127.0.0.1:8765 bash preflight.sh

set -u
rc=0
ok()   { echo "  ok    $*"; }
bad()  { echo "  FAIL  $*"; rc=1; }
warn() { echo "  warn  $*"; }
sec()  { echo; echo "=== $* ==="; }

TARGET="${TARGET:-simulator}"

sec "0. the instance is idle"
leftover=$(flux jobs -no "{id} {state} {name}")
if [ -n "$leftover" ]; then
    echo "$leftover" | sed 's/^/  /'
    echo "  STOP. Clear these first: flux cancel --all"
    exit 1
fi
ok "no active jobs, $(flux resource list -s all -no '{ncores}') cores"

sec "1. the key and the service"
[ -n "${IONQ_API_KEY:-}" ] && ok "IONQ_API_KEY is set" || bad "IONQ_API_KEY is not set"
echo "  endpoint ${IONQ_API_URL:-https://api.ionq.co/v0.4}"
flux python - "$TARGET" <<'EOF' || bad "could not read the backend from the service"
import sys
from flux_quantum.backends.ionq import IonQBackend
b = IonQBackend()
got = b.client.get("/backends/%s" % sys.argv[1])
print("  %s status=%s degraded=%s qubits=%s average_queue_time=%s" % (
    sys.argv[1], got.get("status"), got.get("degraded"), got.get("qubits"),
    got.get("average_queue_time")))
raise SystemExit(0 if got.get("status") == "available" else 1)
EOF
[ $? = 0 ] && ok "$TARGET is available"

sec "2. flux-quantum knows the vendor"
flux python -c "
from flux_quantum.backends import known_vendors
v = known_vendors()
print('  vendors', sorted(v))
raise SystemExit(0 if 'ionq' in v else 1)" && ok "ionq is registered" \
    || bad "ionq is not a registered vendor, update flux-quantum"
flux submit --help 2>&1 | grep -q -- --quantum-dry-run && ok "the common submit options are present" \
    || bad "no --quantum-dry-run, this flux-quantum predates the common options"

sec "3. the plugin and the policy"
cfg=$(flux dmesg | grep -o "quantum: vendors=.*" | tail -1)
[ -n "$cfg" ] && echo "  $cfg" || bad "no quantum config line in dmesg, the plugin did not init"
echo "$cfg" | grep -q "ionq" && ok "the plugin allows ionq" || bad "ionq is not in the plugin's vendors"
pol=$(flux module stats sched-fluxion-qmanager 2>/dev/null | flux python -c "
import json,sys
try:
    print(','.join(sorted({v.get('policy','?') for v in json.load(sys.stdin).get('queues',{}).values()})))
except Exception:
    print('unknown')")
[ "$pol" = coschedule ] && ok "queue policy coschedule" || bad "queue policy is $pol, hold is only honoured by coschedule"

sec "4. the graph has qdevice_ionq, and how many qpus"
# the qpu count is the cap on pairs holding a device at once, and e2 sweeps against it
QPUS=$(flux python -c "
import flux, json
h = flux.Flux()
r = h.rpc('sched-fluxion-resource.find', {'criteria':'status=up','format':'jgf'}).get()['R']
r = json.loads(r) if isinstance(r, str) else r
g = r.get('graph', {})
ty = {n['id']: n['metadata']['type'] for n in g.get('nodes', [])}
par = {e['target']: e['source'] for e in g.get('edges', [])}
print(sum(1 for vid, t in ty.items() if t == 'qpu' and ty.get(par.get(vid)) == 'qdevice_ionq'))
" 2>/dev/null || echo 0)
[ "$QPUS" -ge 1 ] 2>/dev/null && ok "qdevice_ionq with $QPUS qpu(s)" \
    || bad "no qpu under qdevice_ionq. On an idle instance: flux python -m flux_quantum.populate ionq --qpus N"

sec "5. one dry-run pair, end to end"
# the scout's output says whether the account has sessions. Without them it
# falls back to a probe and says so, and every session result is then a probe result
err=$(mktemp)
scout=$(flux submit --quantum-vendor ionq --quantum-dry-run --quantum-device "$TARGET" -n1 \
    -- flux python "$(dirname "$0")/scripts/workload.py" --iterations 1 2>"$err")
main=$(grep -oE 'held classical job [0-9]+' "$err" | awk '{print $NF}')
sed 's/^/  /' "$err"; rm -f "$err"
if [ -z "$main" ]; then
    bad "no pair was created"
else
    if flux job wait-event -t 180 "$main" clean </dev/null >/dev/null 2>&1; then
        out=$(flux job attach "$main" </dev/null 2>&1)
        echo "$out" | grep -q '"event": "end", "t": [0-9.]*, "failed": 0' \
            && ok "the classical job ran a circuit in the hold" \
            || { bad "the classical job did not complete a circuit"; echo "$out" | sed 's/^/    /' | tail -5; }
    else
        bad "the classical job never completed"; flux jobs -a | head
    fi
    flux job wait-event -t 60 "$scout" clean </dev/null >/dev/null 2>&1
    sout=$(flux job attach "$scout" </dev/null 2>&1)
    if echo "$sout" | grep -q "session active"; then
        ok "this account has sessions, and the hold is real"
    elif echo "$sout" | grep -q "probe job instead"; then
        warn "no sessions on this account or target. The scout held with a probe, so session arms are probe arms"
    else
        echo "$sout" | sed 's/^/    /' | tail -5
    fi
    echo "$sout" | grep -q "closed ionq session" && ok "the scout closed the hold" || bad "the scout did not close the hold"
fi

sec "6. drain"
flux cancel --all >/dev/null 2>&1
for _ in $(seq 1 20); do flux jobs -no "{id}" | grep -q . || break; sleep 1; done
flux jobs -no "{id}" | grep -q . && bad "jobs remain" || ok "nothing left running"

echo
[ "$rc" = 0 ] && echo "=== PASS, qpus=$QPUS ===" || echo "=== STOP. Fix the failures above. ==="
exit "$rc"
