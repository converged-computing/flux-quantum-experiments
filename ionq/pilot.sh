#!/bin/bash
# Small, bounded runs on the device, against the IonQ API directly. run.sh is
# the campaign as it ran against the fake and stays as it is; this is the
# separate path for anything that bills.
#
#     export IONQ_API_KEY=...
#     bash pilot.sh estimate    price every stage at the account's rates. Bills nothing
#     bash pilot.sh smoke       one plain job and one pair, one circuit each
#     bash pilot.sh e1          the three arms, TRIALS trials (2)
#     bash pilot.sh e3          the three arms at 1 and 5 circuits, TRIALS trials
#     bash pilot.sh e4          the four hold checks once, with short jobs
#     bash pilot.sh bill        what the service billed for every job recorded so far
#
# Every stage is priced from the service's estimate endpoint before anything
# is submitted, and refused when that is over BUDGET, 100 USD by default. A
# stage never resumes: an earlier run of it is kept aside under a timestamp.
# Every session opened carries a cost limit of BUDGET as well, so a session
# nobody is watching cannot bill past it. Each job is one task, so one copy
# of each circuit is sent. Results go to results-forte/, and bill reads back
# every circuit's cost from the service afterwards.
#
# Settings: TARGET (qpu.forte-1), BUDGET (100), SHOTS (100), TRIALS (2),
# TIMEOUT per job (14400 s, a device queue is minutes to hours), OUTDIR.

set -u
HERE=$(cd "$(dirname "$0")" && pwd)
cd "$HERE" || exit 1
STAGE="${1:-estimate}"
TARGET="${TARGET:-qpu.forte-1}"
BUDGET="${BUDGET:-100}"
SHOTS="${SHOTS:-100}"
TRIALS="${TRIALS:-2}"
OUTDIR="${OUTDIR:-results-forte}"
export TARGET SHOTS
export TASKS=1 DRY_RUN=0 TIMEOUT="${TIMEOUT:-14400}"
export FLUX_QUANTUM_IONQ_COST_LIMIT_USD="${FLUX_QUANTUM_IONQ_COST_LIMIT_USD:-$BUDGET}"

say() { echo "[$(date -u +%H:%M:%S)] $*"; }
die() { echo "ABORT: $*" >&2; exit 1; }

[ -n "${IONQ_API_KEY:-}" ] || die "IONQ_API_KEY is not set"
# the backend treats FLUX_QUANTUM_MOCK as a dry run and sends everything to
# the simulator, and jobs inherit this environment. Nothing here is a dry run
if [ -n "${FLUX_QUANTUM_MOCK:-}" ]; then
    echo "note: FLUX_QUANTUM_MOCK=${FLUX_QUANTUM_MOCK} was set, which makes every job a dry run on the simulator. Unsetting it for this run" >&2
    unset FLUX_QUANTUM_MOCK
fi
case "${IONQ_API_URL:-https://api.ionq.co/v0.4}" in
    https://api.ionq.co*) ;;
    *) [ "${FAKE_OK:-0}" = 1 ] || die "IONQ_API_URL is ${IONQ_API_URL}, not IonQ. FAKE_OK=1 to rehearse against the fake" ;;
esac

# what a stage submits: circuits and warm-ups. e4 is an upper bound, its
# jobs are interrupted
counts() {
    case "$1" in
        smoke) echo "2 1" ;;
        e1)    echo "$((3 * TRIALS)) $TRIALS" ;;
        e3)    echo "$((18 * TRIALS)) $((2 * TRIALS))" ;;
        e4)    echo "12 4" ;;
        *)     echo "0 0" ;;
    esac
}

# the service's price for a stage, from its estimate endpoint. Prints the
# total and the per job prices, exit 1 when over BUDGET
price() {
    set -- $(counts "$1")
    flux python - "$TARGET" "$SHOTS" "$1" "$2" "$BUDGET" <<'EOF'
import sys
from flux_quantum.backends.ionq import IonQBackend
target, shots, circuits, warmups, budget = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), float(sys.argv[5])
c = IonQBackend().client
def est(qubits, q1, q2):
    got = c.get("/jobs/estimate?backend=%s&qubits=%d&shots=%d&1q_gates=%d&2q_gates=%d&error_mitigation=false"
                % (target, qubits, shots, q1, q2))
    rate = got.get("rate_information") or {}
    return float(got.get("estimated_total_cost") or 0), got.get("cost_unit", "usd"), rate
circuit, unit, rate = est(2, 1, 1)
warm, _, _ = est(1, 1, 0)
total = circuits * circuit + warmups * warm
print("  %d circuits at %.2f and %d warm-ups at %.2f: %.2f %s" % (circuits, circuit, warmups, warm, total, unit))
if rate.get("job_cost_minimum") is not None:
    print("  the account's minimum per job is %s" % rate["job_cost_minimum"])
if rate.get("fake"):
    print("  (made-up rates: this is the fake service)")
if total > budget:
    print("  over the budget of %.2f, refusing" % budget)
    sys.exit(1)
EOF
}

run_stage() {
    local stage="$1"; shift
    mkdir -p "$OUTDIR"
    say "$stage on $TARGET, budget $BUDGET"
    price "$stage" || die "$stage is over budget. BUDGET=... to raise it, knowingly"
    [ -z "$(flux jobs -no '{id}')" ] || die "the instance is not idle, flux cancel --all first"
    # never resume. A stage that picks up rows from an earlier run has tested
    # nothing, so what is there is kept aside under a timestamp and bill reads
    # only this run
    if [ -e "$OUTDIR/$stage.csv" ]; then
        stamp=$(date -u +%Y%m%dT%H%M%S)
        for f in "$OUTDIR/$stage.csv" "$OUTDIR/$stage.meta" "$OUTDIR/$stage.log" "$OUTDIR/$stage-jobs"; do
            [ -e "$f" ] && mv "$f" "$f.$stamp"
        done
        say "an earlier $stage is kept as $stage.*.$stamp"
    fi
    say "running"
    if env "$@" OUT="$OUTDIR/$stage.csv" scripts/experiment.sh "${stage/smoke/e1}" > /dev/null 2> "$OUTDIR/$stage.log"; then
        say "$stage done, $(( $(wc -l < "$OUTDIR/$stage.csv") - 1 )) rows"
    else
        say "$stage FAILED, see $OUTDIR/$stage.log"
    fi
    grep -E "TIMEOUT|CENSORED" "$OUTDIR/$stage.log" | sed 's/^/    /'
    say "what the service billed:"
    flux python scripts/bill.py "$OUTDIR" --recent 20 | sed 's/^/    /'
}

case "$STAGE" in
    estimate)
        say "prices at the account's rates, nothing submitted"
        for s in smoke e1 e3 e4; do echo "$s:"; price "$s" || true; done ;;
    smoke)
        # the smallest run that exercises the whole path: a plain job and a
        # pair, one circuit each. bill then shows the pair's circuit ran on
        # the target inside a session, and what each job cost
        run_stage smoke ARMS="plain coscheduled" TRIALS=1 ITERS=1 ;;
    e1)
        run_stage e1 TRIALS="$TRIALS" ITERS=1 ;;
    e3)
        run_stage e3 TRIALS="$TRIALS" ITERS_SWEEP="1 5" ;;
    e4)
        run_stage e4 TRIALS4=1 ITERS4=3 THINK4=5 ;;
    bill)
        flux python scripts/bill.py "$OUTDIR" --recent "${RECENT:-40}" ;;
    *)
        echo "usage: $0 {estimate|smoke|e1|e3|e4|bill}" >&2; exit 2 ;;
esac
