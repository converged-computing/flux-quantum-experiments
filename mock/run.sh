#!/bin/bash
# Run the whole campaign unattended.
#
#     nohup bash overnight.sh > results/overnight.log 2>&1 &
#     tail -f results/overnight.log
#
# Two things this handles that ./run.sh does not.
#
# e6 needs a small core budget or admission never binds and every pair is
# admitted, which is exactly the useless result that made the earlier e6 data
# worthless. So the plugin is reloaded around it and the real budget put back
# afterwards, whether e6 passes or fails.
#
# And it refuses to start if the setup is wrong. A night of runs against the
# wrong queue policy or an unloaded plugin is worse than no runs, because the
# numbers look fine.

set -u

HERE=$(cd "$(dirname "$0")" && pwd)
cd "$HERE" || exit 1
mkdir -p results

CORES="${CORES:-$(flux resource list -s all -no '{ncores}' 2>/dev/null || echo 128)}"
PLUGIN="${PLUGIN:-/etc/flux/system/jobtap/quantum.so}"
VENDORS="${VENDORS:-ibm,braket,mock}"
PREEMPT="${PREEMPT:-5}"
E6_BUDGET="${E6_BUDGET:-20}"        # small enough that admission binds
E6_SIZE="${E6_SIZE:-8}"             # pairs then need 9, so 2 fit in 20
export CORES FLUX_QUANTUM_MOCK=1

say() { echo "[$(date -u +%H:%M:%S)] $*"; }
die() { echo "ABORT: $*" >&2; exit 1; }

load_plugin() {
    # $1 total_cores. Removing first, because loading over a loaded plugin
    # fails and the failure is easy to miss.
    flux jobtap remove quantum.so >/dev/null 2>&1
    flux jobtap load "$PLUGIN" \
        vendors="$VENDORS" protect_types="qpu" \
        total_cores="$1" reserve_cores=0 preempt_after="$PREEMPT" \
        || die "could not load $PLUGIN with total_cores=$1"
}

# ---- preflight, so a bad setup fails now and not in the morning ----
say "preflight"
[ -f "$PLUGIN" ] || die "$PLUGIN is missing, build and install it first"

pol=$(flux module stats sched-fluxion-qmanager 2>/dev/null | flux python -c "
import json, sys
qs = json.load(sys.stdin).get('queues', {})
print(','.join(sorted({v.get('policy', '?') for v in qs.values()})))
" 2>/dev/null) || die "sched-fluxion-qmanager is not loaded"
case "$pol" in
    *coschedule*) ;;
    *) die "queue policy is '$pol'. Hold is only honoured by coschedule, so a
       held classical job would start immediately and every arm would measure
       the same thing. Fix queue-policy in the qmanager config." ;;
esac

# shellcheck disable=SC2046
flux python -m flux_quantum.populate $(echo "$VENDORS" | tr ',' ' ') \
    || die "populate failed, so the first quantum submit would be refused"

load_plugin "$CORES"
say "cores=$CORES policy=$pol preempt_after=$PREEMPT"

# a real pair, end to end, before committing to hours of runs
err=$(mktemp)
flux submit --quantum-vendor mock --quantum-mock-base-overhead 2 \
    -n2 -- sleep 2 >/dev/null 2>"$err"
main=$(grep -oE 'held classical job [0-9]+' "$err" | awk '{print $NF}')
rm -f "$err"
[ -n "$main" ] || die "a mock pair could not even be submitted"
flux job wait-event -t 90 "$main" clean >/dev/null 2>&1 \
    || die "a mock pair did not complete, so nothing below would be trustworthy"
say "a mock pair ran end to end"
echo ""

# ---- the campaign ----
run_one() {
    local e="$1"; shift
    say "=== $e ==="
    if env "$@" OUT="results/$e.csv" scripts/experiment.sh "$e" \
            > /dev/null 2> "results/$e.log"; then
        say "    $e done, $(( $(wc -l < "results/$e.csv") - 1 )) rows"
    else
        # keep going. One failed experiment should not cost the whole night.
        say "    $e FAILED, see results/$e.log"
    fi
    grep -c TIMEOUT "results/$e.log" 2>/dev/null | grep -qv '^0$' \
        && say "    note: $e logged timeouts, check the log"
    return 0
}

# e4 is the long one, four hundred trials each waiting out a background load.
# Its variance was 0.002s, so fewer trials costs almost nothing if time is short.
run_one e1
run_one e2
run_one e3
run_one e5 LOAD_LIVES="15 30 60 120"
run_one e4 "TRIALS=${E4_TRIALS:-10}"

# ---- e6, which needs the small budget ----
say "=== e6, budget $E6_BUDGET so admission binds ==="
load_plugin "$E6_BUDGET"
run_one e6 "TRIALS=${E6_TRIALS:-5}" "PAIRS=${E6_PAIRS:-1 2 3 4 5 6}" \
           "SIZE6=$E6_SIZE"
load_plugin "$CORES"
say "real budget restored: total_cores=$CORES"
echo ""

# ---- what it all says ----
say "=== analysis ==="
python3 scripts/analyze.py results/*.csv | tee results/summary.txt
echo ""
say "=== figures ==="
python3 scripts/plot.py results/*.csv
echo ""
say "done. results/summary.txt and plots/ hold the answers"
