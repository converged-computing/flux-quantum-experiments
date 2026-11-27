#!/bin/bash
# C4 of the QeMCMC campaign (see PROTOCOL.md): N hybrid jobs contending for
# one QPU, baseline (plain flux jobs) against coschedule (quantum pairs).
#
#   bash fluxbatch.sh check   # phase 0 only: is the cluster fit to spend on?
#   bash fluxbatch.sh run     # everything, gated; logs itself
#   bash fluxbatch.sh armb    # resume: arm B + evidence (arm A already paid)
#
# Run `run` inside tmux on the lead broker. Every phase writes into one
# timestamped evidence directory. The spending order is: free state check,
# free mock rehearsal, a free rehearsal of the REAL app entrypoint on EVERY
# node, explicit cost confirmation, a ~$1.50 one-step canary pair that must
# run inside its hold, and only then the two arms.
#
# Jobs write their stdout to the KVS and this script attaches to read it.
# Never hand a job a --output under $HOME or $EVID: those paths exist on the
# lead only, and a job scheduled onto another node dies on open. That is
# exactly how C4 attempt 2 lost baseline 1 (PROTOCOL.md).
set -u

CEPHEUS="arn:aws:braket:us-west-1::device/qpu/rigetti/Cepheus-1-108Q"
REGION="us-west-1"
APP="/opt/flux-quantum-experiments/forte/qemcmc.py"
OUT="/opt/flux-quantum-experiments/forte"
JOBS_PER_ARM=4
STEPS=10
QUBITS=6
MAX_USD_PER_JOB=60
export FLUX_CLI_PLUGINPATH="${FLUX_CLI_PLUGINPATH:-/opt/flux-quantum/cli-plugins}"
# the CLI's braket probe opens a regionless SV1 handle, so the submit
# environment must carry a default region or the vendor reports unavailable
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-$REGION}"

EVID="$HOME/fluxbatch-$(date +%Y%m%d-%H%M%S)"

say()  { echo "[fluxbatch] $*"; }
fail() { echo "[fluxbatch] FAIL: $*"; exit 1; }

# ---------------------------------------------------------------- phase 0
check_state() {
    say "=== phase 0: cluster state ==="
    local bad=""

    flux resource list >/dev/null 2>&1 || bad="$bad broker"
    flux module list 2>/dev/null | grep -q sched-fluxion-qmanager || bad="$bad fluxion"
    flux jobtap list 2>/dev/null | grep -q quantum || bad="$bad jobtap-plugin"
    flux submit --help 2>&1 | grep -q -- --quantum-vendor || bad="$bad cli-plugin"
    flux python -c "import flux.job" 2>/dev/null || bad="$bad flux-python"

    # feasibility is allowed in two states only: absent, or loaded AND
    # answering that a configured vendor's scout is feasible (PROTOCOL.md
    # C5a). It bootstraps from notify, which carries the static scheduling
    # graph, qdevices included. A loaded module that calls it unsatisfiable
    # would refuse every quantum submit, so that fails here, before money.
    if flux module list 2>/dev/null | grep -q sched-fluxion-feasibility; then
        if flux python -c "
import flux
from flux_quantum.launch import build_scout_jobspec
flux.Flux().rpc('feasibility.check',
                {'jobspec': build_scout_jobspec('braket', 1)}).get()
" 2>/dev/null; then
            say "feasibility: loaded and correct"
        else
            bad="$bad feasibility-refuses-quantum-scouts"
        fi
    else
        say "feasibility: absent (allowed; C5a wants it loaded on a fresh boot)"
    fi

    # the broker was configured with the static scheduling graph, so the
    # braket qdevices are in the live graph and reachable from the root. The
    # graph is never mutated: a missing or orphaned qdevice means the boot
    # did not generate /etc/flux/system/scheduling.json correctly.
    flux python -c "
import flux
from flux_quantum import graph
g = graph.get_live_graph(flux.Flux())
assert not graph.orphaned_vendors(g), 'orphaned qdevices'
raise SystemExit(0 if 'braket' in graph.vendors_present(g) else 1)
" 2>/dev/null || bad="$bad scheduling-graph"

    # the CLI's own vendor resolution, the very call a quantum submit makes.
    # On failure its log names the reason: SDK, credentials, region, device.
    local probe
    if probe=$(flux python -c "
from flux_quantum.selector import select_vendor
name, sig, log = select_vendor(['braket'])
print(log[-1])" 2>&1); then
        say "vendor probe: $probe"
    else
        say "vendor probe refused braket:"
        echo "$probe" | tail -5 | sed 's/^/    /'
        bad="$bad braket-vendor-probe"
    fi

    # the scheduler must not be wedged. The static graph removed the one
    # known cause (growing the graph under live allocations corrupted
    # planner spans), but the dmesg grep stays as the cheap tripwire: the
    # trivial probe job can pass while fluxion's own frees fail underneath.
    local probe_id
    if probe_id=$(flux submit -n1 /bin/true 2>/dev/null); then
        flux job wait-event -t 120 "$probe_id" clean </dev/null >/dev/null 2>&1 \
            || bad="$bad scheduler-wedged"
    else
        bad="$bad scheduler-refuses-submits"
    fi
    if flux dmesg 2>/dev/null | grep -qE "planner_multi_rem_span|run_sched_loop"; then
        say "WARNING fluxion logged planner or sched-loop errors since boot:"
        flux dmesg 2>/dev/null | grep -E "planner_multi_rem_span|run_sched_loop" \
            | tail -3 | sed 's/^/    /'
        bad="$bad fluxion-planner-errors"
    fi

    # credentials reach braket in the device's own region, device answers
    local status
    status=$(python3 -c "
import boto3
d = boto3.client('braket', region_name='$REGION').get_device(deviceArn='$CEPHEUS')
print(d['deviceStatus'])" 2>/dev/null) || bad="$bad braket-credentials"
    say "device status: ${status:-unreachable}"
    # A PAUSED or RETIRED device still takes submissions and queues them, so
    # the chains stall on their first task and the run dies by timeout, up to
    # half an hour later. Refuse here instead, and print the execution window
    # so the next attempt can be timed. Override only to test the plumbing.
    if [ "${status:-}" != "ONLINE" ]; then
        python3 -c "
import boto3, json
d = boto3.client('braket', region_name='$REGION').get_device(deviceArn='$CEPHEUS')
caps = json.loads(d['deviceCapabilities'])
for w in caps.get('service', {}).get('executionWindows', []):
    print('    window: {} {}-{}'.format(w.get('executionDay'),
          w.get('windowStartHour'), w.get('windowEndHour')))
" 2>/dev/null
        if [ "${FLUXBATCH_ALLOW_OFFLINE_DEVICE:-}" = "1" ]; then
            say "WARNING device is $status, continuing because the override is set"
        else
            bad="$bad device-not-online"
        fi
    fi

    # The account quota on ACTIVE hybrid jobs (3 by default; QUEUED and
    # CANCELLING count) gates every CreateJob: C4 attempt 3 lost pair 4 to it.
    # The scout now waits a quota refusal out instead of dying, but leftovers
    # from an earlier run eat arm B's slots, so say what is already active.
    local active
    active=$(python3 -c "
import boto3
jobs = boto3.client('braket', region_name='$REGION').search_jobs(filters=[])
print(sum(j.get('status') in ('CREATED', 'QUEUED', 'RUNNING', 'CANCELLING')
          for j in jobs.get('jobs', [])))" 2>/dev/null)
    say "active hybrid jobs in the account: ${active:-unreadable} (quota: 3)"
    if [ "${active:-0}" -ge 3 ] 2>/dev/null; then
        say "WARNING the quota is already full; arm B's scouts will wait for slots"
    fi

    [ -z "$bad" ] || fail "state check:$bad"
    say "state check: OK"
}

# ----------------------------------------------------- submit, both kinds
# plain submit: prints the job id. quantum submit: stdout is the scout id,
# the classical id is on stderr as 'held classical job N'.
submit_pair() {
    local err scout classical
    err=$(mktemp)
    scout=$(flux submit --quantum-vendor "$1" ${2:+--quantum-device "$2"} \
                ${3:+--quantum-hold-max "$3"} -N1 -n1 -c4 \
                -- "${@:4}" 2>"$err") || { cat "$err" >&2; rm -f "$err"; return 1; }
    cat "$err" >>"$EVID/submits.log"
    classical=$(grep -oE 'held classical job [0-9]+' "$err" | awk '{print $NF}')
    rm -f "$err"
    if [ -z "$scout" ] || [ -z "$classical" ]; then
        # the submit may have SUCCEEDED with unparseable output; a live scout
        # would hold (bill) the device unrecorded, so cancel before failing
        [ -n "$scout" ] && flux cancel "$scout" 2>/dev/null
        echo "unparseable pair submit (scout='$scout'); see submits.log" >&2
        return 1
    fi
    echo "$scout $classical"
}

wait_for() { # id timeout label
    [ -n "$1" ] || fail "$3: empty job id (parsing failed upstream)"
    flux job wait-event -t "$2" "$1" clean </dev/null >/dev/null 2>&1 \
        || fail "$3 (job $1) did not finish within $2 s"
}

# finishing is not succeeding: the flux result must be COMPLETED and the
# runner must have printed its summary (attempt 1 "passed" an arm whose
# chains all died inside their jobs)
assert_ran() { # id logfile label
    local result
    result=$(flux jobs -no "{result}" "$1" 2>/dev/null)
    if [ "$result" != "COMPLETED" ] || ! grep -q '"steps_per_hour"' "$2"; then
        say "$3: result=$result, output tail:"
        tail -20 "$2" | sed 's/^/    /'
        fail "$3 finished without running its chain"
    fi
}

# ---------------------------------------------------------------- phase 1
mock_rehearsal() {
    say "=== phase 1: free mock rehearsal, $JOBS_PER_ARM concurrent pairs ==="
    local i pair scout classical ok=0
    for i in $(seq 1 "$JOBS_PER_ARM"); do
        # subshell: a VAR=x prefix on a function call would persist in this
        # shell after it returns, leaking mock mode into the real arms
        pair=$( (export FLUX_QUANTUM_MOCK=1
                 submit_pair mock "" "" \
                     sh -c 'echo session=$QUANTUM_SESSION_ID; sleep 10') ) \
            || fail "mock pair $i was not admitted"
        echo "mock $i $pair" >>"$EVID/pairs.txt"
    done
    grep '^mock' "$EVID/pairs.txt" | while read -r _ i scout classical; do
        wait_for "$classical" 180 "mock classical $i"
        flux job attach "$classical" </dev/null 2>&1 | grep -q 'session=.' \
            || fail "mock classical $i ran without a session"
    done || exit 1
    say "mock rehearsal: OK ($JOBS_PER_ARM/$JOBS_PER_ARM pairs handed a session over)"
}

# --------------------------------------------------------------- phase 1b
# C4 attempt 2 failed AFTER the canary was paid for: a free baseline job
# landed on the second node, where nothing had ever exercised the app, and
# died instantly. The mock rehearsal never catches that class of failure
# because it runs `sh -c echo`, not the app. So before any money: run the
# REAL entrypoint (interpreter, imports, boto3, region, $OUT writability,
# live pricing read) pinned to EVERY rank, free of charge.
node_rehearsal() {
    say "=== phase 1b: free app rehearsal on every node ==="
    local size r id result
    size=$(flux getattr size)
    for r in $(seq 0 $((size - 1))); do
        id=$(flux submit --requires="ranks:$r" --job-name="rehearse-$r" \
                 -- sh -c "python3 $APP estimate --path braket \
                     --device $CEPHEUS --steps $STEPS --max-seconds 3600 \
                     >/dev/null && touch $OUT/.fluxbatch-writable") \
            || fail "rank $r rehearsal did not submit"
        echo "rehearse $r - $id" >>"$EVID/pairs.txt"
    done
    grep '^rehearse' "$EVID/pairs.txt" | while read -r _ r _ id; do
        wait_for "$id" 300 "rank $r rehearsal"
        result=$(flux jobs -no "{result}" "$id" 2>/dev/null)
        if [ "$result" != "COMPLETED" ]; then
            say "rank $r cannot run the app; its output:"
            flux job attach "$id" </dev/null 2>&1 | tail -15 | sed 's/^/    /'
            fail "rank $r failed the app rehearsal; fix the node, spend nothing"
        fi
    done || exit 1
    say "node rehearsal: OK (the app runs and $OUT is writable on all $size ranks)"
}

# ---------------------------------------------------------------- phase 2
confirm_cost() {
    say "=== phase 2: cost ==="
    local est
    est=$(python3 "$APP" estimate --path braket --device "$CEPHEUS" \
              --steps "$STEPS" --max-seconds 3600 2>&1) \
        || { echo "$est" | tail -5 | sed 's/^/    /'
             fail "the estimate itself failed; nothing was spent"; }
    echo "$est" | tee "$EVID/estimate.txt"
    say "that is PER JOB; the batch is $((JOBS_PER_ARM * 2)) jobs of $STEPS steps,"
    say "plus a 1-step canary pair first (~\$1.50)"
    if [ "${FLUXBATCH_YES:-}" != "1" ]; then
        read -r -p "[fluxbatch] type yes to spend: " a
        [ "$a" = "yes" ] || fail "not confirmed"
    fi
}

# --------------------------------------------------------------- phase 2b
canary() {
    say "=== phase 2b: canary, ONE real pair, 1 step ==="
    SPENT=1
    local pair scout classical
    pair=$(submit_pair braket "$CEPHEUS" 1800 \
               python3 "$APP" run --path braket --arm classical \
               --device "$CEPHEUS" --steps 1 --qubits "$QUBITS" \
               --seed 99 --max-usd 5 --yes \
               --out "$OUT/runs-flux-canary.jsonl") \
        || fail "the canary pair was not admitted; nothing else was spent"
    echo "canary 0 $pair" >>"$EVID/pairs.txt"
    read -r scout classical <<<"$pair"
    say "canary scout=$scout classical=$classical (hold open takes ~2 min)"
    wait_for "$classical" 1800 "canary classical"
    flux job attach "$classical" </dev/null >"$EVID/canary.log" 2>&1
    if ! grep -q "credential from the scout: yes" "$EVID/canary.log"; then
        say "canary classical output:"
        tail -20 "$EVID/canary.log" | sed 's/^/    /'
        # the scout may still be holding (billing) the device: kill it first,
        # or the attach below blocks until its hold-max anyway
        flux cancel "$scout" 2>/dev/null
        say "canary scout output:"
        flux job attach "$scout" </dev/null 2>&1 | tail -20 | sed 's/^/    /'
        fail "the canary ran OUTSIDE its hold; the batch is refused"
    fi
    assert_ran "$classical" "$EVID/canary.log" "canary"
    say "canary: OK (the pair ran its chain inside its hold, end to end)"
}

# ---------------------------------------------------------------- phase 3
arm_a() {
    say "=== phase 3: arm A, $JOBS_PER_ARM plain jobs, public queue ==="
    local i id
    date +%s >"$EVID/armA-start"
    # stdout goes to the KVS, read back with flux job attach. A --output
    # under $EVID would be a lead-only path that kills any job scheduled
    # onto the other node (C4 attempt 2, baseline 1).
    for i in $(seq 1 "$JOBS_PER_ARM"); do
        id=$(flux submit -N1 -n1 -c4 --job-name="qb$i" \
                 -- python3 "$APP" run --path braket --arm baseline \
                    --device "$CEPHEUS" --steps "$STEPS" --qubits "$QUBITS" \
                    --seed "$i" --max-usd "$MAX_USD_PER_JOB" --yes \
                    --out "$OUT/runs-flux-base-$i.jsonl") \
            || fail "baseline $i did not submit"
        echo "base $i - $id" >>"$EVID/pairs.txt"
    done
    grep '^base' "$EVID/pairs.txt" | while read -r _ i _ id; do
        wait_for "$id" 10800 "baseline $i"
        flux job attach "$id" </dev/null >"$EVID/qb$i.log" 2>&1
        assert_ran "$id" "$EVID/qb$i.log" "baseline $i"
    done || exit 1
    date +%s >"$EVID/armA-end"
    say "arm A done; 60 s for the public queue to clear"
    sleep 60
}

# ---------------------------------------------------------------- phase 4
arm_b() {
    say "=== phase 4: arm B, $JOBS_PER_ARM quantum pairs ==="
    local i pair
    date +%s >"$EVID/armB-start"
    for i in $(seq 1 "$JOBS_PER_ARM"); do
        pair=$(submit_pair braket "$CEPHEUS" 3600 \
                   python3 "$APP" run --path braket --arm classical \
                   --device "$CEPHEUS" --steps "$STEPS" --qubits "$QUBITS" \
                   --seed "$i" --max-usd "$MAX_USD_PER_JOB" --yes \
                   --out "$OUT/runs-flux-co-$i.jsonl") \
            || fail "pair $i was not admitted"
        echo "pair $i $pair" >>"$EVID/pairs.txt"
    done
    grep '^pair' "$EVID/pairs.txt" | while read -r _ i scout classical; do
        # the holds serialize on one device, so the pair granted LAST can
        # legitimately take JOBS_PER_ARM times one pair's duration
        wait_for "$classical" $((JOBS_PER_ARM * 3600)) "pair $i classical"
        flux job attach "$classical" </dev/null >"$EVID/qc$i.log" 2>&1
        assert_ran "$classical" "$EVID/qc$i.log" "pair $i classical"
    done || exit 1
    date +%s >"$EVID/armB-end"
}

# ---------------------------------------------------------------- phase 5
evidence() {
    say "=== phase 5: evidence ==="
    local i scout classical id yes node size r
    flux jobs -a -no "{id} {name} {status} {t_submit:.1f} {t_run:.1f} {t_cleanup:.1f}" \
        >"$EVID/jobs.txt"
    flux dmesg >"$EVID/dmesg.txt" 2>&1
    cp /etc/flux/system/scheduling.json "$EVID/scheduling.json" 2>/dev/null
    grep '^pair' "$EVID/pairs.txt" | while read -r _ i scout classical; do
        flux job attach "$classical" </dev/null >"$EVID/qc$i.log" 2>&1
        # a scout outliving its classical would block the attach below
        flux job wait-event -t 60 "$scout" clean </dev/null >/dev/null 2>&1 \
            || flux cancel "$scout" 2>/dev/null
        flux job attach "$scout" </dev/null >"$EVID/scout$i.log" 2>&1
        node=$(flux job info "$classical" R 2>/dev/null \
                   | jq -r '.execution.nodelist[0] // empty')
        echo "pair $i classical node: ${node:-unknown}" >>"$EVID/nodes.txt"
    done
    grep '^base' "$EVID/pairs.txt" | while read -r _ i _ id; do
        node=$(flux job info "$id" R 2>/dev/null \
                   | jq -r '.execution.nodelist[0] // empty')
        echo "base $i node: ${node:-unknown}" >>"$EVID/nodes.txt"
    done
    for id in $(awk '{for (f = 3; f <= NF; f++) if ($f != "-") print $f}' \
                    "$EVID/pairs.txt"); do
        flux job eventlog "$id" >"$EVID/eventlog-$id.txt" 2>/dev/null
    done
    # the steps/hr records are written by each job on the node it ran on;
    # pull them back from EVERY rank through flux exec so nothing has to be
    # fetched by hand afterwards. base64 because flux exec's output path is
    # a text channel: it truncated a raw tar at the first NUL byte, which is
    # byte 23 of the header, leaving just the first member's name (C4
    # attempt 4's first copy). The tar must then LIST, or the transcript
    # says so - a 22-byte text file passed the old -s check silently.
    size=$(flux getattr size 2>/dev/null || echo 1)
    for r in $(seq 0 $((size - 1))); do
        flux exec -r "$r" sh -c \
            "cd $OUT 2>/dev/null && tar cf - runs-flux-*.jsonl 2>/dev/null | base64" \
            2>/dev/null </dev/null | base64 -d >"$EVID/runs-flux-rank$r.tar" 2>/dev/null
        if tar tf "$EVID/runs-flux-rank$r.tar" >/dev/null 2>&1; then
            say "rank $r records: $(tar tf "$EVID/runs-flux-rank$r.tar" | tr '\n' ' ')"
        else
            rm -f "$EVID/runs-flux-rank$r.tar"
            say "WARNING rank $r returned no readable records; fetch by hand:"
            say "    flux exec -r $r sh -c 'cd $OUT && tar cf - runs-flux-*.jsonl | base64' | base64 -d > runs-flux-rank$r.tar"
        fi
    done
    grep -H "credential from the scout" "$EVID"/qc*.log 2>/dev/null \
        | tee "$EVID/credentials.txt"

    yes=$(grep -l "credential from the scout: yes" "$EVID"/qc*.log 2>/dev/null | wc -l)
    if [ "$yes" -eq "$JOBS_PER_ARM" ]; then
        say "BATCH VERDICT: OK ($yes/$JOBS_PER_ARM pairs ran inside their hold)"
    else
        say "BATCH VERDICT: DEGRADED ($yes/$JOBS_PER_ARM); mark the others"
    fi
    say "evidence in $EVID, including the per-node records"
    say "(runs-flux-rank*.tar); nothing needs fetching from the other node"
}

# ------------------------------------------------------------------ main
# On ANY exit after submissions began: cancel whatever still runs (an
# orphaned scout bills its hold until hold-max) and, if money moved,
# collect whatever evidence exists instead of abandoning it.
SPENT=0
FINISHED=0
finish() {
    local rc=$?
    [ "$FINISHED" = 1 ] && return
    FINISHED=1
    if [ "$rc" -ne 0 ] && [ -s "$EVID/pairs.txt" ]; then
        say "exiting with rc=$rc: cancelling anything still running"
        awk '{for (f = 3; f <= NF; f++) if ($f != "-") print $f}' \
            "$EVID/pairs.txt" | xargs -r -n1 flux cancel 2>/dev/null
        [ "$SPENT" = 1 ] && evidence
    fi
    sleep 1   # let the transcript tee flush before the shell returns
}

case "${1:-check}" in
    check) check_state ;;
    run | armb)
        mkdir -p "$EVID"
        exec > >(tee "$EVID/transcript.log") 2>&1
        trap finish EXIT
        say "evidence directory: $EVID"
        { date -u; (cd /opt/flux-quantum && git rev-parse HEAD); \
          flux module list; flux jobtap list; } >"$EVID/state.txt" 2>&1
        check_state
        if [ "$1" = "run" ]; then
            mock_rehearsal
            node_rehearsal
            confirm_cost
            canary
            arm_a
        else
            # the original run may have died precisely because the real path
            # broke, so the resume re-earns the rehearsal and the canary
            # before arm B's money
            say "resume mode: canary, arm B, evidence (arm A already paid for)"
            node_rehearsal
            confirm_cost
            canary
        fi
        arm_b
        evidence
        ;;
    *) fail "usage: fluxbatch.sh check|run|armb" ;;
esac
