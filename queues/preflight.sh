#!/bin/bash
# Check that each service can be read before the sampler runs for days.
# Nothing here costs anything: one tick, printed, per vendor that has
# credentials. A vendor without them is reported and skipped, not failed,
# since the sampler does the same.
#
#     bash preflight.sh 2>&1 | tee preflight.log
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
rc=0
ok()   { echo "  ok    $*"; }
bad()  { echo "  FAIL  $*"; rc=1; }
warn() { echo "  warn  $*"; }
sec()  { echo; echo "=== $* ==="; }
PY=${PY:-python3}

sec "0. python and the packages"
$PY -c "import flux_quantum.backends.braket, flux_quantum.backends.ionq.client" 2>/dev/null && ok "flux_quantum importable" || bad "flux_quantum is not importable by $PY"
$PY -c "import boto3, braket.aws" 2>/dev/null && ok "braket sdk" || warn "no braket sdk, pip install amazon-braket-sdk, Braket will be skipped"
$PY -c "import qiskit_ibm_runtime" 2>/dev/null && ok "qiskit-ibm-runtime" || warn "no qiskit-ibm-runtime, pip install qiskit-ibm-runtime, IBM will be skipped"

sec "1. credentials, by name"
[ -n "${AWS_ACCESS_KEY_ID:-}" ] || [ -f "$HOME/.aws/credentials" ] && ok "aws credentials" || warn "no AWS credentials in the environment or ~/.aws"
[ -n "${IONQ_API_KEY:-}${IONQ_API_TOKEN:-}" ] && ok "IONQ_API_KEY" || warn "no IONQ_API_KEY"
if [ -n "${QISKIT_IBM_TOKEN:-}${IBM_CLOUD_TOKEN:-}" ] || env | grep -q "_QRMI_IBM_QRS_IAM_APIKEY="; then ok "ibm credentials"; else warn "no IBM credentials"; fi

sec "2. one tick from each vendor"
out=$(mktemp)
$PY "$HERE/scripts/sample.py" --once --out /dev/null 2>&1 | tee "$out" | sed 's/^/  /' | cut -c1-160
# each row's own error field, not the text of the row, since a device's
# properties can contain the word
$PY - "$out" <<'PYEOF' || rc=1
import json, sys
rows = []
for line in open(sys.argv[1]):
    line = line.strip()
    if line.startswith("{"):
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
rc = 0
for v in ("braket", "ionq", "ibm"):
    mine = [r for r in rows if r.get("vendor") == v]
    if not mine:
        print("  warn  {} not sampled".format(v))
        continue
    bad = [r for r in mine if r.get("error")]
    if bad:
        print("  FAIL  {} answered with errors:".format(v))
        for r in bad:
            print("          {}: {}".format(r.get("device"), str(r["error"])[:200]))
        rc = 1
    else:
        print("  ok    {} reports {} device(s)".format(v, len(mine)))
if not any(r.get("kind") == "sample" for r in rows):
    print("  FAIL  no vendor reported anything")
    rc = 1
sys.exit(rc)
PYEOF
rm -f "$out"

sec "3. the paired machines"
$PY - "$HERE/devices.json" <<'PYEOF'
import json, sys
spec = json.load(open(sys.argv[1]))
for name, p in spec["pairs"].items():
    print("  {:<22} braket {:<60} ionq {}".format(name, p["braket"].split("/")[-1], p["ionq"]))
    if p["braket"] not in spec["braket"] or p["ionq"] not in spec["ionq"]:
        print("  FAIL {} is paired but not in the sampled list".format(name)); sys.exit(1)
PYEOF
[ $? -eq 0 ] || rc=1

echo
[ "$rc" = 0 ] && echo "=== PASS: nohup bash run.sh > results/run.log 2>&1 & ===" || echo "=== FAIL ==="
exit "$rc"
