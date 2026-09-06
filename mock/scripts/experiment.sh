#!/bin/bash
# Coscheduling experiments on the mock vendor. Free, repeatable, and the queue
# depth is a knob, which no real vendor offers.
#
#   ./experiment.sh e1 > e1.csv     release latency against vendor queue depth
#   ./experiment.sh e2 > e2.csv     the arms under classical contention
#   ./experiment.sh e3 > e3.csv     node seconds wasted against allocation size
#   ./experiment.sh e4 > e4.csv     does the knee track allocation size
#   ./experiment.sh e5 > e5.csv     what contention costs, against how long the
#                                   ordinary work has left to run
#   ./experiment.sh all > all.csv
#
# Raw timestamps only. analyze.py derives everything else, so the analysis can
# be redone without spending another run.
#
# Arms
#   baseline       one ordinary job. It takes its nodes, then opens the session
#                and waits in the vendor queue while holding them. The status quo.
#   coscheduled  ours. The classical is held, the scout waits, the classical
#                starts once the QPU is ours.
#                behind other work. The workflow tool shape.
#   nowarmup     ours without the priority check, to separate what the warmup
#                buys from what coscheduling buys.

set -u
export FLUX_QUANTUM_MOCK=1

WHAT="${1:-e1}"
TRIALS="${TRIALS:-10}"     # enough for a mean and a spread worth reporting
SERVICE="${SERVICE:-0.05}"      # seconds per queued task ahead of us
OVERHEAD="${OVERHEAD:-2}"       # fixed vendor wait at depth 0
WORK="${WORK:-5}"               # seconds of classical work
# A pair rescued by preemption resolves in about preempt_after seconds. Anything
# still waiting after that is waiting for the background load to expire, which
# by design it never does, so a long timeout buys nothing and costs hours. e2
# and e4 sweep past the knee on purpose and so spend most of their wall clock
# here.
case "$WHAT" in
    e2|e4) TIMEOUT="${TIMEOUT:-120}" ;;
    *)     TIMEOUT="${TIMEOUT:-600}" ;;
esac
# The background load must outlive any trial, so that a stranded pair is freed
# by preemption and not by the load expiring underneath it.
LOAD_SECS="${LOAD_SECS:-900}"
SIZE5="${SIZE5:-8}"             # allocation size for e5
OUT="${OUT:-}"                  # also append the CSV here, stdout still gets it
FRESH="${FRESH:-0}"             # 1 to discard OUT and start over
# Competing stream, to measure whether the nodes an arm sits on were usable by
# anyone else. Two things have to hold or it measures nothing.
#
#   BG_JOBS > CORES              or every job runs at once and the drain time
#                                is just BG_SECS whatever the arm did
#   BG_JOBS * BG_SECS > free * wait   or the stream finishes before the hold
#
# Off by default, because the stream is submitted before the trial job and so
# fills the cluster, which delays the SCOUT rather than the classical. That
# masks the effect e2 is looking for. Raise it only when you want the
# displacement measurement, and then give the trial job a higher urgency so it
# allocates first.
BG_JOBS="${BG_JOBS:-1}"
BG_SECS="${BG_SECS:-1}"
# Total cores, which is what the sweeps size against. Ask for the total
# explicitly. A format string with no state field makes flux resource list merge
# free, allocated and down onto one line, so the bare -no {ncores} form returns
# the total whatever is running, and reads as if it returned the free count.
# That misreading is what made the background load look like it was not
# occupying the machine. -s all says what is meant, and excludes nothing.
CORES=$(flux resource list -s all -no "{ncores}" 2>/dev/null)
CORES="${CORES:-128}"
# Free cores, for anything that needs the live figure. Note this is the one
# place -s free belongs, and it is not interchangeable with the above.
free_cores() { flux resource list -s free -no "{ncores}" 2>/dev/null; }

say() { echo "[$(date +%H:%M:%S)] $*" >&2; }
# the arms always ran in the same order, so anything that drifts over a trial
# biased whichever went first. Shuffle instead.
shuffled() { printf '%s\n' "$@" | shuf; }
# CSV to stdout, and to OUT when set, so a long run is not lost to a closed pipe
emit() {
    echo "$*"
    [ -n "$OUT" ] && echo "$*" >> "$OUT"
    return 0
}

# Resume. A long sweep gets interrupted, and rerunning the points already
# measured wastes hours, so skip any row already present in OUT.
have_row() {
    [ -n "$OUT" ] && [ -f "$OUT" ] || return 1
    # keyed on load_cores, field 18, not the percentage. want=96 and want=97
    # both render as 75 percent on a 128 core machine, so keying on the
    # percentage silently skipped one spare value per size.
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
# Cancel only the ids that exist. An empty argument makes flux cancel fail on
# the whole list, which leaves the rest running into the next trial.
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

# background load, so the classical actually has to compete for nodes
LOAD_JOB=""

start_load() {
    local n="$1" secs="$2" free
    LOAD_JOB=""
    [ "$n" -le 0 ] && return 0
    free=$(( CORES - n ))
    if [ "$free" -ge "${SIZE_HINT:-8}" ]; then
        say "NOTE the load leaves $free cores free and the job needs ${SIZE_HINT:-8}, so it will not have to wait"
    fi
    if [ "$n" -ge "$CORES" ]; then
        say "NOTE the load asks for every core, so the load job never runs and the cluster stays free"
    fi
    # capture the id. Letting the load expire on its own would free the cores
    # and rescue a stranded pair, so the measurement would be the load lifetime
    # rather than anything about the scheduler.
    LOAD_JOB=$(flux submit -n"$n" sleep "$secs" 2>/dev/null)
    sleep 1
}

stop_load() {
    [ -n "${LOAD_JOB:-}" ] && flux cancel "$LOAD_JOB" >/dev/null 2>&1
    LOAD_JOB=""
}

# a stream of small jobs, so we can count how much other work got through while
# the vendor queue was draining. This is where coscheduling actually pays.
start_stream() {
    local tag="$1"
    # one command rather than a loop, otherwise submitting hundreds of jobs
    # takes longer than the window we are trying to measure
    flux submit --quiet --cc="1-$BG_JOBS" --job-name="$tag" \
        -n1 sleep "$BG_SECS" >/dev/null 2>&1
    # watch it in the background and record when the last one clears. Counting
    # after the trial is useless, they have all finished by then whatever the
    # arm did. How long the stream takes to drain is the real signal.
    (
        while flux jobs --name="$tag" -no "{id}" 2>/dev/null | grep -q .; do
            sleep 0.2
        done
        date +%s.%N > "/tmp/stream-$tag"
    ) &
}
stream_drained() {
    local tag="$1"
    for _ in $(seq 1 300); do
        [ -f "/tmp/stream-$tag" ] && { cat "/tmp/stream-$tag"; rm -f "/tmp/stream-$tag"; return; }
        sleep 0.2
    done
    echo ""
}
stream_total() { echo "$BG_JOBS"; }

qopts() {
    printf '{"queue_depth": %s, "service_time": %s, "base_overhead": %s}' \
        "$1" "$SERVICE" "$OVERHEAD"
}

HDR="exp,arm,depth,load_pct,size,trial,t0,submit,priority,alloc,start,finish,scout_start,scout_finish,bg_total,bg_done,work_s,load_cores,load_secs,timedout"

header() {
    if [ -n "$OUT" ] && [ "$FRESH" = 1 ]; then
        : > "$OUT"
    fi
    if [ -n "$OUT" ] && [ -s "$OUT" ]; then
        # Resume matches rows field by field, so a CSV written by an older
        # version of this script silently matches nothing and every point is
        # measured again. Worse, the old rows stay in the file and get mixed
        # with the new ones under a .meta that describes only this run. Refuse.
        local old_hdr
        old_hdr=$(head -1 "$OUT")
        if [ "$old_hdr" != "$HDR" ]; then
            say "REFUSING to resume from $OUT, its header does not match this script"
            say "  file: $old_hdr"
            say "  this: $HDR"
            say "Those rows were measured under different conditions. Move the file"
            say "aside, or rerun with FRESH=1 to discard it."
            exit 1
        fi
        say "resuming from $OUT with $(( $(wc -l < "$OUT") - 1 )) rows already done"
        say "NOTE resume keeps the existing rows. If the plugin config changed,"
        say "     in particular preempt_after, those rows do not describe this run."
        echo "$HDR"          # stdout only, OUT already has one
    else
        emit "$HDR"
    fi
}

row() { emit "$*"; }

# ---------------------------------------------------------------------------
# one trial of one arm
# ---------------------------------------------------------------------------
trial() {
    local exp="$1" arm="$2" depth="$3" load="$4" size="$5" t="$6"
    export SIZE_HINT="$size"
    # load arrives as a percentage, convert once so the load job and the
    # recorded core count agree
    local load_cores=$(( CORES * load / 100 ))
    # e4 and e5 place the load by core count so their spare figure is exact
    [ -n "${E4_LOAD_CORES:-}" ] && [ "$exp" = e4 ] && load_cores="$E4_LOAD_CORES"
    [ -n "${E5_LOAD_CORES:-}" ] && [ "$exp" = e5 ] && load_cores="$E5_LOAD_CORES"

    if have_row "$exp" "$arm" "$depth" "$load_cores" "$size" "$t" "$LOAD_SECS"; then
        say "skip $exp $arm depth=$depth cores=$load_cores size=$size trial=$t, already measured"
        return 0
    fi
    local tag="bg-$$-$RANDOM"
    local t0 submit priority alloc start finish s_start s_finish bg_total bg_done
    # 1 when the arm never completed. Dropping these rows silently removed the
    # one cell each sweep exists to measure: inline one core short of fitting,
    # which cannot run at all and so always timed out.
    local to=0
    submit=""; priority=""; alloc=""; start=""; finish=""; s_start=""; s_finish=""

    # long enough that it cannot expire during the trial. Only preemption, or
    # the explicit cancel below, frees these cores.
    start_load "$load_cores" "$LOAD_SECS"
    start_stream "$tag"
    bg_total=$(stream_total)
    t0=$(date +%s.%N)

    case "$arm" in
    baseline)
        # the baseline: takes its nodes, then opens the session and
        # waits in the vendor queue while holding them
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

    coscheduled|nowarmup)
        local extra="" err scout main
        [ "$arm" = nowarmup ] && extra="--quantum-mock-queue-depth 0"
        err=$(mktemp)
        # shellcheck disable=SC2086
        scout=$(flux submit --quantum-vendor mock \
            --quantum-mock-queue-depth "$depth" \
            --quantum-mock-service-time "$SERVICE" \
            --quantum-mock-base-overhead "$OVERHEAD" $extra \
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
    bg_done=$(stream_drained "$tag")
    row "$exp,$arm,$depth,$load,$size,$t,$t0,$submit,$priority,$alloc,$start,$finish,$s_start,$s_finish,$bg_total,$bg_done,$WORK,$load_cores,$LOAD_SECS,$to"
    drain
}

run_e1() {   # what the allocation is held for, against vendor queue depth
    for depth in ${DEPTHS:-0 7 14 100 491}; do
        for t in $(seq 1 "$TRIALS"); do
            for arm in $(shuffled baseline coscheduled); do
                trial e1 "$arm" "$depth" 0 8 "$t"
            done
        done
    done
}

run_e2() {   # the arms under classical contention, depth fixed
    # a job needs SIZE cores, so the load has to leave fewer than that free
    # before it waits at all. On 128 cores with size 8 that means above 94%.
    for load in ${LOADS:-0 50 90 94 96 98 99}; do
        for t in $(seq 1 "$TRIALS"); do
            for arm in $(shuffled baseline coscheduled); do
                trial e2 "$arm" "${DEPTH:-14}" "$load" 8 "$t"
            done
        done
    done
}

run_e3() {   # node seconds wasted against allocation size
    for size in ${SIZES:-1 8 32 64 96}; do
        for t in $(seq 1 "$TRIALS"); do
            for arm in $(shuffled baseline coscheduled); do
                trial e3 "$arm" "${DEPTH:-100}" 0 "$size" "$t"
            done
        done
    done
}

record_config () {
    [ -n "$OUT" ] || return 0
    {
        echo "cores       $CORES"
        echo "queue_policy $(flux module stats sched-fluxion-qmanager 2>/dev/null \
            | flux python -c "
import json,sys
print(sorted({v.get('policy','?') for v in json.load(sys.stdin).get('queues',{}).values()})[0])
" 2>/dev/null || echo unknown)"
        echo "jobtap      $(flux jobtap list 2>/dev/null | tr '\n' ' ')"
        echo "preempt     $(flux dmesg 2>/dev/null | grep -c 'preemption is on')"
        echo "load_secs   $LOAD_SECS"
        echo "work        $WORK"
        echo "overhead    $OVERHEAD"
        echo "service     $SERVICE"
        echo "timeout     $TIMEOUT"
        echo "started     $(date -Is)"
    } > "${OUT%.csv}.meta"
    say "config recorded in ${OUT%.csv}.meta"
}

say "cluster has $CORES cores, competing stream is $BG_JOBS jobs of ${BG_SECS}s = $(( BG_JOBS * BG_SECS )) core seconds"
if [ "$BG_JOBS" -le "$CORES" ]; then
    say "WARNING the stream is not oversubscribed, so drain time will just be BG_SECS. Raise BG_JOBS above $CORES"
fi
# Load is set as a percentage, but the mechanism is about spare cores, and
# integer division means a given percentage lands on different spare core
# counts for different sizes. So work backwards, from the spare cores we want
# to the percentage that produces exactly that.
run_e4() {
    # Does the knee track allocation size, or is 94 percent just a number about
    # this cluster. Sweeping spare cores rather than percentages means every
    # size is sampled at the same points, so the curves can be compared.
    for size in ${SIZES:-8 16 32 64}; do
        for spare in ${SPARES:--1 0 1 2 3}; do
            # cores the load must occupy to leave exactly this many spare
            local want=$(( CORES - size - spare ))
            [ "$want" -lt 1 ] && continue
            # recorded as a percentage for the CSV, but the load itself is
            # placed by core count so the spare figure is exact
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

record_config
run_e5() {
    # What does contention actually cost.
    #
    # A pair admitted onto a busy machine waits for the ordinary work to
    # finish, and holds a paid vendor session the whole time. So the cost is
    # not a property of the design, it is however long somebody else's job had
    # left to run. Sweep that.
    #
    # With preemption the cores are taken back after preempt_after, so the cost
    # stops depending on other people's jobs. Run this twice, once with
    # preempt_after set on the plugin and once without, and compare.
    local load want
    # occupy enough that the pair cannot fit. The pair needs SIZE5 + 1, the
    # extra one being the scout, so leaving SIZE5 - 1 spare strands it.
    want=$(( CORES - SIZE5 + 1 ))
    # Place the load by core count, not by percentage. Going to a percentage and
    # back loses cores to integer division twice, which is the same class of
    # error that cost e4 80 trials. On 128 cores with size 8 the round trip
    # turns 121 into 120.
    export E5_LOAD_CORES="$want"
    load=$(( want * 100 / CORES ))
    say "e5 occupies $want of $CORES cores, leaving $(( CORES - want )) spare, pair needs $(( SIZE5 + 1 ))"
    for LOAD_SECS in ${LOAD_LIVES:-15 30 60 120}; do
        export LOAD_SECS
        say "ordinary work lives ${LOAD_SECS}s"
        for t in $(seq 1 "$TRIALS"); do
            for arm in $(shuffled baseline coscheduled); do
                trial e5 "$arm" "${DEPTH:-14}" "$load" "$SIZE5" "$t"
            done
        done
    done
}

header
case "$WHAT" in
    e1) run_e1 ;;
    e2) run_e2 ;;
    e3) run_e3 ;;
    e4) run_e4 ;;
    e5) run_e5 ;;
    all) run_e1; run_e2; run_e3; run_e4; run_e5 ;;
    *) echo "usage: $0 {e1|e2|e3|e4|e5|all}" >&2; exit 2 ;;
esac
