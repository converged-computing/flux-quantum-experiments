#!/bin/bash
# Coscheduling experiments on the mock vendor.
#
#   OUT=results/e1.csv scripts/experiment.sh e1
#
#   e1   allocation held, against vendor queue depth
#   e2   the arms under classical contention
#   e3   core seconds consumed, against allocation size
#   e4   does the knee track allocation size, swept over spare cores
#   e5   what contention costs, against how long the other work has left
#   e6   does admission mean the pair will run
#
# Raw timestamps only. analyze.py and plot.py derive everything else.
#
# Arms
#   baseline     one job. It takes its nodes, then opens the session and waits
#                in the vendor queue while holding them.
#   coscheduled  the classical is held, the scout waits, the classical starts
#                once the QPU is ours.
#
# The mock vendor's queue is set through FLUX_QUANTUM_MOCK_* in the submit
# environment, so this needs a flux-quantum with the common submit options.

set -u
export FLUX_QUANTUM_MOCK=1

WHAT="${1:-e1}"
TRIALS="${TRIALS:-10}"
SERVICE="${SERVICE:-0.05}"      # seconds per queued task ahead of us
OVERHEAD="${OVERHEAD:-2}"       # fixed vendor wait at depth 0
WORK="${WORK:-5}"               # seconds of classical work
# A pair rescued by preemption resolves in about preempt_after seconds. Past
# that it is waiting for the load to expire, which it never does, so a long
# timeout only costs hours. e2 and e4 sweep past the knee on purpose.
case "$WHAT" in
    e2|e4) TIMEOUT="${TIMEOUT:-120}" ;;
    *)     TIMEOUT="${TIMEOUT:-600}" ;;
esac
# the load must outlive any trial, so a stranded pair is freed by preemption
# and not by the load expiring
LOAD_SECS="${LOAD_SECS:-900}"
SIZE5="${SIZE5:-8}"             # allocation size for e5
SIZE6="${SIZE6:-8}"             # classical size for e6
OUT="${OUT:-}"                  # also append the CSV here, stdout still gets it
FRESH="${FRESH:-0}"             # 1 to discard OUT and start over
# the load goes in as chunks, so a sweep over cores taken from preemption has
# individual victims to choose between. 0 for one job
LOAD_CHUNK="${LOAD_CHUNK:-8}"
# with no state field flux resource list merges every state onto one line
CORES=$(flux resource list -s all -no "{ncores}" 2>/dev/null)
CORES="${CORES:-128}"

say() { echo "[$(date +%H:%M:%S)] $*" >&2; }
# the arms are shuffled per trial so drift does not bias whichever went first
shuffled() { printf '%s\n' "$@" | shuf; }
emit() {
    echo "$*"
    [ -n "$OUT" ] && echo "$*" >> "$OUT"
    return 0
}

# Resume: skip a row already in OUT. Keyed on load_cores rather than the
# percentage, since two core counts can render as the same percentage.
have_row() {
    [ -n "$OUT" ] && [ -f "$OUT" ] || return 1
    awk -F, -v e="$1" -v a="$2" -v d="$3" -v c="$4" -v s="$5" -v t="$6" -v w="$7" '
        $1 == e && $2 == a && $3 == d && $18 == c && $5 == s && $6 == t && $19 == w {
            found = 1; exit
        }
        END { exit !found }' "$OUT"
}
ev()  { flux job eventlog "$1" 2>/dev/null | awk -v e="$2" '$2==e {print $1; exit}'; }
memo_ts() {
    flux job eventlog "$1" 2>/dev/null | awk '$2=="memo" && /quantum_session/ {print $1; exit}'
}
# one empty id makes flux cancel fail on the whole list
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
    say "TIMEOUT on $1 after ${TIMEOUT}s"; flux jobs -a >&2; return 1
}
drain() {
    for _ in $(seq 1 60); do
        flux jobs -no "{id}" 2>/dev/null | grep -q . || return 0
        sleep 2
    done
    say "WARNING instance did not drain"
}

# background load, so the classical has to compete for cores
LOAD_JOB=""
start_load() {
    local n="$1" secs="$2" free left take id
    LOAD_JOB=""
    [ "$n" -le 0 ] && return 0
    free=$(( CORES - n ))
    if [ "$free" -ge "${SIZE_HINT:-8}" ]; then
        say "NOTE the load leaves $free cores free and the job needs ${SIZE_HINT:-8}, so it will not wait"
    fi
    if [ "$n" -ge "$CORES" ]; then
        say "NOTE the load asks for every core, so it never runs and the cluster stays free"
    fi
    if [ "$LOAD_CHUNK" -le 0 ]; then
        LOAD_JOB=$(flux submit -n"$n" sleep "$secs" 2>/dev/null)
    else
        left="$n"
        while [ "$left" -gt 0 ]; do
            take="$LOAD_CHUNK"
            [ "$take" -gt "$left" ] && take="$left"
            id=$(flux submit -n"$take" sleep "$secs" 2>/dev/null)
            [ -n "$id" ] && LOAD_JOB="${LOAD_JOB:+$LOAD_JOB }$id"
            left=$(( left - take ))
        done
    fi
    sleep 1
}
stop_load() {
    local id
    for id in ${LOAD_JOB:-}; do
        [ -n "$id" ] && flux cancel "$id" >/dev/null 2>&1
    done
    LOAD_JOB=""
}

qopts() {
    printf '{"queue_depth": %s, "service_time": %s, "base_overhead": %s}' \
        "$1" "$SERVICE" "$OVERHEAD"
}

# bg_total and bg_done are used by e6 only, for pairs asked and admitted
HDR="exp,arm,depth,load_pct,size,trial,t0,submit,priority,alloc,start,finish,scout_start,scout_finish,bg_total,bg_done,work_s,load_cores,load_secs,timedout"

header() {
    if [ -n "$OUT" ] && [ "$FRESH" = 1 ]; then
        : > "$OUT"
    fi
    if [ -n "$OUT" ] && [ -s "$OUT" ]; then
        local old_hdr
        old_hdr=$(head -1 "$OUT")
        if [ "$old_hdr" != "$HDR" ]; then
            say "REFUSING to resume from $OUT, its header does not match this script"
            say "  file: $old_hdr"
            say "  this: $HDR"
            say "Move it aside, or rerun with FRESH=1 to discard it."
            exit 1
        fi
        say "resuming from $OUT with $(( $(wc -l < "$OUT") - 1 )) rows already done"
        say "NOTE those rows were measured under the plugin config of the time"
        echo "$HDR"
    else
        emit "$HDR"
    fi
}

row() { emit "$*"; }

# one trial of one arm
trial() {
    local exp="$1" arm="$2" depth="$3" load="$4" size="$5" t="$6"
    export SIZE_HINT="$size"
    local load_cores=$(( CORES * load / 100 ))
    # e4 and e5 place the load by core count so their spare figure is exact
    [ -n "${E4_LOAD_CORES:-}" ] && [ "$exp" = e4 ] && load_cores="$E4_LOAD_CORES"
    [ -n "${E5_LOAD_CORES:-}" ] && [ "$exp" = e5 ] && load_cores="$E5_LOAD_CORES"

    if have_row "$exp" "$arm" "$depth" "$load_cores" "$size" "$t" "$LOAD_SECS"; then
        say "skip $exp $arm depth=$depth cores=$load_cores size=$size trial=$t, already measured"
        return 0
    fi
    local t0 submit priority alloc start finish s_start s_finish
    # 1 when the arm never completed. Kept as a row, since the cell one core
    # short of fitting is the one the knee sweeps exist to measure
    local to=0
    submit=""; priority=""; alloc=""; start=""; finish=""; s_start=""; s_finish=""

    start_load "$load_cores" "$LOAD_SECS"
    t0=$(date +%s.%N)

    case "$arm" in
    baseline)
        local main
        main=$(QOPTS="$(qopts "$depth")" flux submit -n"$size" \
            flux python -c "
import os, json, time
from flux_quantum.backends import get_backend
b = get_backend('mock')
o = json.loads(os.environ['QOPTS'])
s = b.open_session(o)
b.wait_for_priority(o)
time.sleep($WORK)
print('done', s)")
        say "$exp $arm depth=$depth load=$load size=$size trial=$t main=$main"
        wait_clean "$main" || to=1
        submit=$(ev "$main" submit); alloc=$(ev "$main" alloc)
        start=$(ev "$main" start); finish=$(ev "$main" finish)
        [ "$to" = 1 ] && cancel_jobs "$main"
        ;;

    coscheduled)
        local err scout main
        err=$(mktemp)
        scout=$(FLUX_QUANTUM_MOCK_QUEUE="$depth" \
            FLUX_QUANTUM_MOCK_SERVICE_TIME="$SERVICE" \
            FLUX_QUANTUM_MOCK_BASE_OVERHEAD="$OVERHEAD" \
            flux submit --quantum-vendor mock \
            -n"$size" -- sh -c "sleep $WORK; echo got \$QUANTUM_SESSION_ID" 2>"$err")
        main=$(grep -oE 'held classical job [0-9]+' "$err" | awk '{print $NF}')
        rm -f "$err"
        say "$exp $arm depth=$depth load=$load size=$size trial=$t scout=$scout main=$main"
        [ -z "$main" ] && { say "no classical job"; stop_load; return 1; }
        wait_clean "$main" || to=1
        [ "$to" = 0 ] && { wait_clean "$scout" || to=1; }
        submit=$(ev "$main" submit); priority=$(memo_ts "$main")
        alloc=$(ev "$main" alloc); start=$(ev "$main" start); finish=$(ev "$main" finish)
        s_start=$(ev "$scout" start); s_finish=$(ev "$scout" finish)
        [ "$to" = 1 ] && cancel_jobs "$main" "$scout"
        ;;
    esac

    stop_load
    [ "$to" = 1 ] && say "CENSORED $exp $arm size=$size load_cores=$load_cores, did not complete in ${TIMEOUT}s"
    row "$exp,$arm,$depth,$load,$size,$t,$t0,$submit,$priority,$alloc,$start,$finish,$s_start,$s_finish,,,$WORK,$load_cores,$LOAD_SECS,$to"
    drain
}

run_e1() {
    for depth in ${DEPTHS:-0 7 14 100 491}; do
        for t in $(seq 1 "$TRIALS"); do
            for arm in $(shuffled baseline coscheduled); do
                trial e1 "$arm" "$depth" 0 8 "$t"
            done
        done
    done
}

run_e2() {
    # a job needs SIZE cores, so the load has to leave fewer than that free
    # before it waits at all. On 128 cores with size 8 that is above 94%
    for load in ${LOADS:-0 50 90 94 96 98 99}; do
        for t in $(seq 1 "$TRIALS"); do
            for arm in $(shuffled baseline coscheduled); do
                trial e2 "$arm" "${DEPTH:-14}" "$load" 8 "$t"
            done
        done
    done
}

run_e3() {
    for size in ${SIZES:-1 8 32 64 96}; do
        for t in $(seq 1 "$TRIALS"); do
            for arm in $(shuffled baseline coscheduled); do
                trial e3 "$arm" "${DEPTH:-100}" 0 "$size" "$t"
            done
        done
    done
}

run_e4() {
    # sweep spare cores rather than percentages, so every size is sampled at
    # the same points and the curves can be compared
    for size in ${SIZES:-8 16 32 64}; do
        for spare in ${SPARES:--1 0 1 2 3}; do
            local want=$(( CORES - size - spare ))
            [ "$want" -lt 1 ] && continue
            local load=$(( want * 100 / CORES ))
            say "size=$size spare=$spare needs $want cores occupied"
            export E4_LOAD_CORES="$want"
            for t in $(seq 1 "$TRIALS"); do
                for arm in $(shuffled baseline coscheduled); do
                    trial e4 "$arm" "${DEPTH:-14}" "$load" "$size" "$t"
                done
            done
        done
    done
}

run_e5() {
    # A pair on a busy machine waits for the other work to finish while
    # holding a paid session, so the cost is however long that work had left.
    # With preemption it is bounded by preempt_after. Run twice, with and
    # without, and compare.
    local load want
    want=$(( CORES - SIZE5 + 1 ))     # leaves SIZE5 - 1 spare, and the pair needs SIZE5 + 1
    export E5_LOAD_CORES="$want"
    load=$(( want * 100 / CORES ))
    say "e5 occupies $want of $CORES cores, leaving $(( CORES - want )) spare, pair needs $(( SIZE5 + 1 ))"
    for LOAD_SECS in ${LOAD_LIVES:-15 30 60 120}; do
        export LOAD_SECS
        say "other work lives ${LOAD_SECS}s"
        for t in $(seq 1 "$TRIALS"); do
            for arm in $(shuffled baseline coscheduled); do
                trial e5 "$arm" "${DEPTH:-14}" "$load" "$SIZE5" "$t"
            done
        done
    done
}

run_e6() {
    # Several pairs at once against a budget too small for all of them. A
    # quota admits whenever the arithmetic fits; a capacity test admits what
    # can be reached. Needs the plugin loaded with a small total_cores.
    local admitted rejected err
    for k in ${PAIRS:-1 2 3 4 5 6}; do
        for t in $(seq 1 "$TRIALS"); do
            admitted=0
            rejected=0
            for _ in $(seq 1 "$k"); do
                err=$(mktemp)
                FLUX_QUANTUM_MOCK_QUEUE="${DEPTH:-14}" \
                    FLUX_QUANTUM_MOCK_SERVICE_TIME="$SERVICE" \
                    FLUX_QUANTUM_MOCK_BASE_OVERHEAD="$OVERHEAD" \
                    flux submit --quantum-vendor mock \
                        -n"$SIZE6" -- sleep "$WORK" >/dev/null 2>"$err"
                if grep -q "held classical job" "$err"; then
                    admitted=$(( admitted + 1 ))
                else
                    rejected=$(( rejected + 1 ))
                fi
                rm -f "$err"
            done
            say "e6 pairs=$k trial=$t admitted=$admitted rejected=$rejected"
            row "e6,coscheduled,${DEPTH:-14},0,$SIZE6,$t,,,,,,,,,$k,$admitted,$WORK,0,$LOAD_SECS,0"
            flux cancel --all >/dev/null 2>&1
            drain
        done
    done
}

record_config() {
    [ -n "$OUT" ] || return 0
    {
        echo "cores       $CORES"
        echo "queue_policy $(flux module stats sched-fluxion-qmanager 2>/dev/null \
            | flux python -c "
import json,sys
print(sorted({v.get('policy','?') for v in json.load(sys.stdin).get('queues',{}).values()})[0])
" 2>/dev/null || echo unknown)"
        echo "jobtap      $(flux jobtap list 2>/dev/null | tr '\n' ' ')"
        # read back from the plugin, so it is what the run actually used
        qplug=$(flux jobtap list 2>/dev/null | grep -v '^\.' | head -1)
        flux jobtap query "${qplug:-quantum.so}" 2>/dev/null | flux python -c "
import json, sys
try:
    q = json.load(sys.stdin)
except Exception:
    q = {}
for k in ('total_cores', 'reserve_cores', 'preempt_after', 'vendors'):
    print('%-12s %s' % (k, q.get(k, 'unknown')))
" 2>/dev/null || echo "plugin_conf unknown"
        echo "load_secs   $LOAD_SECS"
        echo "work        $WORK"
        echo "overhead    $OVERHEAD"
        echo "service     $SERVICE"
        echo "timeout     $TIMEOUT"
        echo "load_chunk  $LOAD_CHUNK"
        echo "started     $(date -Is)"
    } > "${OUT%.csv}.meta"
    say "config recorded in ${OUT%.csv}.meta"
}

say "cluster has $CORES cores"
record_config
header
case "$WHAT" in
    e1) run_e1 ;;
    e2) run_e2 ;;
    e3) run_e3 ;;
    e4) run_e4 ;;
    e5) run_e5 ;;
    e6) run_e6 ;;
    all) run_e1; run_e2; run_e3; run_e4; run_e5 ;;
    *) echo "usage: $0 {e1|e2|e3|e4|e5|e6|all}" >&2; exit 2 ;;
esac
