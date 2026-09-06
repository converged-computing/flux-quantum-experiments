#!/bin/bash
# Settles two things before the knee figures go in a paper. About four minutes,
# mock only, no vendor money.
#
#   bash diagnose-knee.sh 2>&1 | tee diagnose-knee.log
#
# QUESTION 1. Is the 2.2s release latency a scheduler property or a parameter
# artifact?
#
#   preempt_after is 5.0s. e4's vendor wait is about 2.75s. 5.0 - 2.75 = 2.25,
#   and every size measured 2.200. That suggests the preemption timer runs
#   concurrently with the vendor wait rather than starting when the classical is
#   released. If so the number is a function of DEPTH and preempt_after, not of
#   the scheduler, and it changes if either moves.
#
#   Prediction, if the artifact theory is right:
#     depth   vendor wait   release latency at slack -1
#         0        ~2.05s     ~2.95s
#        14        ~2.75s     ~2.25s
#       100        ~7.06s     ~0s, preemption already fired during the wait
#
#   If instead release latency stays near 2.2 at every depth, the theory is
#   wrong and the number means something.
#
# QUESTION 2. What does it cost to take cores from preemption?
#
#   Preemptible classical work is part of a pair's budget by policy, so a pair
#   is never short of room. It just has to take some of its cores by preempting
#   someone. preempt_cores is that count, and it is >= 0 by construction.
#
#   The first run of this test found the cost non-monotonic: taking 1 core cost
#   7.87s and taking 2 cost 5.06s. The load was one 121-core job, which cannot
#   be partly preempted, so the victim selector had no choice to make. This run
#   chunks the load into LOAD_CHUNK-core jobs so it does.

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

# one coscheduled pair against a load of $2 cores, at queue depth $1
probe() {
    local depth="$1" lc="$2" load="" err scout main prio alloc start left take id
    # chunked, so preemption has individual victims to choose between
    left="$lc"
    while [ "$left" -gt 0 ]; do
        take="$LOAD_CHUNK"; [ "$take" -gt "$left" ] && take="$left"
        id=$(flux submit -n"$take" sleep 200 2>/dev/null)
        [ -n "$id" ] && load="${load:+$load }$id"
        left=$(( left - take ))
    done
    sleep 3
    err=$(mktemp)
    scout=$(flux submit --quantum-vendor mock \
        --quantum-mock-queue-depth "$depth" \
        --quantum-mock-service-time 0.05 \
        --quantum-mock-base-overhead 2 \
        -n"$SIZE" -- sh -c "sleep 1" 2>"$err")
    main=$(grep -oE 'held classical job [0-9]+' "$err" | awk '{print $NF}')
    rm -f "$err"
    if [ -z "$main" ]; then
        echo "  depth=$depth load=$lc  NO PAIR CREATED"
        cancel_jobs $load "$scout"; return
    fi
    if flux job wait-event -t 90 "$main" clean >/dev/null 2>&1; then
        prio=$(memo_ts "$main"); alloc=$(ev "$main" alloc)
        start=$(ev "$main" start)
        local submit; submit=$(ev "$main" submit)
        python3 - "$submit" "$prio" "$alloc" "$start" "$depth" "$lc" "$SIZE" "$CORES" <<'PY'
import sys
sub, prio, alloc, start, depth, lc, size, cores = sys.argv[1:]
f = lambda x: float(x) if x else None
sub, prio, alloc, start = map(f, (sub, prio, alloc, start))
wait = (prio - sub) if (prio and sub) else None
rel  = (alloc - prio) if (alloc and prio) else None
spare = int(cores) - int(lc)
need = int(size) + 1
pc = max(0, need - spare)
print("  depth=%-4s load=%-4s from_preempt=%-2d vendor_wait=%-7s release_lat=%-7s  total=%-7s  5.0-wait=%s"
      % (depth, lc, pc,
         "%.3f" % wait if wait is not None else "?",
         "%.3f" % rel if rel is not None else "?",
         "%.3f" % (wait + rel) if (wait is not None and rel is not None) else "?",
         "%.3f" % (5.0 - wait) if wait is not None else "?"))
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
echo "=== Q1. release latency against vendor queue depth, at slack -1 ==="
echo "    if release_lat tracks (5.0 - vendor_wait), the 2.2s is an artifact"
for d in 0 14 100; do
    probe "$d" $(( CORES - SIZE + 1 ))
done

echo
echo "=== Q2. cost against how many cores come from preemption ==="
echo "    pair needs $(( SIZE + 1 )) cores. Load is chunked into ${LOAD_CHUNK}-core jobs,"
echo "    so the victim selector has a choice, which it did not have last time."
for pc in 0 1 2 3 4; do
    probe 14 $(( CORES - SIZE - 1 + pc ))
done

echo
echo "Read Q1 as: does release_lat follow the last column."
echo "Read Q2 as: total should rise with from_preempt, or at least not fall."
echo "Last run it fell, 1 core costing 7.87 and 2 costing 5.06, with an"
echo "unchunked load. If it is monotonic now, that was the granularity of the"
echo "victim. If it is still not, the victim selector needs looking at."
flux cancel --all >/dev/null 2>&1
