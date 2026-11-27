#!/bin/bash
# The whole busy-ness campaign: sample plans, run every level, analyze.
#
#   bash run.sh                # ~2 h on an idle coschedule-policy instance
#   TRIALS=5 bash run.sh       # quick pass
#
# Needs: a flux instance with the coschedule queue policy and the
# flux-quantum CLI plugin (same requirements as mock/run.sh), and
# ../queues/results/distribution.json. Everything is free: the vendor is
# the mock, the depths are draws from 42 days of real queue observations.
set -eu
cd "$(dirname "$0")"

DEVICE="${DEVICE:-qpu/ionq/Forte-1}"          # which measured distribution anchors the sweep
LEVELS="${LEVELS:-idle:0,today:1,x2:2,x5:5,x10:10}"
TRIALS="${TRIALS:-20}"
SEED="${SEED:-20261127}"
DIST="${DIST:-../queues/results/distribution.json}"

mkdir -p results
# plans are drawn before anything runs, from data plus a seed, and kept
python3 scripts/sample.py --distribution "$DIST" --device "$DEVICE" \
    --levels "$LEVELS" --trials "$TRIALS" --seed "$SEED" --out plans

flux resource list >/dev/null || { echo "no flux instance"; exit 1; }
flux submit --help 2>&1 | grep -q -- --quantum-vendor \
    || { echo "flux-quantum CLI plugin is not loaded"; exit 1; }

for plan in plans/*.csv; do
    level=$(basename "$plan" .csv)
    PLAN="$plan" LEVEL="$level" OUT="results/$level.csv" \
        bash scripts/experiment.sh
done

python3 scripts/analyze.py results/*.csv | tee results/summary.txt
