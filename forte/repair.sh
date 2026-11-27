#!/bin/bash
# Triage and restart a wedged fluxion instance: "run_remove:
# planner_multi_rem_span returned -1", "cancel_vertex failed on cluster0",
# "run_sched_loop: Invalid argument" (PROTOCOL.md, Boot 3 and C4 attempt 1's
# wedge). The graph has to be rebuilt, which means restarting the broker.
#
#   bash repair.sh pre     # on the lead: stop the queue, drain it, prove idle
#   sudo systemctl restart flux.service   # on EVERY node, followers first
#   bash repair.sh post    # on the lead: verify the rebuilt graph, reopen
#
# Two phases because the restart happens per node and only a human does that.
# flux queue stop is checkpointed, so stopping it in pre keeps the jobs flux
# restores from the KVS (-Scontent.restore=auto) from allocating anything
# before post has verified the instance.
#
# Since the static scheduling graph ([resource] scheduling, generated at boot
# by flux_quantum.schedjgf) there is NO populate step here or anywhere: the
# broker reloads the full graph, qdevices included, on its own. The mutation
# that used to make this repair risky - growing a live graph - no longer
# exists. If this script runs at all, the wedge came from something new;
# collect flux dmesg before restarting.
set -u

say()  { echo "[repair] $*"; }
fail() { echo "[repair] FAIL: $*"; exit 1; }

# fluxion's own count, not flux jobs': a leaked planner span shows up here as
# cores allocated to nothing, which is exactly the damage being repaired
alloc() {
    flux python -c "
import flux
from flux_quantum.graph import allocated_cores
print(allocated_cores(flux.Flux()))" 2>/dev/null
}

active() { flux jobs --filter=pending,running,cleanup -no "{id}" 2>/dev/null | wc -l; }

planner_errors() {
    flux dmesg 2>/dev/null \
        | grep -E "planner_multi_rem_span|cancel_vertex failed|run_sched_loop"
}

# ------------------------------------------------------------------- pre
pre() {
    say "=== pre: close the queue and drain it ==="
    flux resource list >/dev/null 2>&1 \
        || fail "the broker does not answer. Is FLUX_URI set? \
export FLUX_URI=local:///home/ubuntu/run/flux/local"

    # keep the old damage on record: the restart clears the ring buffer
    flux dmesg >"$HOME/repair-dmesg-$(date +%Y%m%d-%H%M%S).txt" 2>&1
    say "dmesg saved to \$HOME (the restart clears the ring buffer)"

    # stop: no new allocations. disable: no new submissions. Both are
    # checkpointed, so they survive the restart and nothing can allocate
    # before post has verified the rebuilt instance.
    flux queue stop -a -m "repair: fluxion wedged" \
        || fail "could not stop the queue"
    flux queue disable -a -m "repair in progress" \
        || fail "could not disable the queue"
    say "queue stopped and disabled (checkpointed, survives the restart)"

    flux cancel --all -q 2>/dev/null
    say "cancelled every active job"

    # A job stuck in CLEANUP because fluxion cannot free it is the corruption
    # itself, so this drain is allowed to time out. The restart discards the
    # graph either way; what must hold is the queue being stopped above.
    if flux queue idle -t 120 >/dev/null 2>&1; then
        say "queue drained"
    else
        say "WARNING the queue did not drain in 120s ($(active) jobs still \
active). Expected if a free is already stuck; the restart clears them."
    fi

    say "allocated cores now: $(alloc)   active jobs: $(active)"
    echo
    say "NEXT, by hand, on EVERY node (followers first, the lead last):"
    say "    sudo systemctl restart flux.service"
    say "then on the lead:  bash $0 post"
}

# ------------------------------------------------------------------ post
post() {
    say "=== post: verify the rebuilt instance, then reopen the queue ==="

    # the broker answers before rc1 has loaded modules, and the sched modules
    # are rank 0 only
    local i
    for i in $(seq 1 60); do
        flux resource list >/dev/null 2>&1 && break
        sleep 2
    done
    flux resource list >/dev/null 2>&1 || fail "the broker never answered"
    for i in $(seq 1 60); do
        flux module list 2>/dev/null | grep -q sched-fluxion-qmanager && break
        sleep 2
    done
    flux module list 2>/dev/null | grep -q sched-fluxion-qmanager \
        || fail "fluxion never finished loading"
    say "broker and fluxion up"

    local nodes
    nodes=$(flux resource list -s up -no "{nnodes}" 2>/dev/null | head -1)
    say "nodes up: ${nodes:-unknown}"
    [ "${nodes:-0}" -ge 2 ] \
        || say "WARNING fewer than 2 nodes are up; restart flux.service on the follower"

    # the restart restored the checkpointed stop; say so out loud
    flux queue status 2>/dev/null | sed 's/^/    /'
    flux queue stop -a -m "repair: verifying" >/dev/null 2>&1
    flux cancel --all -q 2>/dev/null

    local a n
    a=$(alloc); n=$(active)
    say "allocated cores: ${a:-unreadable}   active jobs: $n"
    [ "${a:-1}" = "0" ] || fail "fluxion still reports ${a} cores allocated. \
Cancel what holds them (flux jobs -a) and rerun post; if nothing holds them \
the restart did not take."
    [ "$n" = "0" ] || fail "$n jobs are still active"

    # fresh ring buffer: anything here was logged by the instance we just
    # started, so it is not the old damage
    if planner_errors | grep -q .; then
        planner_errors | tail -5 | sed 's/^/    /'
        fail "the rebuilt instance is ALREADY logging planner errors"
    fi
    say "dmesg clean of planner errors"

    # the broker was configured with the static scheduling graph, so the
    # qdevices must already be back, reachable, with nothing added by hand
    flux python -c "
import flux
from flux_quantum import graph
g = graph.get_live_graph(flux.Flux())
orphans = graph.orphaned_vendors(g)
assert not orphans, 'orphaned qdevices: {}'.format(orphans)
vendors = graph.vendors_present(g)
assert vendors, 'no qdevices in the graph'
print('[repair] qdevices back from the static graph:', ' '.join(sorted(vendors)))
" || fail "the rebuilt graph is missing its qdevices. Check [resource] \
scheduling in /etc/flux/system/conf.d and /etc/flux/system/scheduling.json"

    flux queue start -a >/dev/null 2>&1 || fail "could not start the queue"
    flux queue enable -a >/dev/null 2>&1 || fail "could not enable the queue"
    say "queue started and enabled"

    if planner_errors | grep -q .; then
        planner_errors | tail -5 | sed 's/^/    /'
        fail "planner errors appeared during the repair"
    fi

    echo
    say "REPAIRED. Next, in order:"
    say "    bash /opt/flux-quantum/tests/integration/test-system-instance.sh  # rc=0"
    say "    bash /opt/flux-quantum-experiments/forte/fluxbatch.sh check   # OK"
}

case "${1:-}" in
    pre)  pre ;;
    post) post ;;
    *)    echo "usage: $0 pre|post   (see the header)"; exit 2 ;;
esac
