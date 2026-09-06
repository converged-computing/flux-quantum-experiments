#!/bin/bash
# Run this on the instance before spending money on a campaign. It checks the
# live runtime state, not the config files, because every wasted run so far came
# from a config that was silently not in force.
#
#     bash preflight.sh 2>&1 | tee preflight-$(date +%Y%m%d-%H%M).log
#
# Exit 0 means go. Anything else means stop and read.
#
# Nothing here costs vendor money. The mock vendor is free and no IBM call is
# made. It takes about three minutes.

set -u
export FLUX_QUANTUM_MOCK=1
rc=0
GRAPH_OK=0
VENDORS="${VENDORS:-ibm braket mock}"
ok()   { echo "  ok    $*"; }
bad()  { echo "  FAIL  $*"; rc=1; }
warn() { echo "  warn  $*"; }
sec()  { echo; echo "=== $* ==="; }

# Correct idioms. A format string with no state field merges every state onto
# one line, so the bare -no {ncores} form returns the total while reading like
# the free count.
total_cores() { flux resource list -s all  -no "{ncores}"; }
free_cores()  { flux resource list -s free -no "{ncores}"; }

# Cancel only the ids that exist. Passing an empty string makes flux cancel fail
# on the whole argument list, so one missing id leaves everything else running
# and the next check runs on a busy machine.
cancel_jobs() {
    local id
    for id in "$@"; do
        [ -n "$id" ] || continue
        flux cancel "$id" >/dev/null 2>&1
        flux job wait-event -t 20 "$id" clean >/dev/null 2>&1
    done
}

CORES=$(total_cores)
EXPECT_CORES="${EXPECT_CORES:-$CORES}"

sec "0. the instance must be idle before any of this means anything"
# A leftover job silently invalidated a whole previous run of this script: the
# core accounting check compared against capacity that something else held.
leftover=$(flux jobs -no "{id} {state} {nnodes} {name}")
if [ -n "$leftover" ]; then
    echo "$leftover" | sed 's/^/  /'
    echo "  STOP. Clear these first:  flux cancel --all  (then re-run)"
    exit 1
fi
ok "no active jobs"
f0=$(free_cores)
[ "$f0" = "$CORES" ] && ok "all $CORES cores free" \
    || { bad "only $f0 of $CORES cores free on an empty queue"; exit 1; }

sec "1. versions and branches"
flux version | head -2 | sed 's/^/  /'
for d in /opt/flux-sched /opt/flux-quantum; do
    [ -d "$d" ] || { warn "$d absent, no source tree to record a commit from"; continue; }
    b=$(git -C "$d" rev-parse --abbrev-ref HEAD 2>/dev/null)
    h=$(git -C "$d" rev-parse --short HEAD 2>/dev/null)
    echo "  $d  branch=$b  head=$h"
done
# The design does not work without these. A wrong branch looks like the design
# failing rather than the branch being wrong.
# The installed binary is what runs. A source tree can be right, absent, or
# rebuilt-but-not-installed and none of that tells you what the broker loaded.
# "was already released" is the flux_log literal from jobmanager_alloc_cb, so it
# is only present if durable release is actually compiled in.
qmod=$(find /usr /usr/local -name 'sched-fluxion-qmanager*.so' 2>/dev/null | head -1)
if [ -z "$qmod" ]; then
    bad "no sched-fluxion-qmanager module found on disk"
elif strings "$qmod" | grep -q "was already released"; then
    ok "installed qmanager has durable release ($qmod)"
else
    bad "installed qmanager predates durable release ($qmod), rebuild and reinstall flux-sched"
fi
grep -q "cannot read plugin config" /opt/flux-quantum/flux_quantum/jobtap/quantum.c 2>/dev/null \
    && ok "flux-quantum has the config-fail-loud plugin" \
    || bad "flux-quantum predates the plugin config fix, re-clone qrmi"

sec "2. the plugin is loaded and its config is in force"
flux jobtap list | sed 's/^/  /'
flux jobtap list | grep -q quantum || bad "quantum jobtap plugin is not loaded"
# This is the line that would have caught the last campaign. It is what the
# plugin actually holds, not what the toml says.
cfg=$(flux dmesg | grep -o "quantum: vendors=.*" | tail -1)
[ -n "$cfg" ] && echo "  $cfg" || bad "no quantum config line in dmesg, plugin did not init"
pa=$(echo "$cfg" | grep -o "preempt_after=[0-9.]*" | cut -d= -f2)
tc=$(echo "$cfg" | grep -o "total_cores=[0-9]*" | cut -d= -f2)
case "$pa" in
    ""|0|0.0) bad "PREEMPTION IS OFF. This is the error that invalidated e1 to e4." ;;
    *)        ok "preemption on, preempt_after=${pa}s" ;;
esac
[ "$tc" = "$EXPECT_CORES" ] && ok "admission budget $tc matches $EXPECT_CORES cores" \
    || bad "admission budget is $tc but the cluster has $EXPECT_CORES cores"

sec "3. the live queue policy, not the config file"
pol=$(flux module stats sched-fluxion-qmanager 2>/dev/null | flux python -c "
import json,sys
try:
    print(','.join(sorted({v.get('policy','?') for v in json.load(sys.stdin).get('queues',{}).values()})))
except Exception:
    print('unknown')")
echo "  live policy: $pol"
case "$pol" in
    easy|hybrid|conservative) ok "policy reserves for held jobs" ;;
    fcfs) bad "fcfs never reserves for a held job, the design silently loses its cores" ;;
    *)    bad "could not read the live policy, do not trust the config file alone" ;;
esac

sec "4. resource accounting reads correctly"
echo "  total=$(total_cores)  free=$(free_cores)"
[ "$CORES" = "$EXPECT_CORES" ] || bad "total is $CORES, expected $EXPECT_CORES"
[ "$(free_cores)" = "$CORES" ] || warn "cluster is not idle, later checks may be noisy"
probe=$(flux submit -n4 sleep 15)
flux job wait-event -t 30 "$probe" start >/dev/null 2>&1
f=$(free_cores)
[ "$f" = "$(( CORES - 4 ))" ] && ok "a 4 core job moves free to $f" \
    || bad "free is $f after a 4 core job, expected $(( CORES - 4 ))"
cancel_jobs "$probe"

sec "5. the qdevice vertices are in the graph"
# The scout matches qdevice_<vendor> -> qpu with the qpu exclusive, so a qdevice
# with no qpu under it can never be matched. A previous run passed this check
# with zero qpus, which would have wasted the entire campaign.
flux python -c "
import flux, json, sys
want = set(sys.argv[1:])
h = flux.Flux()
r = h.rpc('sched-fluxion-resource.find', {'criteria':'status=up','format':'jgf'}).get()['R']
r = json.loads(r) if isinstance(r, str) else r
g = r.get('graph', {})
ty  = {n['id']: n['metadata']['type'] for n in g.get('nodes', [])}
par = {e['target']: e['source'] for e in g.get('edges', [])}
qpus = {}
for vid, t in ty.items():
    if t == 'qpu':
        qpus[ty.get(par.get(vid), '?')] = qpus.get(ty.get(par.get(vid), '?'), 0) + 1
qd = {t for t in ty.values() if t.startswith('qdevice_')}
for v in sorted(want):
    d = 'qdevice_' + v
    n = qpus.get(d, 0)
    print('  %-16s qdevice=%s qpus=%d' % (v, d in qd, n))
missing = [v for v in want if qpus.get('qdevice_' + v, 0) < 1]
orphan  = [d for d in qd if qpus.get(d, 0) < 1]
if orphan:
    print('  ORPHAN qdevices with no qpu:', sorted(orphan))
raise SystemExit(1 if (missing or orphan) else 0)" $VENDORS
if [ $? = 0 ]; then GRAPH_OK=1; ok "every vendor has a qdevice and at least one qpu"
else GRAPH_OK=0; bad "graph is incomplete. On an IDLE instance: sudo systemctl restart flux, then flux python -m flux_quantum.populate $VENDORS"
fi

sec "6. the whole-cluster request, the one thing never root caused"
# -n128 once sat in SCHED forever while -n127 ran, and it was never explained.
# If it happens here it will silently distort every contention sweep.
#
# This runs after the graph check on purpose. The suspicion is that the qdevice
# vertices are involved, so testing it on a graph without them proves nothing.
if [ "$GRAPH_OK" != 1 ]; then
    warn "skipped, the graph has no qdevice vertices so this would prove nothing"
else
for n in $(( CORES - 1 )) "$CORES"; do
    id=$(flux submit -n"$n" sleep 8)
    sleep 5
    st=$(flux jobs -no "{state}" "$id" 2>/dev/null)
    [ "$st" = RUN ] && ok "-n$n reached RUN" || bad "-n$n is $st with $(free_cores) free, the old mystery is live"
    cancel_jobs "$id"
done
fi

sec "7. a pair actually strands when the machine is full"
# This is the premise of e2, e4 and e5. If it does not hold, those sweeps
# measure nothing, which is what a whole day went into last time.
SZ=8
load=$(flux submit -n$(( CORES - SZ )) sleep 120)
flux job wait-event -t 30 "$load" start >/dev/null 2>&1
echo "  load holds $(( CORES - SZ )), free=$(free_cores), a pair needs $(( SZ + 1 ))"
a=$(flux submit -n1 sleep 60); sleep 2
b=$(flux submit -n$SZ sleep 60); sleep 5
sb=$(flux jobs -no "{state}" "$b" 2>/dev/null)
[ "$sb" = SCHED ] && ok "the classical-sized job strands in SCHED as required" \
    || bad "a $SZ core job is $sb with $(free_cores) free, contention sweeps will measure 0"
cancel_jobs "$load" "$a" "$b"

sec "8. preemption actually fires"
# Verified in isolation before, but it was off for an entire campaign, so
# confirm it on this instance rather than assume.
big=$(flux submit -n$(( CORES - 4 )) sleep 300)
flux job wait-event -t 30 "$big" start >/dev/null 2>&1
err=$(mktemp)
scout=$(flux submit --quantum-vendor mock --quantum-mock-queue-depth 0 \
        -n8 -- sh -c 'sleep 2' 2>"$err")
main=$(grep -oE 'held classical job [0-9]+' "$err" | awk '{print $NF}')
if [ -z "$main" ]; then
    bad "no classical job was created: $(tail -1 "$err" | cut -c1-90)"
else
    if flux job wait-event -t $(( ${pa%.*} + 40 )) "$main" start >/dev/null 2>&1; then
        ok "the pair started on a full machine, so preemption reclaimed cores"
        v=$(flux jobs -a -no "{id} {state}" "$big" 2>/dev/null)
        echo "  victim: $v"
    else
        bad "the pair never started, preemption did not fire, session would bill"
    fi
fi
rm -f "$err"
cancel_jobs "$big" "$scout" "$main"

sec "9. admission control rejects an oversized pair"
out=$(flux submit --quantum-vendor mock -n$(( CORES * 10 )) true 2>&1)
echo "$out" | grep -q "no room for another pair" \
    && ok "rejected at submit, not admitted and left waiting" \
    || bad "an oversized pair was not rejected: $(echo "$out" | tail -1 | cut -c1-80)"

sec "10. drain"
flux cancel --all >/dev/null 2>&1
sleep 3
for _ in $(seq 1 30); do flux jobs -no "{id}" | grep -q . || break; sleep 2; done
left=$(flux jobs -no "  leftover {id} {state} {name}")
[ -n "$left" ] && { echo "$left"; bad "active jobs remain"; } || ok "nothing left running"

echo
if [ "$rc" = 0 ]; then
    echo "=== PASS. The instance is in a known good state. ==="
    echo
    echo "  If you have not piloted this build:  TRIALS=1 ./run.sh e1 e2 e3 e4 e5"
    echo "  Otherwise the full campaign:         ./run.sh e1 e2 e3 e4 e5"
    echo
    echo "  Afterwards, in this order:"
    echo "    1. every results/*.meta says preempt 1, and records the timeouts used"
    echo "    2. summary.txt, the section listing conditions where an arm could"
    echo "       not run. A censored cell is a result, but a whole condition"
    echo "       censored across every trial means the sweep missed its point"
    echo "    3. that each metric VARIES across conditions. At TRIALS=1 every sd"
    echo "       is 0.000 because n=1, so spread tells you nothing. What tells"
    echo "       you a knob is connected is the mean moving with the condition."
else
    echo "=== STOP. Something above is wrong. Fix it before running anything. ==="
fi
exit "$rc"
