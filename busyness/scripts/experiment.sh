#!/bin/bash
# One busy-ness level: paired arms over a sampled depth plan.
#
#   PLAN=plans/today.csv LEVEL=today OUT=results/today.csv scripts/experiment.sh
#
# Adapted from mock/scripts/experiment.sh (the proven trial mechanics), with
# two changes: the depth comes from the plan file - one seeded draw from a
# measured busy-ness distribution per trial, the SAME draw for both arms -
# and the classical-load axis is dropped (the contention under study is the
# vendor queue; mock/ already covers cluster load).
#
# Arms, as in mock/:
#   baseline     one job takes its cores, opens the session, and waits out
#                the vendor queue while holding them.
#   coscheduled  the classical is held, a one-core scout waits, the
#                classical is released once the QPU is ours.
#
# Raw timestamps only; analyze.py derives everything else.
set -u
export FLUX_QUANTUM_MOCK=1

PLAN="${PLAN:?set PLAN=plans/<level>.csv}"
LEVEL="${LEVEL:?set LEVEL=<level name>}"
OUT="${OUT:-}"
SERVICE="${SERVICE:-0.05}"   # seconds per queued task ahead of us
OVERHEAD="${OVERHEAD:-2}"    # fixed vendor wait at depth 0
WORK="${WORK:-5}"            # seconds of classical work
SIZE="${SIZE:-8}"            # classical allocation size, cores
TIMEOUT="${TIMEOUT:-900}"
FRESH="${FRESH:-0}"

say() { echo "[$(date +%H:%M:%S)] $*" >&2; }
shuffled() { printf '%s\n' "$@" | shuf; }
emit() {
    echo "$*"
    [ -n "$OUT" ] && echo "$*" >> "$OUT"
    return 0
}
ev()  { flux job eventlog "$1" 2>/dev/null | awk -v e="$2" '$2==e {print $1; exit}'; }
memo_ts() {
    flux job eventlog "$1" 2>/dev/null | awk '$2=="memo" && /quantum_session/ {print $1; exit}'
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
    say "TIMEOUT on $1 after ${TIMEOUT}s"; flux jobs -a >&2; return 1
}
drain() {
    for _ in $(seq 1 60); do
        flux jobs -no "{id}" 2>/dev/null | grep -q . || return 0
        sleep 2
    done
    say "WARNING instance did not drain"
}
qopts() {
    printf '{"queue_depth": %s, "service_time": %s, "base_overhead": %s}' \
        "$1" "$SERVICE" "$OVERHEAD"
}

HDR="exp,arm,level,depth,size,trial,t0,submit,priority,alloc,start,finish,scout_start,scout_finish,work_s,service,overhead,timedout"
have_row() {
    [ -n "$OUT" ] && [ -f "$OUT" ] || return 1
    awk -F, -v a="$1" -v l="$2" -v d="$3" -v t="$4" '
        $2 == a && $3 == l && $4 == d && $6 == t { found = 1; exit }
        END { exit !found }' "$OUT"
}
header() {
    if [ -n "$OUT" ] && [ "$FRESH" = 1 ]; then : > "$OUT"; fi
    if [ -n "$OUT" ] && [ -s "$OUT" ]; then
        [ "$(head -1 "$OUT")" = "$HDR" ] || {
            say "REFUSING to resume from $OUT, its header does not match"
            exit 1
        }
        say "resuming from $OUT with $(( $(wc -l < "$OUT") - 1 )) rows already done"
        echo "$HDR"
    else
        emit "$HDR"
    fi
}

trial() { # arm depth trial
    local arm="$1" depth="$2" t="$3" to=0
    local t0 submit priority alloc start finish s_start s_finish
    submit=""; priority=""; alloc=""; start=""; finish=""; s_start=""; s_finish=""
    if have_row "$arm" "$LEVEL" "$depth" "$t"; then
        say "skip $arm $LEVEL depth=$depth trial=$t, already measured"
        return 0
    fi
    t0=$(date +%s.%N)
    case "$arm" in
    baseline)
        local main
        main=$(QOPTS="$(qopts "$depth")" flux submit -n"$SIZE" \
            flux python -c "
import os, json, time
from flux_quantum.backends import get_backend
b = get_backend('mock')
o = json.loads(os.environ['QOPTS'])
s = b.open_session(o)
b.wait_for_priority(o)
time.sleep($WORK)
print('done', s)")
        say "baseline $LEVEL depth=$depth trial=$t main=$main"
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
            -n"$SIZE" -- sh -c "sleep $WORK; echo got \$QUANTUM_SESSION_ID" 2>"$err")
        main=$(grep -oE 'held classical job [0-9]+' "$err" | awk '{print $NF}')
        rm -f "$err"
        say "coscheduled $LEVEL depth=$depth trial=$t scout=$scout main=$main"
        [ -z "$main" ] && { say "no classical job"; return 1; }
        wait_clean "$main" || to=1
        [ "$to" = 0 ] && { wait_clean "$scout" || to=1; }
        submit=$(ev "$main" submit); priority=$(memo_ts "$main")
        alloc=$(ev "$main" alloc); start=$(ev "$main" start); finish=$(ev "$main" finish)
        s_start=$(ev "$scout" start); s_finish=$(ev "$scout" finish)
        [ "$to" = 1 ] && cancel_jobs "$main" "$scout"
        ;;
    esac
    [ "$to" = 1 ] && say "CENSORED $arm $LEVEL depth=$depth trial=$t"
    emit "busy,$arm,$LEVEL,$depth,$SIZE,$t,$t0,$submit,$priority,$alloc,$start,$finish,$s_start,$s_finish,$WORK,$SERVICE,$OVERHEAD,$to"
    drain
}

header
# skip the plan header; same sampled depth for BOTH arms of a trial
tail -n +2 "$PLAN" | while IFS=, read -r t depth _rest; do
    for arm in $(shuffled baseline coscheduled); do
        trial "$arm" "$depth" "$t" || exit 1
    done
done || exit 1
say "level $LEVEL done"
