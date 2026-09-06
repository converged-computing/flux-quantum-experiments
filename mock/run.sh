#!/bin/bash
# Run the experiments and write everything into results/.
#
#     ./run.sh          all four
#     ./run.sh e2 e4    just those
#
# CORES has to be the real core count. analyze and plot read it too, and e4
# predicts its knee to the individual core, so a wrong value misplaces every
# predicted point.

set -u
HERE=$(cd "$(dirname "$0")" && pwd)
# -s all, not the bare -no '{ncores}'. Without a state field in the format,
# flux resource list merges free, allocated and down onto one line, so the bare
# form returns the total while reading like the free count, and it counts down
# cores as capacity.
export CORES="${CORES:-$(flux resource list -s all -no '{ncores}' 2>/dev/null || echo 128)}"
export FLUX_QUANTUM_MOCK=1

which=("$@")
[ ${#which[@]} -eq 0 ] && which=(e1 e2 e3 e4)

echo "cluster has $CORES cores"
echo "policy: $(flux module stats sched-fluxion-qmanager 2>/dev/null \
    | flux python -c "
import json,sys
print(sorted({v.get('policy','?') for v in json.load(sys.stdin).get('queues',{}).values()})[0])
" 2>/dev/null || echo unknown)"
echo ""

for e in "${which[@]}"; do
    out="$HERE/results/$e.csv"
    log="$HERE/results/$e.log"
    echo "=== $e -> results/$e.csv ==="
    OUT="$out" "$HERE/scripts/experiment.sh" "$e" > /dev/null 2> "$log"
    rows=$(( $(wc -l < "$out" 2>/dev/null || echo 1) - 1 ))
    echo "    $rows rows, log in results/$e.log"
done

echo ""
echo "=== analysis ==="
# shellcheck disable=SC2086
CORES="$CORES" python3 "$HERE/scripts/analyze.py" "$HERE"/results/*.csv \
    | tee "$HERE/results/summary.txt"

echo ""
echo "=== figures ==="
cd "$HERE" || exit 1
CORES="$CORES" python3 scripts/plot.py results/*.csv
