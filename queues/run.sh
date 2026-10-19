#!/bin/bash
# Sample every service's queue for days, unattended.
#
#     nohup bash run.sh > results/run.log 2>&1 &
#     DAYS=4 INTERVAL=60 nohup bash run.sh > results/run.log 2>&1 &
#
# Nothing is submitted, the queues are only read. Writes results/queues.jsonl, appending, so a restart carries on in the
# same file. Stops after DAYS, or when killed, and runs the analysis either
# way. The tables are in results/analysis.txt, the figures in plots/.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
cd "$HERE" || exit 1
mkdir -p results plots
PY=${PY:-python3}
DAYS=${DAYS:-4}
INTERVAL=${INTERVAL:-60}
VENDORS=${VENDORS:-braket,ionq,ibm}
TZ_HOURS=${TZ_HOURS:-0}

say() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

say "preflight"
bash preflight.sh > results/preflight.log 2>&1 || { say "preflight failed, see results/preflight.log"; exit 1; }
say "sampling $VENDORS every ${INTERVAL}s for $DAYS day(s)"
timeout "$(python3 -c "print(int($DAYS * 86400))")" \
    $PY scripts/sample.py --vendors "$VENDORS" --interval "$INTERVAL" --out results/queues.jsonl
say "sampler exited with $?"

say "analysis"
$PY scripts/analyze.py results/queues.jsonl --tz "$TZ_HOURS" --interval "$INTERVAL" \
    --distribution results/distribution.json | tee results/analysis.txt
$PY scripts/changes.py results/queues.jsonl --out results/changes.json
$PY scripts/figures.py results/queues.jsonl --out plots
$PY scripts/page.py results/queues.jsonl --out plots/index.html
$PY scripts/plot.py results/queues.jsonl --tz "$TZ_HOURS" --out plots || say "no matplotlib figures, is matplotlib installed?"
say "done"
