#!/usr/bin/env bash
set -euo pipefail
JOB="${1:?JOB_REQUIRED}"
shift
case "$JOB" in discovery|research|watchdog|blind-replay|missed-replay|pipeline) ;; *) exit 2;; esac
# Fixed phase labels only: never print command arguments, paths or environment.
# Use the system interpreter so a checkout/PATH change cannot change the clock.
monotonic_ns() { /usr/bin/python3 -I -S -c 'import time; print(time.monotonic_ns())'; }
LAUNCH_STARTED=$(monotonic_ns)
ATTEMPT=0
timed() {
  local phase=$1 started finished status=0
  shift
  started=$(monotonic_ns)
  echo "HUNTER_LAUNCH_PHASE_START job=$JOB attempt=$ATTEMPT phase=$phase clock=monotonic_ns"
  "$@" || status=$?
  finished=$(monotonic_ns)
  echo "HUNTER_LAUNCH_PHASE_END job=$JOB attempt=$ATTEMPT phase=$phase elapsed_ns=$((finished-started)) exit=$status clock=monotonic_ns"
  return "$status"
}
exec 9>"/run/lock/hunter-job-$JOB.lock"
timed admission flock -n 9 || { echo "HUNTER_JOB_ALREADY_RUNNING $JOB"; exit 0; }
SOURCE=/opt/shadow-runner/btc-grid-state
REMOTE=$(git -C "$SOURCE" remote get-url origin)
TASK_DIR=
RETRY_LOG=$(mktemp /opt/shadow-runner/job-retry.XXXXXX)
cleanup() {
  cd "$SOURCE" || return
  if [ -n "$TASK_DIR" ]; then rm -rf "$TASK_DIR"; fi
  rm -f "$RETRY_LOG"
}
finish() {
  local status=$? finished
  timed cleanup cleanup || true
  finished=$(monotonic_ns)
  echo "HUNTER_LAUNCH_TOTAL job=$JOB elapsed_ns=$((finished-LAUNCH_STARTED)) exit=$status clock=monotonic_ns scope=launcher_entry_through_cleanup"
  exit "$status"
}
trap finish EXIT
run_generation() {
timed clone git clone --quiet --shared "$SOURCE" "$TASK_DIR/repo" || return
cd "$TASK_DIR/repo" || return
git remote set-url origin "$REMOTE" || return
timed fetch git fetch origin main || return
timed checkout git checkout --detach origin/main || return
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
  timed watchdog_health python3 -m scripts.hunter_market_stream --watchdog || echo FAST_WATCH_HEALTH_READ_FAILED
fi
if [ "$JOB" = blind-replay ]; then
  # Historical public market files only; GitHub main remains the state authority.
  export HUNTER_ARCHIVE_CACHE_DIR=/var/cache/hunter-binance-archives
fi
timed runner python3 -m scripts.hunter_job_runner "$JOB" "$@"
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
  timed retry_cleanup rm -rf "$TASK_DIR"
  TASK_DIR=
done
