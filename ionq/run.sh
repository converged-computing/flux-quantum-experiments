#!/bin/bash
# Run the IonQ campaign unattended, on the simulator by default.
#
#     export IONQ_API_KEY=...
#     nohup bash run.sh > results/run.log 2>&1 &
#
# Without a key, against the fake service, which it starts and stops itself:
#
#     FAKE=1 nohup bash run.sh > results/run.log 2>&1 &
#
# Refuses to start if preflight fails, and the fake it started goes with it.
# e1 e3 e2 e4, e5 against the fake, then the analysis.

set -u
HERE=$(cd "$(dirname "$0")" && pwd)
cd "$HERE" || exit 1
mkdir -p results

say() { echo "[$(date -u +%H:%M:%S)] $*"; }
die() { echo "ABORT: $*" >&2; exit 1; }

FAKE_PID=""
if [ "${FAKE:-0}" = 1 ]; then
    log=$(mktemp)
    # jobs run on any node of the instance, and the fake only answers where
    # it listens. Past one node it has to listen on every interface, and the
    # URL it prints is then the address the other nodes reach this one at
    bind=127.0.0.1
    [ "$(flux resource list -s all -no '{nnodes}' 2>/dev/null || echo 1)" -gt 1 ] && bind=0.0.0.0
    FAKE_IONQ_SECONDS="${FAKE_IONQ_SECONDS:-1}" flux python -m flux_quantum.backends.ionq.fake --port 0 --bind "$bind" >"$log" 2>&1 &
    FAKE_PID=$!
    for _ in $(seq 1 50); do grep -q "listening on" "$log" && break; sleep 0.1; done
    IONQ_API_URL=$(grep -oE 'http://[0-9.]+:[0-9]+' "$log")
    export IONQ_API_URL IONQ_API_KEY="${IONQ_API_KEY:-fake}"
    [ -n "$IONQ_API_URL" ] || die "the fake service did not start"
    say "fake service at $IONQ_API_URL"
    trap 'kill $FAKE_PID 2>/dev/null' EXIT
fi

say "preflight"
bash preflight.sh > results/preflight.log 2>&1 || { cat results/preflight.log; die "preflight failed"; }
grep -E "^  (ok|warn|FAIL)" results/preflight.log | sed 's/^/    /'

run_one() {
    local e="$1"; shift
    say "=== $e ==="
    if env "$@" OUT="results/$e.csv" scripts/experiment.sh "$e" > /dev/null 2> "results/$e.log"; then
        say "    $e done, $(( $(wc -l < "results/$e.csv") - 1 )) rows"
    else
        say "    $e FAILED, see results/$e.log"
    fi
    grep -c -E "TIMEOUT|CENSORED" "results/$e.log" 2>/dev/null | grep -qv '^0$' \
        && say "    note: $e logged timeouts"
    return 0
}

run_one e1
run_one e3
run_one e2
run_one e4
# e5 sweeps the fake's queue, so it only runs against the fake
[ "${FAKE:-0}" = 1 ] && run_one e5 ITERS="${ITERS5:-5}" TRIALS="${TRIALS5:-3}"
echo ""

say "=== analysis ==="
python3 scripts/analyze.py results/*.csv | tee results/summary.txt
say "done"
