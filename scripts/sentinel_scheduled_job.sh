#!/usr/bin/env bash
set -euo pipefail
# Read-only deployment until the original Leading Warning engine is available.
exec 9>/run/lock/sentinel-evidence-scan.lock
flock -n 9 || { echo SENTINEL_ALREADY_RUNNING; exit 0; }
SOURCE=/opt/shadow-runner/btc-grid-state
TASK_DIR=$(mktemp -d /opt/shadow-runner/sentinel-evidence.XXXXXX)
trap 'rm -rf "$TASK_DIR"' EXIT
REMOTE=$(git -C "$SOURCE" remote get-url origin)
git clone --quiet --shared "$SOURCE" "$TASK_DIR/repo"
cd "$TASK_DIR/repo"
git remote set-url origin "$REMOTE"
git fetch origin main
git checkout --detach origin/main
export PYTHONDONTWRITEBYTECODE=1
install -d -m 0755 /var/lib/sentinel-evidence
python3 -m scripts.sentinel_runtime --preview --evidence-output /var/lib/sentinel-evidence/preview.json
