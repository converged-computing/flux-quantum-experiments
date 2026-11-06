#!/bin/bash
# Sample every service's queue for days, unattended.
#
#     UNTIL=2026-11-13T23:59:59Z bash run.sh
#     DAYS=4 INTERVAL=60 bash run.sh
#
# Everything is printed and also appended to results/run.log, so run it in tmux
# or screen to watch it, or with nohup ... > /dev/null 2>&1 & to not.
#
# Nothing is submitted, the queues are only read. Writes one file per UTC day
# into results/queues/, 2026-10-25.jsonl and so on, appending and forcing each
# tick to disk, so a crash or a restart carries on in the same day's file and
# loses at most the minute in flight. The analysis reads the whole directory.
# DATA=results/june puts a run's days somewhere else.
#
# UNTIL is a fixed end time. Starting this again after a reboot samples until the
# same moment, it does not add DAYS again. Without UNTIL the end is DAYS from
# now. If the sampler dies before the end it is started again a minute later.
# To stop early, touch results/STOP, or kill this script, and the analysis runs
# either way. The tables are in results/analysis.txt, the figures in plots/.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
cd "$HERE" || exit 1
mkdir -p results plots "${DATA:-results/queues}"
# print and keep a copy, appending, so a restart does not lose the old log
exec > >(tee -a results/run.log) 2>&1
PY=${PY:-python3}
DAYS=${DAYS:-4}
INTERVAL=${INTERVAL:-60}
VENDORS=${VENDORS:-braket,ionq,ibm}
TZ_HOURS=${TZ_HOURS:-0}
DATA=${DATA:-results/queues}
# a run started before the samples went one file a day wrote them all to
# results/queues.jsonl, and the analysis reads that too
INPUTS=("$DATA")
[ -f results/queues.jsonl ] && INPUTS=(results/queues.jsonl "$DATA")

say() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

if [ -n "${UNTIL:-}" ]; then
    END=$(date -u -d "$UNTIL" +%s) || { say "cannot read UNTIL=$UNTIL"; exit 1; }
else
    END=$(( $(date +%s) + $(python3 -c "print(int($DAYS * 86400))") ))
fi
say "sampling $VENDORS every ${INTERVAL}s until $(date -u -d "@$END" +%Y-%m-%dT%H:%M:%SZ)"

# kill this script and the sampler stops cleanly, then the analysis runs
STOPPING=0
CHILD=
trap 'STOPPING=1; [ -n "$CHILD" ] && kill -TERM "$CHILD" 2>/dev/null' TERM INT
rm -f results/STOP

while [ "$STOPPING" = 0 ] && [ ! -e results/STOP ] && [ "$(date +%s)" -lt "$END" ]; do
    # the network may not be up yet after a reboot, so a failed preflight
    # waits and tries again instead of giving up on the run
    if ! bash preflight.sh >> results/preflight.log 2>&1; then
        say "preflight failed, see results/preflight.log, trying again in 5 minutes"
        sleep 300 & CHILD=$!; wait "$CHILD"; CHILD=
        continue
    fi
    LEFT=$(( END - $(date +%s) ))
    [ "$LEFT" -gt 0 ] || break
    timeout "$LEFT" $PY scripts/sample.py --vendors "$VENDORS" --interval "$INTERVAL" \
        --out "$DATA" --stop-file results/STOP &
    CHILD=$!
    wait "$CHILD"; rc=$?
    CHILD=
    say "sampler exited with $rc"
    if [ "$STOPPING" = 0 ] && [ ! -e results/STOP ] && [ "$(date +%s)" -lt "$END" ]; then
        say "before the end, starting again in a minute"
        sleep 60 & CHILD=$!; wait "$CHILD"; CHILD=
    fi
done

TZ_HOURS="$TZ_HOURS" INTERVAL="$INTERVAL" bash analyze.sh "${INPUTS[@]}"
