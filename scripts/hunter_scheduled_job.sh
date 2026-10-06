#!/usr/bin/env bash
set -euo pipefail
JOB="${1:?JOB_REQUIRED}"
shift
case "$JOB" in discovery|research|watchdog|blind-replay|missed-replay|pipeline) ;; *) exit 2;; esac
exec 9>"/run/lock/hunter-job-$JOB.lock"
flock -n 9 || { echo "HUNTER_JOB_ALREADY_RUNNING $JOB"; exit 0; }
TASK_DIR=$(mktemp -d /opt/shadow-runner/job-generation.XXXXXX)
trap 'rm -rf "$TASK_DIR"' EXIT
SOURCE=/opt/shadow-runner/btc-grid-state
REMOTE=$(git -C "$SOURCE" remote get-url origin)
git clone --quiet --shared "$SOURCE" "$TASK_DIR/repo"
cd "$TASK_DIR/repo"
git remote set-url origin "$REMOTE"
git fetch origin main
git checkout --detach origin/main
mkdir "$TASK_DIR/bin"
ln -s /usr/bin/python3 "$TASK_DIR/bin/python"
export PATH="$TASK_DIR/bin:$PATH"
export PYTHONDONTWRITEBYTECODE=1
export RUNNER_TEMP="$TASK_DIR"
export GITHUB_OUTPUT="$TASK_DIR/output"
export GITHUB_ENV="$TASK_DIR/env"
export HUNTER_RESEARCH_ISOLATED_CHECKOUT=1
if [ "$JOB" = blind-replay ]; then
  # Historical public market files only; GitHub main remains the state authority.
  export HUNTER_ARCHIVE_CACHE_DIR=/var/cache/hunter-binance-archives
fi
python3 -m scripts.hunter_job_runner "$JOB" "$@"
