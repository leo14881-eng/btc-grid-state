#!/usr/bin/env bash
set -euo pipefail
JOB="${1:?JOB_REQUIRED}"
shift
case "$JOB" in discovery|research|watchdog|blind-replay|missed-replay|pipeline) ;; *) exit 2;; esac
exec 9>"/run/lock/hunter-job-$JOB.lock"
flock -n 9 || { echo "HUNTER_JOB_ALREADY_RUNNING $JOB"; exit 0; }
SOURCE=/opt/shadow-runner/btc-grid-state
REMOTE=$(git -C "$SOURCE" remote get-url origin)
TASK_DIR=
RETRY_LOG=$(mktemp /opt/shadow-runner/job-retry.XXXXXX)
trap 'cd "$SOURCE"; if [ -n "$TASK_DIR" ]; then rm -rf "$TASK_DIR"; fi; rm -f "$RETRY_LOG"' EXIT
run_generation() {
git clone --quiet --shared "$SOURCE" "$TASK_DIR/repo" || return
cd "$TASK_DIR/repo" || return
git remote set-url origin "$REMOTE" || return
git fetch origin main || return
git checkout --detach origin/main || return
mkdir "$TASK_DIR/bin" || return
ln -s /usr/bin/python3 "$TASK_DIR/bin/python" || return
export PATH="$TASK_DIR/bin:$PATH"
export PYTHONDONTWRITEBYTECODE=1
export RUNNER_TEMP="$TASK_DIR"
export GITHUB_OUTPUT="$TASK_DIR/output"
export GITHUB_ENV="$TASK_DIR/env"
export HUNTER_RESEARCH_ISOLATED_CHECKOUT=1
if [ "$JOB" = watchdog ]; then
  # Optional observation health never disables the authoritative hourly/5m chain.
  python3 -m scripts.hunter_market_stream --watchdog || echo FAST_WATCH_HEALTH_READ_FAILED
fi
if [ "$JOB" = blind-replay ]; then
  # Historical public market files only; GitHub main remains the state authority.
  export HUNTER_ARCHIVE_CACHE_DIR=/var/cache/hunter-binance-archives
fi
python3 -m scripts.hunter_job_runner "$JOB" "$@"
}
# Only auxiliary publication failures can retry a whole fresh-source job.
# Portfolio-writing Research never enters this launcher retry path.
for ATTEMPT in 1 2 3; do
  TASK_DIR=$(mktemp -d /opt/shadow-runner/job-generation.XXXXXX)
  if run_generation "$@" 2>&1 | tee "$RETRY_LOG"; then exit 0; else PIPE_RESULTS=("${PIPESTATUS[@]}"); STATUS=${PIPE_RESULTS[0]}; fi
  if [ "$STATUS" -eq 0 ]; then STATUS=1; fi
  case "$JOB" in
    discovery|blind-replay|missed-replay) ;;
    *) exit "$STATUS" ;;
  esac
  if [ "$ATTEMPT" -ge 3 ] || ! grep -Eq '^RuntimeError: (AUX_CAS_REJECTED_STALE_WRITER|AUX_PUSH_RETRY_EXHAUSTED_RACE|AUX_PUSH_RETRY_EXHAUSTED_TRANSPORT_FAILED)([[:space:]]|$)' "$RETRY_LOG"; then
    exit "$STATUS"
  fi
  echo "HUNTER_AUX_FRESH_SOURCE_RETRY $JOB attempt=$ATTEMPT"
  cd "$SOURCE"
  rm -rf "$TASK_DIR"
  TASK_DIR=
done
