#!/bin/bash
# One coscheduled pair per vendor: the mechanism-verification table.
#
#   bash run.sh                          # mock only, free rehearsal
#   VENDORS="mock braket" bash run.sh    # add real vendors explicitly
#   VENDORS="braket ionq ibm" bash run.sh
#
# Each vendor gets exactly ONE pair: scout opens the session, waits out the
# vendor queue, memos the session onto the held classical, releases it. The
# row records what the paper's table needs: vendor, device, vendor wait,
# release latency, session id, and the scout's own account of the queue.
#
# On braket and ionq the released classical does real work: one QeMCMC step
# (STEPS to raise it) through forte/qemcmc.py with the scout's credential,
# so the row shows a circuit executing, not a sleep. Cost per step at
# SHOTS=100: braket->Forte-1 $8.30, ionq direct $25.79, braket->SV1 pennies.
# Raise MAX_USD along with STEPS or SHOTS, or qemcmc refuses the run.
# mock and ibm have no qemcmc path; their classical stays the sleep probe.
#
# Paid vendors run only when named in VENDORS. Defaults are the cheapest
# device per vendor (braket: SV1 simulator; ionq/ibm: backend default).
# Override with BRAKET_DEVICE / IONQ_DEVICE / IBM_DEVICE, e.g. the two-doors
# comparison of the same machine:
#   VENDORS="braket ionq" \
#     BRAKET_DEVICE="arn:aws:braket:us-east-1::device/qpu/ionq/Forte-1" \
#     IONQ_DEVICE="qpu.forte-1" bash run.sh        # COSTS REAL MONEY
set -u
cd "$(dirname "$0")"

VENDORS="${VENDORS:-mock}"
SIZE="${SIZE:-2}"
WORK="${WORK:-2}"
TIMEOUT="${TIMEOUT:-1800}"
OUT="${OUT:-results/pairs.csv}"
STEPS="${STEPS:-1}"
SHOTS="${SHOTS:-100}"
QUBITS="${QUBITS:-6}"
MAX_USD="${MAX_USD:-30}"
FORTE="$(cd ../forte && pwd)"
# the scout's braket backend defaults to SV1; qemcmc's path defaults to
# Forte-1, so the payload always names the device the scout actually used
BRAKET_SV1="arn:aws:braket:::device/quantum-simulator/amazon/sv1"
mkdir -p results

say() { echo "[$(date +%H:%M:%S)] $*" >&2; }
ev()  { flux job eventlog "$1" 2>/dev/null | awk -v e="$2" '$2==e {print $1; exit}'; }
memo_ts() {
    flux job eventlog "$1" 2>/dev/null | awk '$2=="memo" && /quantum_session/ {print $1; exit}'
}

flux resource list >/dev/null || { echo "no flux instance"; exit 1; }
flux submit --help 2>&1 | grep -q -- --quantum-vendor \
    || { echo "flux-quantum CLI plugin is not loaded"; exit 1; }

HDR="vendor,device,t0,submit,scout_start,priority,alloc,start,finish,scout_finish,vendor_wait_s,release_lat_s,timedout,session"
[ -s "$OUT" ] || echo "$HDR" > "$OUT"

payload_for() { # vendor device-or-empty -> the classical job's sh -c payload
    local v="$1" dev="$2" qdev
    case "$v" in
    braket|ionq)
        qdev="$dev"
        [ "$v" = braket ] && [ -z "$qdev" ] && qdev="$BRAKET_SV1"
        # one real QeMCMC step with the scout's credential. qemcmc is rank
        # aware: rank 0 drives the chain, every other task of -n$SIZE exits,
        # so the vendor is billed once.
        echo "echo got \${QUANTUM_SESSION_ID:-nothing}; \
exec python3 '$FORTE/qemcmc.py' run --path $v --arm classical \
--steps $STEPS --shots $SHOTS --qubits $QUBITS --max-usd $MAX_USD \
${qdev:+--device '$qdev'} --out '$PWD/results/qemcmc-$v.jsonl'"
        ;;
    *)  # no qemcmc path for mock or ibm; the handoff itself is the row
        echo "sleep $WORK; echo got \${QUANTUM_SESSION_ID:-nothing}"
        ;;
    esac
}

one_pair() { # vendor device-or-empty
    local v="$1" dev="$2" devargs=() to=0
    [ -n "$dev" ] && devargs=(--quantum-device "$dev")
    local err t0 scout main payload
    err=$(mktemp)
    t0=$(date +%s.%N)
    payload=$(payload_for "$v" "$dev")
    say "$v: submitting one pair (device: ${dev:-backend default})"
    if [ "$v" = mock ]; then
        export FLUX_QUANTUM_MOCK=1 FLUX_QUANTUM_MOCK_QUEUE=20 \
               FLUX_QUANTUM_MOCK_SERVICE_TIME=0.1 FLUX_QUANTUM_MOCK_BASE_OVERHEAD=2
    fi
    scout=$(flux submit --quantum-vendor "$v" "${devargs[@]}" -n"$SIZE" \
        -- sh -c "$payload" 2>"$err")
    main=$(grep -oE 'held classical job [0-9]+' "$err" | awk '{print $NF}')
    [ -z "$main" ] && { say "$v: no held classical job"; cat "$err" >&2; rm -f "$err"; return 1; }
    rm -f "$err"
    say "$v: scout=$scout classical=$main, waiting (timeout ${TIMEOUT}s)"
    flux job wait-event -t "$TIMEOUT" "$main" clean </dev/null >/dev/null 2>&1 || to=1
    [ "$to" = 0 ] && { flux job wait-event -t 120 "$scout" clean </dev/null >/dev/null 2>&1 || to=1; }
    if [ "$to" = 1 ]; then
        say "$v: TIMEOUT, cancelling pair"
        flux cancel "$main" "$scout" >/dev/null 2>&1
        flux job wait-event -t 30 "$scout" clean >/dev/null 2>&1
    fi
    local submit s_start priority alloc start finish s_finish session wait lat
    submit=$(ev "$main" submit); priority=$(memo_ts "$main")
    alloc=$(ev "$main" alloc); start=$(ev "$main" start); finish=$(ev "$main" finish)
    s_start=$(ev "$scout" start); s_finish=$(ev "$scout" finish)
    session=$(flux job attach "$scout" 2>/dev/null | grep -oE 'session=[^ ]+' | head -1 | cut -d= -f2)
    wait=""; lat=""
    [ -n "$priority" ] && [ -n "$s_start" ] && wait=$(echo "$priority $s_start" | awk '{printf "%.2f", $1-$2}')
    [ -n "$priority" ] && [ -n "$start" ]   && lat=$(echo "$start $priority"   | awk '{printf "%.4f", $1-$2}')
    echo "$v,${dev:-default},$t0,$submit,$s_start,$priority,$alloc,$start,$finish,$s_finish,$wait,$lat,$to,${session:-}" >> "$OUT"
    say "$v: vendor wait ${wait:-?}s, release latency ${lat:-?}s, session ${session:-?}"
    # the scout's own log is the queue-position evidence, keep it whole
    flux job attach "$scout" > "results/scout-$v.log" 2>&1
    flux job eventlog "$main" > "results/classical-$v.eventlog" 2>&1
    # and the classical's own log: on braket/ionq this is the QeMCMC step
    flux job attach "$main" > "results/classical-$v.log" 2>&1
    [ "$to" = 0 ]
}

rc=0
for v in $VENDORS; do
    case "$v" in
    mock) dev="" ;;
    braket) dev="${BRAKET_DEVICE:-}" ;;   # backend default is the SV1 simulator
    ionq) dev="${IONQ_DEVICE:-}" ;;
    ibm) dev="${IBM_DEVICE:-}" ;;
    *) say "unknown vendor $v, skipping"; continue ;;
    esac
    one_pair "$v" "$dev" || rc=1
done
echo
column -s, -t < "$OUT"
exit $rc
