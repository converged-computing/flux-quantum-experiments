#!/bin/bash
# Run the whole campaign unattended.
#
#     nohup bash run.sh > results/run.log 2>&1 &
#
# Refuses to start if the setup is wrong, since a night against the wrong
# policy or an unloaded plugin produces numbers that look fine. e6 needs a
# core budget small enough that admission binds, so the plugin is reloaded
# around it and the real budget put back afterwards.

set -u

HERE=$(cd "$(dirname "$0")" && pwd)
cd "$HERE" || exit 1
mkdir -p results

CORES="${CORES:-$(flux resource list -s all -no '{ncores}' 2>/dev/null || echo 128)}"
PLUGIN="${PLUGIN:-/etc/flux/system/jobtap/quantum.so}"
VENDORS="${VENDORS:-ibm,braket,mock}"
PREEMPT="${PREEMPT:-5}"
E6_BUDGET="${E6_BUDGET:-20}"        # pairs of E6_SIZE need E6_SIZE+1, so 2 fit
E6_SIZE="${E6_SIZE:-8}"
export CORES FLUX_QUANTUM_MOCK=1

say() { echo "[$(date -u +%H:%M:%S)] $*"; }
die() { echo "ABORT: $*" >&2; exit 1; }

load_plugin() {
    # loading over a loaded plugin fails, so remove first
    flux jobtap remove quantum.so >/dev/null 2>&1
    flux jobtap load "$PLUGIN" \
        vendors="$VENDORS" protect_types="qpu" \
        total_cores="$1" reserve_cores=0 preempt_after="$PREEMPT" \
        || die "could not load $PLUGIN with total_cores=$1"
}

say "preflight"
[ -f "$PLUGIN" ] || die "$PLUGIN is missing, build and install it first"

pol=$(flux module stats sched-fluxion-qmanager 2>/dev/null | flux python -c "
import json, sys
qs = json.load(sys.stdin).get('queues', {})
print(','.join(sorted({v.get('policy', '?') for v in qs.values()})))
" 2>/dev/null) || die "sched-fluxion-qmanager is not loaded"
case "$pol" in
    *coschedule*) ;;
    *) die "queue policy is '$pol'. Hold is only honoured by coschedule." ;;
esac

# shellcheck disable=SC2046
flux python -m flux_quantum.populate $(echo "$VENDORS" | tr ',' ' ') \
    || die "populate failed, so the first quantum submit would be refused"

load_plugin "$CORES"
say "cores=$CORES policy=$pol preempt_after=$PREEMPT"

# one real pair before committing to hours of runs
err=$(mktemp)
FLUX_QUANTUM_MOCK_BASE_OVERHEAD=2 flux submit --quantum-vendor mock \
    -n2 -- sleep 2 >/dev/null 2>"$err"
main=$(grep -oE 'held classical job [0-9]+' "$err" | awk '{print $NF}')
rm -f "$err"
[ -n "$main" ] || die "a mock pair could not be submitted"
flux job wait-event -t 90 "$main" clean >/dev/null 2>&1 \
    || die "a mock pair did not complete"
say "a mock pair ran end to end"
echo ""

run_one() {
    local e="$1"; shift
    say "=== $e ==="
    if env "$@" OUT="results/$e.csv" scripts/experiment.sh "$e" \
            > /dev/null 2> "results/$e.log"; then
        say "    $e done, $(( $(wc -l < "results/$e.csv") - 1 )) rows"
    else
        say "    $e FAILED, see results/$e.log"
    fi
    grep -c TIMEOUT "results/$e.log" 2>/dev/null | grep -qv '^0$' \
        && say "    note: $e logged timeouts"
    return 0
}

run_one e1
run_one e2
run_one e3
run_one e5 LOAD_LIVES="15 30 60 120"
# e4 is the long one, four hundred trials each waiting out a background load
run_one e4 "TRIALS=${E4_TRIALS:-10}"

say "=== e6, budget $E6_BUDGET so admission binds ==="
load_plugin "$E6_BUDGET"
run_one e6 "TRIALS=${E6_TRIALS:-5}" "PAIRS=${E6_PAIRS:-1 2 3 4 5 6}" \
           "SIZE6=$E6_SIZE"
load_plugin "$CORES"
say "real budget restored: total_cores=$CORES"
echo ""

say "=== analysis ==="
python3 scripts/analyze.py results/*.csv | tee results/summary.txt
echo ""
say "=== figures ==="
python3 scripts/plot.py results/*.csv
echo ""
say "done. results/summary.txt and plots/ hold the answers"
