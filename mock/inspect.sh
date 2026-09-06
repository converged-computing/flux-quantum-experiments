#!/bin/bash
# Inspect the coscheduling setup structurally: the fluxion graph, the jobspec
# the plugin builds, the allocation that comes back, and the plugin config.
# preflight.sh says whether it works, this says what is there.
#
#     bash inspect.sh [vendor]

set -u
vendor="${1:-mock}"
rc=0
ok()   { echo "  ok    $*"; }
bad()  { echo "  FAIL  $*"; rc=1; }
info() { echo "        $*"; }

echo "=== 1. fluxion graph ==="
flux python -c "
import json, sys, flux
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
paths = [m.get('paths', {}).get('containment', '') for m in by_type.get('qpu', [])]
print('QPU_PATHS ' + json.dumps(paths))
print('BAD_PATHS ' + json.dumps([p for p in paths if '/qdevice_' not in p]))
" 2>/tmp/g.err > /tmp/g.out || { bad "could not read the graph"; cat /tmp/g.err; exit 1; }
grep -v '^[A-Z_][A-Z_]* ' /tmp/g.out
qpaths=$(grep '^QPU_PATHS ' /tmp/g.out | cut -d' ' -f2-)
bpaths=$(grep '^BAD_PATHS ' /tmp/g.out | cut -d' ' -f2-)
if [ "$qpaths" = "[]" ]; then
    bad "no qpu vertices in the graph"
    info "a broker restart empties it. Populate at startup, before any job runs:"
    info "flux python -m flux_quantum.populate ibm braket mock"
elif [ "$bpaths" != "[]" ]; then
    bad "a qpu is not under a qdevice: $bpaths"
else
    ok "every qpu sits under a qdevice"
fi

echo ""
echo "=== 2. the scout jobspec against the graph ==="
flux python -c "
import json, flux
from flux_quantum import qresource
from flux_quantum.graph import get_live_graph

want = qresource.jobspec_resource('$vendor')
types = {n.get('metadata', {}).get('type') for n in get_live_graph(flux.Flux()).get('nodes', [])}

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
print('MISSING ' + json.dumps([t for t, _, _ in req if t not in types and t not in ('slot', 'core')]))
" 2>/dev/null > /tmp/j.out || bad "could not build the scout jobspec"
grep -v '^MISSING ' /tmp/j.out
miss=$(grep '^MISSING ' /tmp/j.out | cut -d' ' -f2-)
[ "$miss" = "[]" ] && ok "every type the scout requests exists in the graph" \
    || bad "the scout requests types the graph does not have: $miss"

echo ""
echo "=== 3. queue policy ==="
flux module stats sched-fluxion-qmanager 2>/dev/null | flux python -c "
import json, sys
qs = json.load(sys.stdin).get('queues', {})
worst = None
for name, q in sorted(qs.items()):
    p = q.get('policy', '?')
    print('        queue %-10s policy=%-14s depth=%s' % (name, p, q.get('queue_depth')))
    if p != 'coschedule':
        worst = p
print('NONRESERVING ' + json.dumps(worst))
" > /tmp/p.out 2>/dev/null
grep -v '^NONRESERVING ' /tmp/p.out
nonres=$(grep '^NONRESERVING ' /tmp/p.out | cut -d' ' -f2-)
[ "$nonres" = "null" ] && ok "coschedule, so held jobs keep their footprint until released" \
    || bad "a queue runs $nonres, and hold is only honoured by coschedule"

echo ""
echo "=== 4. jobtap plugin ==="
flux jobtap list 2>/dev/null | grep -q quantum && ok "quantum.so loaded" \
    || bad "the quantum jobtap plugin is not loaded"

echo ""
echo "=== 5. fluxion allocates the device ==="
# a qpu sits at cluster level with rank -1, so RV1 cannot show it. The find
# RPC in jgf format can
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
        r = h.rpc('sched-fluxion-resource.find', {'criteria': crit, 'format': 'jgf'}).get()['R']
    except Exception:
        return []
    r = json.loads(r) if isinstance(r, str) else r
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
        bad "the $vendor qpu is not allocated while a scout holds it, so two jobs could use it at once"
    else
        ok "fluxion has the $vendor qpu allocated to the scout"
    fi
    flux cancel "$scout" >/dev/null 2>&1
    [ -n "$main" ] && flux cancel "$main" >/dev/null 2>&1
    sleep 3
fi

echo ""
echo "=== 6. the handoff ==="
err=$(mktemp)
scout=$(flux submit --quantum-vendor "$vendor" -n1 -- true 2>"$err")
main=$(grep -oE 'held classical job [0-9]+' "$err" | awk '{print $NF}')
rm -f "$err"
if [ -z "$main" ]; then
    bad "no pair was created"
else
    if flux job wait-event -t 90 "$scout" alloc >/dev/null 2>&1; then
        ok "the scout was allocated a core alongside the device"
    else
        bad "the scout was never allocated"
        flux jobs -no "        {id} {state} {annotations}" "$scout"
    fi
    flux job wait-event -t 60 "$main" clean >/dev/null 2>&1
    log=$(flux job eventlog "$main" 2>/dev/null)
    echo "$log" | grep -q 'memo' && ok "memo posted to the classical eventlog" \
        || bad "no memo, the scout never handed the session over"
    echo "$log" | grep -q 'released' && ok "the memo carries the durable released marker" \
        || bad "no released marker, a qmanager restart would re-hold this job"
    echo "$log" | grep -q 'jobspec-update.*protected' && ok "the plugin stamped protected on the classical" \
        || bad "no protected stamp, this job could be preempted"
    flux cancel "$scout" >/dev/null 2>&1
fi

echo ""
echo "=== rc=$rc ==="
exit "$rc"
