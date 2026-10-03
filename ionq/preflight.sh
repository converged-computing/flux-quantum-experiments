#!/bin/bash
# Check the instance before an IonQ campaign. Reads the live state, and runs
# one dry-run pair end to end, which also answers whether this account can
# open sessions. Nothing here reaches a QPU.
#
#     bash preflight.sh 2>&1 | tee preflight.log
#
# Against the fake service instead of IonQ. On an instance of more than one
# node, --bind 0.0.0.0 and the URL it prints, which the other nodes reach:
#
#     flux python -m flux_quantum.backends.ionq.fake --port 8765 --bind 0.0.0.0 &
#     IONQ_API_KEY=x IONQ_API_URL=http://<address it printed>:8765 bash preflight.sh

set -u
HERE=$(cd "$(dirname "$0")" && pwd)
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

sec "1b. what a circuit costs on $TARGET"
# the service prices from the gate counts and shots, with a per job minimum.
# The workload's circuit is a GHZ state: one 1q gate, qubits-1 2q gates
SHOTS="${SHOTS:-100}"
flux python - "$TARGET" "$SHOTS" <<'EOF' || warn "no estimate from the service, check the rate in the console before a run that bills"
import sys
from flux_quantum.backends.ionq import IonQBackend
target, shots = sys.argv[1], int(sys.argv[2])
c = IonQBackend().client
def est(qubits, q1, q2, what):
    got = c.get("/jobs/estimate?backend=%s&qubits=%d&shots=%d&1q_gates=%d&2q_gates=%d&error_mitigation=false"
                % (target, qubits, shots, q1, q2))
    rate = got.get("rate_information") or {}
    print("  %-28s %8.2f %s%s" % (what, float(got.get("estimated_total_cost") or 0), got.get("cost_unit", "usd"),
          "  (job minimum %s)" % rate.get("job_cost_minimum") if rate.get("job_cost_minimum") is not None else ""))
    if rate.get("fake"):
        print("  (made-up rates: this is the fake service)")
    return float(got.get("estimated_total_cost") or 0)
circuit = est(2, 1, 1, "one workload circuit, %d shots" % shots)
warm = est(1, 1, 0, "one warm-up job, %d shots" % shots)
print("  a pair with one circuit is about %.2f, a plain job %.2f" % (circuit + warm, circuit))
EOF

sec "1c. every node has these scripts and reaches the endpoint"
# the instance shares nothing between nodes. The pair runs where the scheduler
# puts it, and needs this directory, flux-quantum and the endpoint from there.
# A fake on the loopback of this node fails here, and so does a checkout that
# is only on this node
NNODES=$(flux resource list -s all -no '{nnodes}')
echo "  $NNODES node(s): $(flux resource list -s all -no '{nodelist}')"
nodes_out=$(PREFLIGHT_DIR="$HERE" flux run -N"$NNODES" --label-io --cwd=/tmp -o cpu-affinity=off \
    flux python -c '
import os, socket, sys, urllib.request, urllib.error
h = socket.gethostname().split(".")[0]
rc = 0
d = os.environ["PREFLIGHT_DIR"]
if os.path.isdir(os.path.join(d, "scripts")):
    print("%s has %s" % (h, d))
else:
    print("%s has no %s" % (h, d)); rc = 1
try:
    import flux_quantum.backends.ionq
except ImportError as e:
    print("%s has no flux_quantum with ionq: %s" % (h, e)); rc = 1
url = (os.environ.get("IONQ_API_URL") or "https://api.ionq.co/v0.4").rstrip("/")
try:
    urllib.request.urlopen(url + "/backends", timeout=10)
except urllib.error.HTTPError:
    pass  # it answered, and a status is a reachable service
except Exception as e:
    print("%s cannot reach %s: %s" % (h, url, e)); rc = 1
if rc == 0:
    print("%s reaches %s" % (h, url))
sys.exit(rc)
' 2>&1)
rcn=$?
echo "$nodes_out" | sed 's/^/  /'
if [ "$rcn" = 0 ]; then
    ok "every node has the scripts, flux-quantum and the endpoint"
else
    bad "a node is missing something above. Clone this repository at the same path on every node, and start the fake with --bind 0.0.0.0 so the URL it prints is reachable from all of them"
fi

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
# the plugin reports its configuration through flux jobtap query. dmesg has
# the same line at init, but it is a ring buffer and a long run scrolls it out
qplug=$(flux jobtap list 2>/dev/null | grep -v '^\.' | grep -i quantum | head -1)
cfg=$(flux jobtap query "${qplug:-quantum.so}" 2>/dev/null | flux python -c "
import json, sys
try:
    q = json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
print(' '.join('%s=%s' % (k, q[k]) for k in ('vendors', 'total_cores', 'reserve_cores', 'preempt_after') if k in q))
" 2>/dev/null)
[ -n "$cfg" ] && echo "  quantum plugin ${qplug:-quantum.so}: $cfg" \
    || bad "the quantum jobtap plugin is not loaded, flux jobtap list shows none"
echo "$cfg" | grep -qE "vendors=[^ ]*ionq" && ok "the plugin allows ionq" || bad "ionq is not in the plugin's vendors"
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
    -- flux python "$HERE/scripts/workload.py" --iterations 1 2>"$err")
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

sec "5b. can this account open a session on $TARGET"
# the dry run above asks the simulator, which can refuse a session the
# account has for the device. A session bills nothing by itself, only its
# jobs do, so open one on the target with no job in it and end it at once
if [ "$TARGET" = simulator ]; then
    echo "  the target is the simulator, section 5 already asked it"
else
    flux python - "$TARGET" <<'EOF'
import sys
from flux_quantum.backends.ionq import APIError, IonQBackend
c = IonQBackend().client
body = {"backend": sys.argv[1], "settings": {"duration_limit_min": 1, "job_count_limit": 1}}
try:
    s = c.post("/sessions", body)
except APIError as e:
    print("  %s refused a session: HTTP %s" % (sys.argv[1], e.status))
    print("  no sessions on this account for the device. Sessions are in beta: ask support@ionq.com, or hold with --quantum-hold probe")
    raise SystemExit(2)
sid = s.get("id")
print("  session %s created on %s, status %s" % (sid, sys.argv[1], s.get("status")))
ended = c.post("/sessions/%s/end" % sid)
print("  ended, status %s" % ended.get("status"))
raise SystemExit(0 if ended.get("status") == "ended" else 1)
EOF
    case $? in
        0) ok "this account has sessions on $TARGET, opened and ended one with no job in it" ;;
        2) warn "no sessions on $TARGET. The pair can only hold with a probe, which releases the classical job when the warm-up starts and holds nothing after" ;;
        *) bad "a session was opened on $TARGET but did not end. Check the console and end it" ;;
    esac
fi

sec "6. drain"
flux cancel --all >/dev/null 2>&1
for _ in $(seq 1 20); do flux jobs -no "{id}" | grep -q . || break; sleep 1; done
flux jobs -no "{id}" | grep -q . && bad "jobs remain" || ok "nothing left running"

echo
[ "$rc" = 0 ] && echo "=== PASS, qpus=$QPUS ===" || echo "=== STOP. Fix the failures above. ==="
exit "$rc"
