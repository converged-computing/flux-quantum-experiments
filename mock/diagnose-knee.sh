#!/bin/bash
# Two questions about the knee, settled in about four minutes on the mock.
#
#   bash diagnose-knee.sh 2>&1 | tee diagnose-knee.log
#
# Q1. Is the 2.2s release latency a scheduler property or an artifact?
#     preempt_after is 5s and e4's vendor wait about 2.75s. If the preemption
#     timer runs during the vendor wait, release latency at one core short is
#     5 - wait, and it moves with queue depth. If it stays near 2.2 at every
#     depth, it means something.
#
# Q2. What does taking cores from preemption cost, and is it monotonic?
#     The load is chunked into LOAD_CHUNK-core jobs so the victim selector has
#     a choice. scout_place near 0 means the scout got its core at once and
#     any delay is in the session path; near preempt_after means the scout
#     was held waiting for a core.

set -u
export FLUX_QUANTUM_MOCK=1
SIZE="${SIZE:-8}"
LOAD_CHUNK="${LOAD_CHUNK:-8}"
CORES=$(flux resource list -s all -no "{ncores}")
say() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }

cancel_jobs() {
    local id
    for id in "$@"; do
        [ -n "$id" ] || continue
        flux cancel "$id" >/dev/null 2>&1
        flux job wait-event -t 30 "$id" clean >/dev/null 2>&1
    done
}
ev() { flux job eventlog "$1" 2>/dev/null | awk -v e="$2" '$2==e {print $1; exit}'; }
memo_ts() {
    flux job eventlog "$1" 2>/dev/null | awk '$2=="memo" && /quantum_session/ {print $1; exit}'
}

# one coscheduled pair at queue depth $1 against a load of $2 cores
probe() {
    local depth="$1" lc="$2" load="" err scout main prio alloc start left take id
    left="$lc"
    while [ "$left" -gt 0 ]; do
        take="$LOAD_CHUNK"; [ "$take" -gt "$left" ] && take="$left"
        id=$(flux submit -n"$take" sleep 200 2>/dev/null)
        [ -n "$id" ] && load="${load:+$load }$id"
        left=$(( left - take ))
    done
    sleep 3
    err=$(mktemp)
    scout=$(FLUX_QUANTUM_MOCK_QUEUE="$depth" FLUX_QUANTUM_MOCK_SERVICE_TIME=0.05 \
        FLUX_QUANTUM_MOCK_BASE_OVERHEAD=2 \
        flux submit --quantum-vendor mock -n"$SIZE" -- sh -c "sleep 1" 2>"$err")
    main=$(grep -oE 'held classical job [0-9]+' "$err" | awk '{print $NF}')
    rm -f "$err"
    if [ -z "$main" ]; then
        echo "  depth=$depth load=$lc  NO PAIR CREATED"
        cancel_jobs $load "$scout"; return
    fi
    if flux job wait-event -t 90 "$main" clean >/dev/null 2>&1; then
        prio=$(memo_ts "$main"); alloc=$(ev "$main" alloc)
        start=$(ev "$main" start)
        local submit s_sub s_start
        submit=$(ev "$main" submit)
        s_sub=$(ev "$scout" submit); s_start=$(ev "$scout" start)
        python3 - "$submit" "$prio" "$alloc" "$start" "$depth" "$lc" "$SIZE" "$CORES" "$s_sub" "$s_start" <<'PY'
import sys
sub, prio, alloc, start, depth, lc, size, cores, s_sub, s_start = sys.argv[1:]
f = lambda x: float(x) if x else None
sub, prio, alloc, start, s_sub, s_start = map(f, (sub, prio, alloc, start, s_sub, s_start))
wait = (prio - sub) if (prio and sub) else None
rel = (alloc - prio) if (alloc and prio) else None
s_place = (s_start - s_sub) if (s_start and s_sub) else None   # scout submit to running
s_queue = (prio - s_start) if (prio and s_start) else None      # scout running to session
spare = int(cores) - int(lc)
pc = max(0, int(size) + 1 - spare)
fmt = lambda v: ("%.3f" % v) if v is not None else "?"
print("  depth=%-4s load=%-4s from_preempt=%-2d scout_place=%-7s scout_queue=%-7s release_lat=%-7s total=%-7s"
      % (depth, lc, pc, fmt(s_place), fmt(s_queue), fmt(rel),
         fmt(wait + rel) if (wait is not None and rel is not None) else "?"))
PY
    else
        echo "  depth=$depth load=$lc  TIMEOUT, pair never completed"
    fi
    cancel_jobs $load "$scout" "$main"
    sleep 2
}

say "cluster has $CORES cores, probing with size=$SIZE"
flux jobs -no "{id}" | grep -q . && { say "STOP, instance is not idle"; exit 1; }

echo
echo "=== Q1. release latency against queue depth, one core short ==="
for d in 0 14 100; do
    probe "$d" $(( CORES - SIZE + 1 ))
done

echo
echo "=== Q2. cost against cores taken from preemption, pair needs $(( SIZE + 1 )) ==="
for pc in 0 1 2 3 4; do
    probe 14 $(( CORES - SIZE - 1 + pc ))
done
flux cancel --all >/dev/null 2>&1
