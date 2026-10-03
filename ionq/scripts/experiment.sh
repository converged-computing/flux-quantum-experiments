#!/bin/bash
# Coscheduling experiments against IonQ, on the simulator or a QPU.
#
#   OUT=results/e1.csv scripts/experiment.sh e1
#
#   e1   overhead of the pair, one at a time: every milestone from submit to
#        the first result, against two baselines
#   e2   pairs submitted at once against the qpu cap: admission and makespan
#   e3   an iterative workload inside one session, per circuit latency
#   e4   the hold is released whatever happens to the pair
#   e5   cores held through the device's queue, fake only: the queue is swept
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
TASKS="${TASKS:-$SIZE}"         # tasks it runs, each running the workload. SIZE, as
                                # the fake campaign ran. 1 on a device, where every
                                # task's copy of a circuit bills
ARMS="${ARMS:-plain session coscheduled}"
ITERS4="${ITERS4:-60}"          # e4 wants a long job to interrupt: circuits, and
THINK4="${THINK4:-2}"           # classical seconds between them
ITERS="${ITERS:-1}"             # circuits per classical job
SHOTS="${SHOTS:-100}"
TARGET="${TARGET:-qpu.forte-1}" # what the noise model, or the run, is for
DRY_RUN="${DRY_RUN:-1}"
HOLD="${HOLD:-session}"         # session or probe, for the coscheduled arm
TIMEOUT="${TIMEOUT:-600}"
OUT="${OUT:-}"
FRESH="${FRESH:-0}"
QUEUE="${QUEUE:-}"              # the fake's queue for a row, set by e5
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
    # the eventlog's text form is: <ts> memo quantum_session="<id>" released=1
    flux job eventlog "$1" 2>/dev/null | awk '$2=="memo"' | grep -o 'quantum_session="[^"]*"' | head -1 | cut -d'"' -f2
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

HDR="exp,arm,trial,batch,size,iters,submit,scout_start,priority,alloc,start,finish,scout_finish,session,timedout,queue"
# e4 is a set of checks, not a sweep, so it has its own columns
HDR4="exp,check,trial,main,scout,session,session_status,classical_rc,scout_rc,note"
[ "$WHAT" = e4 ] && HDR="$HDR4"
JOBS_DIR="${OUT:+${OUT%.csv}-jobs}"

have_row() {
    [ -n "$OUT" ] && [ -f "$OUT" ] || return 1
    awk -F, -v e="$1" -v a="$2" -v t="$3" -v b="$4" -v s="$5" -v i="$6" -v q="${7:-}" '
        $1 == e && $2 == a && $3 == t && $4 == b && $5 == s && $6 == i && (q == "" || $16 == q) { found = 1; exit }
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
        main=$(flux submit -n"$TASKS" -c"$((SIZE / TASKS))" -- flux python "$WORKLOAD" --no-session \
            --iterations "$ITERS" --shots "$SHOTS")
        echo "$main"
        ;;
    session)
        main=$(flux submit -n"$TASKS" -c"$((SIZE / TASKS))" -- flux python "$WORKLOAD" --own-session \
            --iterations "$ITERS" --shots "$SHOTS")
        echo "$main"
        ;;
    coscheduled)
        err=$(mktemp)
        # shellcheck disable=SC2086
        scout=$(flux submit --quantum-vendor ionq --quantum-device "$TARGET" \
            --quantum-hold "$HOLD" $DRY_FLAG -n"$TASKS" -c"$((SIZE / TASKS))" \
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
        # the name carries what the sweeps vary, the circuit count for e3 and
        # the queue for e5, so a sweep does not overwrite its own records
        local stem="$JOBS_DIR/$exp-$arm-$trial-$batch-i$ITERS${QUEUE:+-q$QUEUE}"
        flux job attach "$main" </dev/null > "$stem.jsonl" 2>&1
        [ -n "$scout" ] && flux job attach "$scout" </dev/null > "$stem.scout" 2>&1
    fi
    [ "$to" = 1 ] && { say "CENSORED $exp $arm trial=$trial"; cancel_jobs "$main" "$scout"; }
    emit "$exp,$arm,$trial,$batch,$SIZE,$ITERS,$submit,$s_start,$priority,$alloc,$start,$finish,$s_finish,$session,$to,$QUEUE"
}

# one arm, one pair at a time
trial() {
    local exp="$1" arm="$2" t="$3" ids main scout
    if have_row "$exp" "$arm" "$t" 1 "$SIZE" "$ITERS" "$QUEUE"; then
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
        for arm in $(shuffled $ARMS); do
            trial e1 "$arm" "$t"
        done
    done
}

run_e2() {
    # k pairs submitted back to back. The qpu count under qdevice_ionq caps how
    # many hold a device at once, and the plugin's budget caps admission, so
    # rows per batch say how many were created and the timestamps say how
    # they were served. A refused pair leaves no row
    local k t i ids main scout mains scouts
    for k in ${PAIRS:-1 2 4 8}; do
        for t in $(seq 1 "$TRIALS"); do
            if have_row e2 coscheduled "$t" "$k" "$SIZE" "$ITERS"; then
                say "skip e2 pairs=$k trial=$t, already measured"
                continue
            fi
            mains=""; scouts=""
            for i in $(seq 1 "$k"); do
                ids=$(submit_arm coscheduled)
                main=${ids%% *}; scout=${ids#* }
                [ -n "$main" ] && { mains="$mains $main"; scouts="$scouts $scout"; }
            done
            say "e2 pairs=$k trial=$t created=$(echo $mains | wc -w)"
            set -- $scouts
            for main in $mains; do
                record e2 coscheduled "$t" "$main" "$k" "$1"
                shift
            done
            drain
        done
    done
}

run_e3() {
    # the same three arms, with more circuits per job. What a session buys an
    # iterative workload is in the per circuit latency, and on a QPU in
    # whether later circuits queue again
    local iters t arm
    for iters in ${ITERS_SWEEP:-1 5 20}; do
        ITERS="$iters"
        for t in $(seq 1 "$TRIALS"); do
            for arm in $(shuffled $ARMS); do
                trial e3 "$arm" "$t"
            done
        done
    done
}

fake_queue() {
    # set the fake's queue. Only the fake has the route, so against IonQ
    # this says so and e5 does not run
    flux python - "$1" <<'EOF'
import sys
from flux_quantum.backends.ionq import IonQBackend
try:
    got = IonQBackend().client.post("/fake/config", {"queue": sys.argv[1]})
except Exception as e:
    sys.exit("e5 needs the fake service, and this endpoint is not it: %s" % e)
print("  fake queue now %s s" % got.get("queue"))
EOF
}

run_e5() {
    # the three arms against a device queue. A plain job holds its cores
    # through the queue for every circuit, a session job for its first, the
    # pair for none: the scout waits it out on one core. The queue is swept
    # and the row records it. Fake only
    local q t arm
    for q in ${QUEUE_SWEEP:-0 10 30 60}; do
        fake_queue "$q" >&2 || exit 1
        QUEUE="$q"
        for t in $(seq 1 "$TRIALS"); do
            for arm in $(shuffled $ARMS); do
                trial e5 "$arm" "$t"
            done
        done
    done
    fake_queue 0 >&2
}

session_status() {
    # what the service says about a session now, or none for a probe handover
    case "$1" in ""|job:*) echo none; return ;; esac
    flux python -c "
from flux_quantum.backends.ionq import IonQBackend
try:
    print(IonQBackend().client.get('/sessions/$1').get('status'))
except Exception as e:
    print('error')" 2>/dev/null
}

# one e4 check: submit a long pair, do something to it, see what is left
check() {
    local name="$1" trial="$2" extra="$3" action="$4" ids main scout session status crc src note
    err=$(mktemp)
    # shellcheck disable=SC2086
    scout=$(flux submit --quantum-vendor ionq --quantum-device "$TARGET" \
        --quantum-hold "$HOLD" $DRY_FLAG $extra -n"$TASKS" -c"$((SIZE / TASKS))" \
        -- flux python "$WORKLOAD" --iterations "$ITERS4" --think "$THINK4" --shots "$SHOTS" 2>"$err")
    main=$(grep -oE 'held classical job [0-9]+' "$err" | awk '{print $NF}')
    rm -f "$err"
    [ -z "$main" ] && { emit "e4,$name,$trial,,,,,,,no pair created"; return; }
    say "e4 $name trial=$trial main=$main scout=$scout"
    note=""
    case "$action" in
    cancel-classical)
        flux job wait-event -t 120 "$main" start </dev/null >/dev/null 2>&1 || note="classical never started"
        sleep 5; flux cancel "$main" >/dev/null 2>&1 ;;
    cancel-scout)
        flux job wait-event -t 120 "$main" start </dev/null >/dev/null 2>&1 || note="classical never started"
        sleep 5; flux cancel "$scout" >/dev/null 2>&1 ;;
    wait)
        # the pair ends by itself: the hold expires, or the scout gives up
        ;;
    esac
    flux job wait-event -t "$TIMEOUT" "$main" clean </dev/null >/dev/null 2>&1 || note="$note; classical did not end"
    flux job wait-event -t "$TIMEOUT" "$scout" clean </dev/null >/dev/null 2>&1 || note="$note; scout did not end"
    session=$(memo_session "$main")
    # a scout that gave up before the memo names the session it closed in its output
    [ -z "$session" ] && session=$(flux job attach "$scout" </dev/null 2>&1 \
        | grep -oE 'session[= ][0-9a-f-]{36}' | head -1 | grep -oE '[0-9a-f-]{36}')
    status=$(session_status "$session")
    crc=$(flux jobs -a -no "{returncode}" "$main" 2>/dev/null)
    src=$(flux jobs -a -no "{returncode}" "$scout" 2>/dev/null)
    if [ -n "$JOBS_DIR" ]; then
        flux job attach "$main" </dev/null > "$JOBS_DIR/e4-$name-$trial.jsonl" 2>&1
        flux job attach "$scout" </dev/null > "$JOBS_DIR/e4-$name-$trial.scout" 2>&1
    fi
    emit "e4,$name,$trial,$main,$scout,$session,$status,$crc,$src,${note#; }"
    cancel_jobs "$main" "$scout"
    drain
}

run_e4() {
    # Whatever happens to the pair, the session must end: a session left
    # started is billed and blocks the device for everyone. Every check
    # records what the service says about the session afterwards
    local t
    for t in $(seq 1 "${TRIALS4:-2}"); do
        check cancel-classical "$t" "" cancel-classical
        check cancel-scout "$t" "" cancel-scout
        # the session's own limit is a minute, the workload wants longer
        check hold-max "$t" "--quantum-hold-max 60" wait
        # the scout gives up before the hold is active and cancels the held job
        check wait "$t" "--quantum-wait 1" wait
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
        echo "tasks        $TASKS"
        echo "arms         $ARMS"
        [ "$WHAT" = e4 ] && echo "iters4       $ITERS4 think $THINK4"
        echo "timeout      $TIMEOUT"
        echo "endpoint     ${IONQ_API_URL:-https://api.ionq.co/v0.4}"
        echo "fake_queue   ${FAKE_IONQ_QUEUE:-0}"
        [ "$WHAT" = e5 ] && echo "queue_sweep  ${QUEUE_SWEEP:-0 10 30 60}"
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
    e2) run_e2 ;;
    e3) run_e3 ;;
    e4) run_e4 ;;
    e5) run_e5 ;;
    *) echo "usage: $0 {e1|e2|e3|e4|e5}" >&2; exit 2 ;;
esac
