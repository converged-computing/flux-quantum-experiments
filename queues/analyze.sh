#!/bin/bash
# The tables, the distribution, the record changes, the figures and the
# page, from any set of sample files or directories of them.
#
#     bash analyze.sh results/queues.jsonl results/queues
#     TZ_HOURS=-7 bash analyze.sh results-days/queues.jsonl results/queues.jsonl results/queues
#
# run.sh calls this at the end of a run with that run's files. Call it by
# hand to put several runs on one page. OUT is where the tables go
# (results), PLOTS where the figures go (plots).
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
cd "$HERE" || exit 1
PY=${PY:-python3}
TZ_HOURS=${TZ_HOURS:-0}
INTERVAL=${INTERVAL:-60}
OUT=${OUT:-results}
PLOTS=${PLOTS:-plots}
[ $# -gt 0 ] || { echo "usage: bash analyze.sh FILE_OR_DIR..." >&2; exit 2; }
mkdir -p "$OUT" "$PLOTS"
say() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

say "analysis of $*"
$PY scripts/analyze.py "$@" --tz "$TZ_HOURS" --interval "$INTERVAL" \
    --distribution "$OUT/distribution.json" | tee "$OUT/analysis.txt"
$PY scripts/changes.py "$@" --out "$OUT/changes.json"
$PY scripts/figures.py "$@" --out "$PLOTS" --changes "$OUT/changes.json"
$PY scripts/page.py "$@" --out "$PLOTS/index.html" --changes "$OUT/changes.json"
$PY scripts/plot.py "$@" --tz "$TZ_HOURS" --out "$PLOTS" || say "no matplotlib figures, is matplotlib installed?"
say "done"
