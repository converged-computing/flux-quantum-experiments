#!/bin/bash
# Set up a fresh Ubuntu EC2 instance to sample the queues for weeks, as a
# systemd service that starts at boot and starts again if it dies.
#
#     ssh ubuntu@HOST
#     git clone https://github.com/converged-computing/flux-quantum-experiments
#     bash flux-quantum-experiments/queues/ec2/setup.sh
#
# Then put the credentials in /etc/queues.env, run the preflight, and start
# the service. The README's EC2 section has the three commands.
#
# Safe to run again: it updates the checkouts and the unit and leaves
# /etc/queues.env alone once it has been written.
set -euo pipefail

HOME_DIR=${HOME_DIR:-$HOME}
VENV=$HOME_DIR/queues-venv
EXPERIMENTS=$HOME_DIR/flux-quantum-experiments
QUANTUM=$HOME_DIR/flux-quantum
QUEUES=$EXPERIMENTS/queues
ENV_FILE=/etc/queues.env

echo "=== packages"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv python3-pip git rsync > /dev/null

echo "=== checkouts"
# the sampler imports flux_quantum.backends for the Braket window check and
# the IonQ client. Those modules are plain python, so no Flux is needed
[ -d "$QUANTUM" ] || git clone --depth 1 https://github.com/converged-computing/flux-quantum "$QUANTUM"
[ -d "$EXPERIMENTS" ] || git clone --depth 1 https://github.com/converged-computing/flux-quantum-experiments "$EXPERIMENTS"
git -C "$QUANTUM" pull -q --ff-only || true
git -C "$EXPERIMENTS" pull -q --ff-only || true

echo "=== python"
[ -d "$VENV" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -e "$QUANTUM" amazon-braket-sdk qiskit-ibm-runtime boto3
"$VENV/bin/python3" -c "import flux_quantum.backends.braket, flux_quantum.backends.ionq.client, braket.aws, qiskit_ibm_runtime; print('  imports ok')"

echo "=== credentials file $ENV_FILE"
if [ ! -f "$ENV_FILE" ]; then
    sudo tee "$ENV_FILE" > /dev/null <<'EOF'
# Read by the queues service (systemd EnvironmentFile: no quotes, no export).
# A vendor without credentials is skipped, not failed.
# When to stop sampling, a fixed UTC time. The service ends itself then.
UNTIL=2026-12-31T23:59:59Z
# AWS, an IAM access key. SSO and other temporary credentials expire mid run.
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
AWS_DEFAULT_REGION=us-east-1
IONQ_API_KEY=
# an IBM Cloud API key and the CRN of the Quantum instance
IBM_CLOUD_TOKEN=
IBM_CLOUD_CRN=
EOF
    # yours to edit and for the preflight to read; systemd reads it as root
    sudo chown "$(id -un)" "$ENV_FILE"
    sudo chmod 600 "$ENV_FILE"
    echo "  written, fill it in: nano $ENV_FILE"
else
    echo "  exists, left alone"
fi

echo "=== service"
sed -e "s#@QUEUES@#$QUEUES#g" -e "s#@PY@#$VENV/bin/python3#g" -e "s#@USER@#$(id -un)#g" \
    "$QUEUES/ec2/queues.service" | sudo tee /etc/systemd/system/queues.service > /dev/null
sudo systemctl daemon-reload
echo "  installed /etc/systemd/system/queues.service"

cat <<EOF

Next, as in the README:
  nano $ENV_FILE                                             # the credentials
  (set -a; . $ENV_FILE; set +a; PY=$VENV/bin/python3 bash $QUEUES/preflight.sh)
  sudo systemctl enable --now queues                         # start, and at every boot
  journalctl -u queues -f                                    # watch
EOF
