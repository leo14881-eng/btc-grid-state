#!/usr/bin/env bash
set -euo pipefail
CAS_RETRY_COUNT=${HUNTER_MONITOR_CAS_RETRY_COUNT:-0}
[[ "$CAS_RETRY_COUNT" =~ ^[012]$ ]] || { echo INVALID_MONITOR_CAS_RETRY_COUNT; exit 1; }
# Prepare an isolated checkout and test code before joining the portfolio lock.
# Prepared state is never executed: refresh it again inside the shared lock.
SOURCE=/opt/shadow-runner/btc-grid-state
TASK_DIR=$(mktemp -d /opt/shadow-runner/monitor-generation.XXXXXX)
LOCKED=0
release_lock() {
  if [ "$LOCKED" = 1 ]; then
    python3 -m scripts.hunter_portfolio_lock release --fd 9 --pid "$$" --status "${1:-complete}" || echo PORTFOLIO_LOCK_AUDIT_RELEASE_FAILED
    exec 9>&-
    LOCKED=0
  fi
}
cleanup() {
  STATUS=$?
  trap - EXIT
  release_lock "exit=$STATUS"
  cd "$SOURCE"
  rm -rf "$TASK_DIR"
  exit "$STATUS"
}
trap cleanup EXIT
PREPARE_STARTED=$SECONDS
REMOTE=$(git -C "$SOURCE" remote get-url origin)
git clone --quiet --shared "$SOURCE" "$TASK_DIR/repo"
cd "$TASK_DIR/repo"
git remote set-url origin "$REMOTE"
git fetch origin main
git checkout --detach origin/main
export PYTHONDONTWRITEBYTECODE=1
export HUNTER_MONITOR_ISOLATED_CHECKOUT=1
PREPARED_SHA=$(git rev-parse HEAD)
if ! python3 -m unittest tests.test_hunter_shadow_trader_v2 tests.test_hunter_position_monitor tests.test_hunter_tail_risk tests.test_hunter_leading_risk tests.test_hunter_scheduler_health tests.test_hunter_monitor_persist > "$TASK_DIR/regression.log" 2>&1; then
  cat "$TASK_DIR/regression.log"
  exit 1
fi
tail -n 4 "$TASK_DIR/regression.log"
echo "HUNTER_MONITOR_PREPARE_TIMING seconds=$((SECONDS - PREPARE_STARTED)) base=$PREPARED_SHA"
# Do not truncate/unlink the lock file: it also holds diagnostic owner metadata.
exec 9<>/run/lock/hunter-position-monitor.lock
python3 -m scripts.hunter_portfolio_lock acquire --fd 9 --pid "$$" --base-sha "$PREPARED_SHA"
LOCKED=1
python3 -m scripts.hunter_portfolio_lock phase --fd 9 --pid "$$" --name rebind
python3 scripts/hunter_monitor_persist.py rebind --base "$PREPARED_SHA" --isolated-checkout
BASE_SHA=$(git rev-parse HEAD)
STARTED_AT=$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)
GATE=$(python3 -m research.hunter_scheduler_health gate)
echo "$GATE"
PROCESS=$(python3 -c 'import json,sys; print(str(json.load(sys.stdin)["process"]).lower())' <<<"$GATE")
GENERATION=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["generation_id"])' <<<"$GATE")
[ "$PROCESS" = true ] || { echo "ALREADY_PROCESSED generation=$GENERATION"; exit 0; }
python3 -m scripts.hunter_portfolio_lock phase --fd 9 --pid "$$" --name evaluate --generation "$GENERATION" --base-sha "$BASE_SHA"
python3 -m research.hunter_position_monitor
# Preserve the generation admitted at the gate, even across a time boundary.
python3 -m research.hunter_scheduler_health success --started-at "$STARTED_AT" --generation "$GENERATION" --trigger-source VULTR_SYSTEMD --state-revision "$BASE_SHA"
python3 scripts/hunter_monitor_persist.py validate --generation "$GENERATION"
python3 -m scripts.hunter_portfolio_lock phase --fd 9 --pid "$$" --name persist_and_readback
git config user.name hunter-vultr-shadow
git config user.email hunter-vultr-shadow@localhost
if python3 scripts/hunter_monitor_persist.py persist --base "$BASE_SHA" --generation "$GENERATION" > "$TASK_DIR/persist.log" 2>&1; then
  cat "$TASK_DIR/persist.log"
else
  PERSIST_STATUS=$?
  cat "$TASK_DIR/persist.log"
  # A protected input changed while this observation ran. Never reuse its
  # decisions/output or weaken CAS: re-enter admission and recompute on main.
  # Two fresh retries are bounded by this service's existing 240s timeout.
  if [ "$CAS_RETRY_COUNT" -lt 2 ] && grep -Eq '^RuntimeError: SHADOW_STATE_CAS_REJECTED_STALE_WRITER( |$)' "$TASK_DIR/persist.log"; then
    export HUNTER_MONITOR_CAS_RETRY_COUNT=$((CAS_RETRY_COUNT + 1))
    echo "HUNTER_MONITOR_FRESH_CAS_RETRY attempt=$HUNTER_MONITOR_CAS_RETRY_COUNT"
    # Release before preparing/retesting a fresh checkout. A waiting Research
    # process gets a blocking admission opportunity between complete attempts.
    release_lock cas_retry
    # Re-entry inherits cwd. Leave the disposable checkout before removing it;
    # otherwise the next real git clone starts in an unlinked work directory.
    cd "$SOURCE"
    rm -rf "$TASK_DIR"
    exec "$0"
  fi
  exit "$PERSIST_STATUS"
fi
echo "HUNTER_MONITOR_SUCCESS $(date -u +%FT%TZ)"

