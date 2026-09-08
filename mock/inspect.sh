#!/bin/bash
# Inspect the coscheduling setup structurally. Reads the fluxion graph, the
# jobspec the plugin builds, the R that comes back, and the plugin config, and
# checks they agree with each other. Behaviour tests tell you something broke,
# this tells you what is actually there.
#
#     bash inspect-coschedule.sh [vendor]

set -u
vendor="${1:-mock}"
rc=0
ok()   { echo "  ok    $*"; }
bad()  { echo "  FAIL  $*"; rc=1; }
info() { echo "        $*"; }

echo "=== 1. fluxion graph, vertex by vertex ==="
flux python -c "
import json, sys, flux
from flux_quantum import qresource
from flux_quantum.graph import get_live_graph

g = get_live_graph(flux.Flux())
nodes = g.get('nodes', [])
edges = g.get('edges', [])
by_type = {}
for n in nodes:
    md = n.get('metadata', {})
    by_type.setdefault(md.get('type'), []).append(md)
print('        %d vertices, %d edges' % (len(nodes), len(edges)))
for t in sorted(by_type):
    if t.startswith('qdevice_') or t == 'qpu' or t in ('cluster', 'node'):
        print('        %-18s x%d' % (t, len(by_type[t])))

# the qpu must hang off its qdevice, that containment path is what the
# jobspec walks
paths = [m.get('paths', {}).get('containment', '') for m in by_type.get('qpu', [])]
bad_paths = [p for p in paths if '/qdevice_' not in p]
print('QPU_PATHS ' + json.dumps(paths))
print('BAD_PATHS ' + json.dumps(bad_paths))
print('TYPES ' + json.dumps(sorted(t for t in by_type if t)))
" 2>/tmp/g.err > /tmp/g.out || { bad "could not read the graph"; cat /tmp/g.err; exit 1; }
grep -v '^[A-Z_][A-Z_]* ' /tmp/g.out
qpaths=$(grep '^QPU_PATHS ' /tmp/g.out | cut -d' ' -f2-)
bpaths=$(grep '^BAD_PATHS ' /tmp/g.out | cut -d' ' -f2-)
if [ "$qpaths" = "[]" ]; then
    bad "no qpu vertices in the graph right now"
    info "a broker restart empties the graph. If later steps pass, a submit"
    info "repopulated it, which is the path that corrupts fluxion on a busy"
    info "instance. Populate at startup: flux python -m flux_quantum.populate ibm braket mock"
elif [ "$bpaths" != "[]" ]; then
    bad "a qpu is not under a qdevice, so the jobspec containment walk will fail"
    info "$bpaths"
else
    ok "every qpu sits under a qdevice"
fi

echo ""
echo "=== 2. does the scout jobspec match what the graph offers ==="
flux python -c "
import json, flux
from flux_quantum import qresource
from flux_quantum.graph import get_live_graph

want = qresource.jobspec_resource('$vendor')
g = get_live_graph(flux.Flux())
types = {n.get('metadata', {}).get('type') for n in g.get('nodes', [])}

def walk(entries, out):
    for e in entries or []:
        out.append((e.get('type'), e.get('count'), e.get('exclusive', False)))
        walk(e.get('with'), out)
    return out

req = walk([want], [])
print('        the scout asks for:')
for t, c, x in req:
    mark = 'ok' if t in types or t in ('slot', 'core') else 'MISSING FROM GRAPH'
    print('          %-18s count=%-3s exclusive=%-5s %s' % (t, c, x, mark))
print('MISSING ' + json.dumps([t for t, _, _ in req
                               if t not in types and t not in ('slot', 'core')]))
" 2>/dev/null > /tmp/j.out || bad "could not build the scout jobspec"
grep -v '^MISSING ' /tmp/j.out
miss=$(grep '^MISSING ' /tmp/j.out | cut -d' ' -f2-)
if [ "$miss" = "[]" ]; then
    ok "every type the scout requests exists in the graph"
else
    bad "the scout requests types the graph does not have: $miss"
fi

echo ""
echo "=== 3. queue policy, per queue ==="
flux module stats sched-fluxion-qmanager 2>/dev/null | flux python -c "
import json, sys
qs = json.load(sys.stdin).get('queues', {})
# hold is only honoured by the coschedule policy now. The backfill
# policies were left stock, so a held job under them is an ordinary job.
reserving = {'coschedule'}
worst = None
for name, q in sorted(qs.items()):
    p = q.get('policy', '?')
    print('        queue %-10s policy=%-14s depth=%s' % (name, p, q.get('queue_depth')))
    if p not in reserving:
        worst = p
print('NONRESERVING ' + json.dumps(worst))
" > /tmp/p.out 2>/dev/null
grep -v '^NONRESERVING ' /tmp/p.out
nonres=$(grep '^NONRESERVING ' /tmp/p.out | cut -d' ' -f2-)
if [ "$nonres" = "null" ]; then
    ok "coschedule, so held jobs keep their footprint until released"
else
    bad "a queue runs $nonres, which does not support coscheduling"
    info "hold is honoured only by the coschedule policy. Under anything else"
    info "the classical half starts immediately instead of waiting for the scout."
    info "Set queue-policy = \"coschedule\" in the qmanager config."
fi

echo ""
echo "=== 4. jobtap plugin, and the config it was given ==="
if flux jobtap list 2>/dev/null | grep -q quantum; then
    ok "quantum.so loaded"
else
    bad "the quantum jobtap plugin is not loaded"
fi

echo ""
echo "=== 5. does fluxion actually allocate the device ==="
# flux resource list and ion-resource find both return RV1, which is keyed by
# rank, and a qpu sits at cluster level with rank -1. So neither can show it.
# The find RPC in jgf format can, and sched-now tells us free from allocated.
export FLUX_QUANTUM_MOCK=1
err=$(mktemp)
scout=$(flux submit --quantum-vendor "$vendor" -n1 -- sleep 20 2>"$err")
main=$(grep -oE 'held classical job [0-9]+' "$err" | awk '{print $NF}')
rm -f "$err"
if [ -z "$scout" ]; then
    bad "could not submit a pair"
else
    flux job wait-event -t 90 "$scout" alloc >/dev/null 2>&1
    sleep 2
    flux python -c "
import flux, json
h = flux.Flux()

def qpus(crit):
    try:
        r = h.rpc('sched-fluxion-resource.find',
                  {'criteria': crit, 'format': 'jgf'}).get()['R']
    except Exception:
        return []
    if isinstance(r, str):
        r = json.loads(r)
    if not r:
        return []
    return [n['metadata'].get('paths', {}).get('containment', '')
            for n in r.get('graph', {}).get('nodes', [])
            if n.get('metadata', {}).get('type') == 'qpu']

free, alloc = qpus('sched-now=free'), qpus('sched-now=allocated')
for p in sorted(alloc):
    print('        allocated  ' + p)
for p in sorted(free):
    print('        free       ' + p)
print('HELD ' + json.dumps([p for p in alloc if '$vendor' in p]))
" > /tmp/q.out 2>/dev/null
    grep -v '^HELD ' /tmp/q.out
    held=$(grep '^HELD ' /tmp/q.out | cut -d' ' -f2-)
    if [ "$held" = "[]" ] || [ -z "$held" ]; then
        bad "fluxion does not show the $vendor qpu as allocated while a scout holds it"
        info "nothing is reserving the device, so two jobs could use it at once"
    else
        ok "fluxion has the $vendor qpu allocated to the scout"
    fi
    flux cancel "$scout" >/dev/null 2>&1
    [ -n "$main" ] && flux cancel "$main" >/dev/null 2>&1
    sleep 3
fi

echo "=== 6. the handoff, end to end ==="
err=$(mktemp)
scout=$(flux submit --quantum-vendor "$vendor" -n1 -- true 2>"$err")
main=$(grep -oE 'held classical job [0-9]+' "$err" | awk '{print $NF}')
rm -f "$err"
if [ -z "$main" ]; then
    bad "no pair was created, cannot inspect R"
else
    if flux job wait-event -t 90 "$scout" alloc >/dev/null 2>&1; then
        ok "the scout was allocated a core alongside the device"
    else
        bad "the scout was never allocated"
        flux jobs -no "        {id} {state} {annotations}" "$scout"
    fi
    flux job wait-event -t 60 "$main" clean >/dev/null 2>&1
    log=$(flux job eventlog "$main" 2>/dev/null)
    if echo "$log" | grep -q 'memo'; then
        ok "memo posted to the classical eventlog"
    else
        bad "no memo, the scout never handed the session over"
    fi
    if echo "$log" | grep -q 'released'; then
        ok "the memo carries the durable released marker"
    else
        bad "no released marker, a qmanager restart would re-hold this job"
    fi
    if echo "$log" | grep -q 'jobspec-update.*protected'; then
        ok "the plugin stamped protected on the classical"
    else
        bad "no protected stamp, this job could be preempted"
    fi
    flux cancel "$scout" >/dev/null 2>&1
fi

echo ""
echo "=== rc=$rc ==="
exit "$rc"
