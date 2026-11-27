#!/bin/bash
# C2f/C3f of the QeMCMC campaign (PROTOCOL.md): the standalone mechanism
# cells repeated THROUGH FLUX. At each filler level (0, 5, 10): one baseline
# job, then one coscheduled pair, sequential, 60 s of queue clear between
# cells so no cell sees another's contention.
#
# The fillers are the app's own FillerPool (qemcmc.py --fillers N): 10-shot
# tasks kept topped up in the device's public queue, cancelled at the end,
# billed only if executed - the same mechanism, order and shot count as the
# standalone C2/C3, so the steps/hr columns are directly comparable. What is
# new is the scheduler in the loop: every measured job runs under flux, so
# alloc->free core-seconds and eventlogs exist for every cell, which is the
# whole point of repeating them (see PROTOCOL.md's deviation note).
#
#   bash fluxcells.sh check   # fluxbatch's full phase-0 gate, nothing more
#   bash fluxcells.sh run     # everything, gated; logs itself
#
# Run `run` inside tmux on the lead. One hybrid job is open at a time, so
# the hybrid-jobs quota is never contended. If a cell's job is cancelled
# from OUTSIDE (flux cancel, broker death), its FillerPool's finally may
# not run: at worst that cell's N fillers execute unpaid-for-nothing
# (~$0.31 each) - check the Braket console after any abort.
set -u

CEPHEUS="arn:aws:braket:us-west-1::device/qpu/rigetti/Cepheus-1-108Q"
REGION="us-west-1"
APP="/opt/flux-quantum-experiments/forte/qemcmc.py"
OUT="/opt/flux-quantum-experiments/forte"
HERE="$(cd "$(dirname "$0")" && pwd)"
STEPS=20              # the standalone C2/C3 ran 20 steps; keep comparable
QUBITS=6
SEED=1                # one seed everywhere: identical proposal sequences,
                      # so only the timing differs across arms and levels
FILLER_LEVELS="0 5 10"
FILLER_SHOTS=10
# worst case at 10 fillers is every top-up executing: ~10*(STEPS+1) tasks.
# The estimate phase prints the real number; this only has to clear it.
MAX_USD_PER_JOB=120
export FLUX_CLI_PLUGINPATH="${FLUX_CLI_PLUGINPATH:-/opt/flux-quantum/cli-plugins}"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-$REGION}"

EVID="$HOME/fluxcells-$(date +%Y%m%d-%H%M%S)"

say()  { echo "[fluxcells] $*"; }
fail() { echo "[fluxcells] FAIL: $*"; exit 1; }

# ----------------------------------------------------- submit, both kinds
submit_pair() { # vendor device hold_max cmd...
    local err scout classical
    err=$(mktemp)
    scout=$(flux submit --quantum-vendor "$1" ${2:+--quantum-device "$2"} \
                ${3:+--quantum-hold-max "$3"} -N1 -n1 -c4 \
                -- "${@:4}" 2>"$err") || { cat "$err" >&2; rm -f "$err"; return 1; }
    cat "$err" >>"$EVID/submits.log"
    classical=$(grep -oE 'held classical job [0-9]+' "$err" | awk '{print $NF}')
    rm -f "$err"
    if [ -z "$scout" ] || [ -z "$classical" ]; then
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

assert_ran() { # id logfile label
    local result
    result=$(flux jobs -no "{result}" "$1" 2>/dev/null)
    if [ "$result" != "COMPLETED" ] || ! grep -q '"steps_per_hour"' "$2"; then
        say "$3: result=$result, output tail:"
        tail -20 "$2" | sed 's/^/    /'
        fail "$3 finished without running its chain"
    fi
}

# ---------------------------------------------------------------- phase 0
check_state() {
    # fluxbatch's gate is the apparatus gate; do not fork a second copy
    bash "$HERE/fluxbatch.sh" check || fail "fluxbatch's state check refused"
}

# ---------------------------------------------------------------- phase 1
mock_rehearsal() {
    say "=== phase 1: free mock rehearsal, one pair ==="
    local pair scout classical
    # subshell: a leaked FLUX_QUANTUM_MOCK would put mocks in the real cells
    pair=$( (export FLUX_QUANTUM_MOCK=1
             submit_pair mock "" "" \
                 sh -c 'echo session=$QUANTUM_SESSION_ID; sleep 10') ) \
        || fail "the mock pair was not admitted"
    echo "mock 1 $pair" >>"$EVID/pairs.txt"
    read -r scout classical <<<"$pair"
    wait_for "$classical" 180 "mock classical"
    flux job attach "$classical" </dev/null 2>&1 | grep -q 'session=.' \
        || fail "the mock classical ran without a session"
    say "mock rehearsal: OK"
}

# --------------------------------------------------------------- phase 1b
node_rehearsal() {
    say "=== phase 1b: free app rehearsal on every node ==="
    local size r id result
    size=$(flux getattr size)
    for r in $(seq 0 $((size - 1))); do
        id=$(flux submit --requires="ranks:$r" --job-name="rehearse-$r" \
                 -- sh -c "python3 $APP estimate --path braket \
                     --device $CEPHEUS --steps $STEPS --max-seconds 3600 \
                     >/dev/null && touch $OUT/.fluxcells-writable") \
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
    say "=== phase 2: cost, per filler level ==="
    local n est
    for n in $FILLER_LEVELS; do
        est=$(python3 "$APP" estimate --path braket --device "$CEPHEUS" \
                  --steps "$STEPS" --fillers "$n" --filler-shots "$FILLER_SHOTS" \
                  --max-seconds 3600 2>&1) \
            || { echo "$est" | tail -5 | sed 's/^/    /'
                 fail "the estimate failed at $n fillers; nothing was spent"; }
        say "--- $n fillers (per job; each level runs 2 jobs) ---"
        echo "$est" | tee -a "$EVID/estimate.txt"
    done
    say "fillers_worst_usd assumes EVERY topped-up filler executes; the"
    say "standalone C3 paid only for executed ones, far below worst case."
    say "6 chain jobs of $STEPS steps, plus a 1-step canary pair (~\$1.50)."
    if [ "${FLUXCELLS_YES:-}" != "1" ]; then
        read -r -p "[fluxcells] type yes to spend: " a
        [ "$a" = "yes" ] || fail "not confirmed"
    fi
}

# --------------------------------------------------------------- phase 2b
canary() {
    say "=== phase 2b: canary, ONE real pair, 1 step, 0 fillers ==="
    SPENT=1
    local pair scout classical
    pair=$(submit_pair braket "$CEPHEUS" 1800 \
               python3 "$APP" run --path braket --arm classical \
               --device "$CEPHEUS" --steps 1 --qubits "$QUBITS" \
               --seed 99 --max-usd 5 --yes \
               --out "$OUT/runs-flux-cell-canary.jsonl") \
        || fail "the canary pair was not admitted; nothing else was spent"
    echo "canary 0 $pair" >>"$EVID/pairs.txt"
    read -r scout classical <<<"$pair"
    say "canary scout=$scout classical=$classical (hold open takes ~2 min)"
    wait_for "$classical" 1800 "canary classical"
    flux job attach "$classical" </dev/null >"$EVID/canary.log" 2>&1
    assert_ran "$classical" "$EVID/canary.log" "canary"
    say "canary: OK (the pair ran its chain inside its hold, end to end)"
    sleep 60
}

# ---------------------------------------------------------------- phase 3
# One cell = one measured job at one filler level. Baseline first, then the
# pair, 60 s of clear between them: each cell must see only its own fillers.
cells() {
    local n id pair scout classical
    for n in $FILLER_LEVELS; do
        say "=== cell: $n fillers, baseline (plain flux job) ==="
        date +%s >"$EVID/cell-base-$n-start"
        id=$(flux submit -N1 -n1 -c4 --job-name="cellb$n" \
                 -- python3 "$APP" run --path braket --arm baseline \
                    --device "$CEPHEUS" --steps "$STEPS" --qubits "$QUBITS" \
                    --seed "$SEED" --fillers "$n" --filler-shots "$FILLER_SHOTS" \
                    --max-usd "$MAX_USD_PER_JOB" --yes \
                    --out "$OUT/runs-flux-cell-base-$n.jsonl") \
            || fail "baseline at $n fillers did not submit"
        echo "cellbase $n - $id" >>"$EVID/pairs.txt"
        wait_for "$id" 7200 "baseline at $n fillers"
        flux job attach "$id" </dev/null >"$EVID/cell-base-$n.log" 2>&1
        assert_ran "$id" "$EVID/cell-base-$n.log" "baseline at $n fillers"
        date +%s >"$EVID/cell-base-$n-end"
        say "baseline at $n fillers: OK; 60 s for the queue to clear"
        sleep 60

        say "=== cell: $n fillers, coscheduled pair ==="
        date +%s >"$EVID/cell-pair-$n-start"
        pair=$(submit_pair braket "$CEPHEUS" 3600 \
                   python3 "$APP" run --path braket --arm classical \
                   --device "$CEPHEUS" --steps "$STEPS" --qubits "$QUBITS" \
                   --seed "$SEED" --fillers "$n" --filler-shots "$FILLER_SHOTS" \
                   --max-usd "$MAX_USD_PER_JOB" --yes \
                   --out "$OUT/runs-flux-cell-co-$n.jsonl") \
            || fail "the pair at $n fillers was not admitted"
        echo "cellpair $n $pair" >>"$EVID/pairs.txt"
        read -r scout classical <<<"$pair"
        wait_for "$classical" 7200 "pair at $n fillers"
        flux job attach "$classical" </dev/null >"$EVID/cell-co-$n.log" 2>&1
        assert_ran "$classical" "$EVID/cell-co-$n.log" "pair at $n fillers"
        date +%s >"$EVID/cell-pair-$n-end"
        say "pair at $n fillers: OK; 60 s for the queue to clear"
        sleep 60
    done
}

# ---------------------------------------------------------------- phase 4
evidence() {
    say "=== phase 4: evidence ==="
    local n scout classical id size r yes
    flux jobs -a -no "{id} {name} {status} {t_submit:.1f} {t_run:.1f} {t_cleanup:.1f}" \
        >"$EVID/jobs.txt"
    flux dmesg >"$EVID/dmesg.txt" 2>&1
    cp /etc/flux/system/scheduling.json "$EVID/scheduling.json" 2>/dev/null
    grep '^cellpair' "$EVID/pairs.txt" | while read -r _ n scout classical; do
        # a scout outliving its classical would block the attach below
        flux job wait-event -t 60 "$scout" clean </dev/null >/dev/null 2>&1 \
            || flux cancel "$scout" 2>/dev/null
        flux job attach "$scout" </dev/null >"$EVID/cell-scout-$n.log" 2>&1
    done
    for id in $(awk '{for (f = 3; f <= NF; f++) if ($f != "-") print $f}' \
                    "$EVID/pairs.txt"); do
        flux job eventlog "$id" >"$EVID/eventlog-$id.txt" 2>/dev/null
    done
    # base64 because flux exec is a text channel: it truncated a raw tar at
    # its first NUL byte (C4 attempt 4's first evidence copy)
    size=$(flux getattr size 2>/dev/null || echo 1)
    for r in $(seq 0 $((size - 1))); do
        flux exec -r "$r" sh -c \
            "cd $OUT 2>/dev/null && tar cf - runs-flux-cell-*.jsonl 2>/dev/null | base64" \
            2>/dev/null </dev/null | base64 -d >"$EVID/runs-flux-rank$r.tar" 2>/dev/null
        if tar tf "$EVID/runs-flux-rank$r.tar" >/dev/null 2>&1; then
            say "rank $r records: $(tar tf "$EVID/runs-flux-rank$r.tar" | tr '\n' ' ')"
        else
            rm -f "$EVID/runs-flux-rank$r.tar"
            say "WARNING rank $r returned no readable records; fetch by hand:"
            say "    flux exec -r $r sh -c 'cd $OUT && tar cf - runs-flux-cell-*.jsonl | base64' | base64 -d > runs-flux-rank$r.tar"
        fi
    done

    yes=$(grep -lE '"steps_per_hour"' "$EVID"/cell-base-*.log "$EVID"/cell-co-*.log \
              2>/dev/null | wc -l)
    if [ "$yes" -eq 6 ]; then
        say "CELLS VERDICT: OK (6/6 chains ran; 3 of them inside their hold)"
    else
        say "CELLS VERDICT: DEGRADED ($yes/6); mark the others"
    fi
    say "evidence in $EVID"
}

# ------------------------------------------------------------------ main
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
        say "NOTE a cancelled cell may leave its fillers queued; check the"
        say "Braket console (region $REGION) and cancel leftover tasks"
        [ "$SPENT" = 1 ] && evidence
    fi
    sleep 1   # let the transcript tee flush before the shell returns
}

case "${1:-check}" in
    check) check_state ;;
    run)
        mkdir -p "$EVID"
        exec > >(tee "$EVID/transcript.log") 2>&1
        say "evidence directory: $EVID"
        trap finish EXIT
        check_state
        mock_rehearsal
        node_rehearsal
        confirm_cost
        canary
        cells
        evidence
        ;;
    *) fail "usage: fluxcells.sh check|run" ;;
esac
