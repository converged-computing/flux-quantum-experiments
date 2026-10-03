#!/bin/bash
# Coscheduling experiments against IonQ, on the simulator or a QPU.
#
#   OUT=results/e1.csv scripts/experiment.sh e1
#
#   e1   overhead of the pair, one at a time: every milestone from submit to
#        the first result, against two baselines
#
# Arms
#   plain        one job, no session. Submits its circuits as anyone would.
#   session      one job. Takes its nodes, opens a session, runs inside it.
#   coscheduled  the pair. The classical is held, the scout opens the session
#                and warms it up, the classical is released into it.
#
# Raw timestamps only. Flux's come from the eventlogs into the CSV, IonQ's
# come from the workload's JSON lines, saved beside the CSV per trial.
# metrics.py derives everything else.
#
# DRY_RUN=1, the default, runs on the simulator with the noise model of
# TARGET. DRY_RUN=0 runs on TARGET itself, which bills.

set -u

WHAT="${1:-e1}"
HERE=$(cd "$(dirname "$0")" && pwd)
WORKLOAD="$HERE/workload.py"
TRIALS="${TRIALS:-5}"
SIZE="${SIZE:-4}"               # cores for the classical job
ITERS="${ITERS:-1}"             # circuits per classical job
SHOTS="${SHOTS:-100}"
TARGET="${TARGET:-qpu.forte-1}" # what the noise model, or the run, is for
DRY_RUN="${DRY_RUN:-1}"
HOLD="${HOLD:-session}"         # session or probe, for the coscheduled arm
TIMEOUT="${TIMEOUT:-600}"
OUT="${OUT:-}"
FRESH="${FRESH:-0}"
CORES=$(flux resource list -s all -no "{ncores}" 2>/dev/null || echo 0)

# the baselines have no scout to set the target, so set the same one here
if [ "$DRY_RUN" = 1 ]; then
    BASE_TARGET="simulator"
    case "$TARGET" in qpu.*) NOISE="${TARGET#qpu.}" ;; *) NOISE="" ;; esac
    DRY_FLAG="--quantum-dry-run"
else
    BASE_TARGET="$TARGET"
    NOISE=""
    DRY_FLAG=""
fi
export IONQ_BACKEND="$BASE_TARGET"
[ -n "$NOISE" ] && export IONQ_NOISE_MODEL="$NOISE"

say() { echo "[$(date +%H:%M:%S)] $*" >&2; }
shuffled() { printf '%s\n' "$@" | shuf; }
emit() {
    echo "$*"
    [ -n "$OUT" ] && echo "$*" >> "$OUT"
    return 0
}
ev() { flux job eventlog "$1" 2>/dev/null | awk -v e="$2" '$2==e {print $1; exit}'; }
memo_ts() {
    flux job eventlog "$1" 2>/dev/null | awk '$2=="memo" && /quantum_session/ {print $1; exit}'
}
memo_session() {
    flux job eventlog "$1" 2>/dev/null | grep memo | grep -o '"quantum_session": *"[^"]*"' | head -1 | cut -d'"' -f4
}
cancel_jobs() {
    local id
    for id in "$@"; do
        [ -n "$id" ] || continue
        flux cancel "$id" >/dev/null 2>&1
        flux job wait-event -t 30 "$id" clean >/dev/null 2>&1
    done
}
wait_clean() {
    flux job wait-event -t "$TIMEOUT" "$1" clean </dev/null >/dev/null 2>&1 && return 0
    say "TIMEOUT on $1 after ${TIMEOUT}s"; return 1
}
drain() {
    for _ in $(seq 1 60); do
        flux jobs -no "{id}" 2>/dev/null | grep -q . || return 0
        sleep 1
    done
    say "WARNING instance did not drain"
}

HDR="exp,arm,trial,batch,size,iters,submit,scout_start,priority,alloc,start,finish,scout_finish,session,timedout"
JOBS_DIR="${OUT:+${OUT%.csv}-jobs}"

have_row() {
    [ -n "$OUT" ] && [ -f "$OUT" ] || return 1
    awk -F, -v e="$1" -v a="$2" -v t="$3" -v b="$4" -v s="$5" -v i="$6" '
        $1 == e && $2 == a && $3 == t && $4 == b && $5 == s && $6 == i { found = 1; exit }
        END { exit !found }' "$OUT"
}

header() {
    [ -n "$OUT" ] && [ "$FRESH" = 1 ] && : > "$OUT"
    [ -n "$JOBS_DIR" ] && mkdir -p "$JOBS_DIR"
    if [ -n "$OUT" ] && [ -s "$OUT" ]; then
        if [ "$(head -1 "$OUT")" != "$HDR" ]; then
            say "REFUSING to resume from $OUT, its header does not match this script"
            exit 1
        fi
        say "resuming from $OUT with $(( $(wc -l < "$OUT") - 1 )) rows already done"
        echo "$HDR"
    else
        emit "$HDR"
    fi
}

# submit one arm. Prints "main scout", scout empty for the baselines
submit_arm() {
    local arm="$1" err main scout
    case "$arm" in
    plain)
        main=$(flux submit -n"$SIZE" -- flux python "$WORKLOAD" --no-session \
            --iterations "$ITERS" --shots "$SHOTS")
        echo "$main"
        ;;
    session)
        main=$(flux submit -n"$SIZE" -- flux python "$WORKLOAD" --own-session \
            --iterations "$ITERS" --shots "$SHOTS")
        echo "$main"
        ;;
    coscheduled)
        err=$(mktemp)
        # shellcheck disable=SC2086
        scout=$(flux submit --quantum-vendor ionq --quantum-device "$TARGET" \
            --quantum-hold "$HOLD" $DRY_FLAG -n"$SIZE" \
            -- flux python "$WORKLOAD" --iterations "$ITERS" --shots "$SHOTS" 2>"$err")
        main=$(grep -oE 'held classical job [0-9]+' "$err" | awk '{print $NF}')
        [ -z "$main" ] && sed 's/^/  /' "$err" >&2
        rm -f "$err"
        echo "$main $scout"
        ;;
    esac
}

# wait for a pair and write its row. $5 is the batch it was submitted in
record() {
    local exp="$1" arm="$2" trial="$3" main="$4" batch="$5" scout="${6:-}"
    local to=0 submit priority alloc start finish s_start s_finish session
    wait_clean "$main" || to=1
    if [ -n "$scout" ] && [ "$to" = 0 ]; then
        wait_clean "$scout" || to=1
    fi
    submit=$(ev "$main" submit); alloc=$(ev "$main" alloc)
    start=$(ev "$main" start); finish=$(ev "$main" finish)
    priority=""; s_start=""; s_finish=""; session=""
    if [ -n "$scout" ]; then
        priority=$(memo_ts "$main"); session=$(memo_session "$main")
        s_start=$(ev "$scout" start); s_finish=$(ev "$scout" finish)
    fi
    if [ -n "$JOBS_DIR" ]; then
        flux job attach "$main" </dev/null > "$JOBS_DIR/$exp-$arm-$trial-$batch.jsonl" 2>&1
        [ -n "$scout" ] && flux job attach "$scout" </dev/null > "$JOBS_DIR/$exp-$arm-$trial-$batch.scout" 2>&1
    fi
    [ "$to" = 1 ] && { say "CENSORED $exp $arm trial=$trial"; cancel_jobs "$main" "$scout"; }
    emit "$exp,$arm,$trial,$batch,$SIZE,$ITERS,$submit,$s_start,$priority,$alloc,$start,$finish,$s_finish,$session,$to"
}

# one arm, one pair at a time
trial() {
    local exp="$1" arm="$2" t="$3" ids main scout
    if have_row "$exp" "$arm" "$t" 1 "$SIZE" "$ITERS"; then
        say "skip $exp $arm trial=$t, already measured"
        return 0
    fi
    ids=$(submit_arm "$arm")
    main=${ids%% *}; scout=${ids#* }; [ "$scout" = "$ids" ] && scout=""
    [ -z "$main" ] && { say "$exp $arm trial=$t: nothing submitted"; return 1; }
    say "$exp $arm trial=$t main=$main${scout:+ scout=$scout}"
    record "$exp" "$arm" "$t" "$main" 1 "$scout"
    drain
}

run_e1() {
    for t in $(seq 1 "$TRIALS"); do
        for arm in $(shuffled plain session coscheduled); do
            trial e1 "$arm" "$t"
        done
    done
}

record_config() {
    [ -n "$OUT" ] || return 0
    {
        echo "cores        $CORES"
        echo "target       $TARGET"
        echo "dry_run      $DRY_RUN"
        echo "base_target  $BASE_TARGET"
        echo "noise        ${NOISE:-none}"
        echo "hold         $HOLD"
        echo "size         $SIZE"
        echo "iters        $ITERS"
        echo "shots        $SHOTS"
        echo "timeout      $TIMEOUT"
        echo "endpoint     ${IONQ_API_URL:-https://api.ionq.co/v0.4}"
        echo "queue_policy $(flux module stats sched-fluxion-qmanager 2>/dev/null | flux python -c "
import json,sys
print(sorted({v.get('policy','?') for v in json.load(sys.stdin).get('queues',{}).values()})[0])
" 2>/dev/null || echo unknown)"
        qplug=$(flux jobtap list 2>/dev/null | grep -v '^\.' | head -1)
        flux jobtap query "${qplug:-quantum.so}" 2>/dev/null | flux python -c "
import json, sys
try:
    q = json.load(sys.stdin)
except Exception:
    q = {}
for k in ('total_cores', 'reserve_cores', 'preempt_after', 'vendors'):
    print('%-12s %s' % (k, q.get(k, 'unknown')))
" 2>/dev/null || echo "plugin_conf  unknown"
        echo "flux_quantum $(flux python -c 'import flux_quantum, os, subprocess; d=os.path.dirname(flux_quantum.__file__); print(subprocess.run(["git","-C",d,"rev-parse","--short","HEAD"],capture_output=True,text=True).stdout.strip() or "unknown")' 2>/dev/null)"
        echo "started      $(date -Is)"
    } > "${OUT%.csv}.meta"
    say "config recorded in ${OUT%.csv}.meta"
}

say "cores=$CORES target=$TARGET dry_run=$DRY_RUN hold=$HOLD size=$SIZE iters=$ITERS"
record_config
header
case "$WHAT" in
    e1) run_e1 ;;
    *) echo "usage: $0 e1" >&2; exit 2 ;;
esac
