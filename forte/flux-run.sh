#!/bin/bash
# One cell of the experiment through flux, on the cluster, so the pipeline
# itself is what runs: the baseline is a plain job, the coschedule arm is a
# quantum pair, and the classical half is trial.py either way.
#
#   flux-run.sh braket baseline                 # no hold, 3 tasks
#   flux-run.sh braket coschedule 3             # the hold, behind 3 fillers
#   flux-run.sh ionq coschedule 0 probe
#
# The fillers go in from here, before the submit, since in the coschedule arm
# the hold has to find them already queued. They are drained after. The record
# is annotated with the job's own timings afterwards: when its cores were
# allocated and freed, and when the scout started and handed the session over.
#
#   TASKS, SHOTS, FILLER_SHOTS, MAX_SECONDS, WALLTIME, RECORD  override the defaults
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
path=${1:?path: braket or ionq}
arm=${2:?arm: baseline or coschedule}
fillers=${3:-0}
hold=${4:-}
TASKS=${TASKS:-3}; SHOTS=${SHOTS:-100}; FILLER_SHOTS=${FILLER_SHOTS:-100}
MAX_SECONDS=${MAX_SECONDS:-1800}; WALLTIME=${WALLTIME:-40m}
RECORD=${RECORD:-$HERE/runs.jsonl}
DEVICE_ARG=${DEVICE:+--device $DEVICE}

flux resource list >/dev/null 2>&1 || { echo "no flux instance"; exit 1; }
[ -n "$hold" ] || hold=$([ "$path" = braket ] && echo session || echo probe)
if [ "$arm" = coschedule ]; then
    python3 "$HERE/trial.py" estimate --path "$path" --arm "$arm" --hold "$hold" --tasks "$TASKS" --shots "$SHOTS" --fillers "$fillers" --filler-shots "$FILLER_SHOTS" $DEVICE_ARG
else
    python3 "$HERE/trial.py" estimate --path "$path" --arm "$arm" --tasks "$TASKS" --shots "$SHOTS" --fillers "$fillers" --filler-shots "$FILLER_SHOTS" $DEVICE_ARG
fi
read -r -p "continue? [y/N] " ans; case "$ans" in y|Y) ;; *) exit 0 ;; esac

FILLERS_FILE=$(mktemp)
if [ "$fillers" -gt 0 ]; then
    echo "== $fillers fillers into the public queue =="
    python3 "$HERE/trial.py" fill --path "$path" --fillers "$fillers" --filler-shots "$FILLER_SHOTS" --fillers-file "$FILLERS_FILE" $DEVICE_ARG || exit 1
    sleep 10
fi

classical="flux python $HERE/trial.py classical --path $path --arm $arm --tasks $TASKS --shots $SHOTS --max-seconds $MAX_SECONDS --record $RECORD --fillers-file $FILLERS_FILE --drain $DEVICE_ARG"
err=$(mktemp)
if [ "$arm" = coschedule ]; then
    echo "== submitting the pair, hold $hold =="
    scout=$(flux submit -t "$WALLTIME" --quantum-vendor "$path" --quantum-hold "$hold" --quantum-wait "$MAX_SECONDS" \
        ${DEVICE:+--quantum-device "$DEVICE"} -n1 -- $classical --hold "$hold" 2>"$err")
    sed 's/^/  /' "$err"
    main=$(grep -oE 'held classical job [0-9]+' "$err" | awk '{print $NF}')
    [ -n "$main" ] || { echo "no held classical job"; rm -f "$err"; exit 1; }
    echo "  scout=$scout classical=$main"
else
    echo "== submitting the classical, no hold =="
    main=$(flux submit -t "$WALLTIME" -n1 -- $classical 2>"$err") || { cat "$err"; exit 1; }
    scout=""
    echo "  classical=$main"
fi
rm -f "$err"

echo "== waiting =="
flux job wait-event -t "$((MAX_SECONDS + 900))" "$main" clean >/dev/null 2>&1 || echo "  the classical did not finish in time"
[ -n "$scout" ] && flux job wait-event -t 300 "$scout" clean >/dev/null 2>&1
echo "== classical output =="
flux job attach "$main" 2>&1 | sed 's/^/  /'
if [ -n "$scout" ]; then
    echo "== scout output =="
    flux job attach "$scout" 2>&1 | sed 's/^/  /'
fi
echo "== flux timings =="
python3 "$HERE/trial.py" annotate --job "$main" ${scout:+--scout "$scout"} --record "$RECORD" \
    || echo "  no record for job $main, the classical may have failed before writing one"
if [ "$fillers" -gt 0 ]; then
    echo "== draining fillers, in case the classical did not =="
    python3 "$HERE/trial.py" drain --path "$path" --fillers-file "$FILLERS_FILE" $DEVICE_ARG
fi
rm -f "$FILLERS_FILE"
