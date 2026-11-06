#!/bin/bash
# Copy the instance's samples here, into results/ec2/, one file a day, and
# its logs. Run it as often as you like; rsync sends only what is new, and a
# day's file that is still growing is sent whole again.
#
#     bash ec2/pull.sh ubuntu@HOST
#     bash analyze.sh results-days/queues.jsonl results/queues.jsonl results/queues results/ec2
#
# The instance's files go in their own directory so a day sampled both here
# and there, the day the run moved, does not overwrite either copy. The
# readers take any number of directories.
set -eu
HERE=$(cd "$(dirname "$0")/.." && pwd)
HOST=${1:?usage: bash ec2/pull.sh ubuntu@HOST [REMOTE_QUEUES_DIR]}
REMOTE=${2:-flux-quantum-experiments/queues}
mkdir -p "$HERE/results/ec2"
rsync -av --progress "$HOST:$REMOTE/results/queues/" "$HERE/results/ec2/"
rsync -av "$HOST:$REMOTE/results/run.log" "$HERE/results/ec2/run.log"
du -sh "$HERE/results/ec2"
