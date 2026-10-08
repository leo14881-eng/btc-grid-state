#!/usr/bin/env bash
set -euo pipefail
CAS_RETRY_COUNT=${HUNTER_MONITOR_CAS_RETRY_COUNT:-0}
[[ "$CAS_RETRY_COUNT" =~ ^[012]$ ]] || { echo INVALID_MONITOR_CAS_RETRY_COUNT; exit 1; }
# One host lock; every generation gets an isolated checkout. Never reset the
# running production checkout or share its working tree with another writer.
exec 9>/run/lock/hunter-position-monitor.lock
flock -w 120 9 || { echo PORTFOLIO_LOCK_WAIT_TIMEOUT; exit 1; }
SOURCE=/opt/shadow-runner/btc-grid-state
TASK_DIR=$(mktemp -d /opt/shadow-runner/monitor-generation.XXXXXX)
trap 'rm -rf "$TASK_DIR"' EXIT
REMOTE=$(git -C "$SOURCE" remote get-url origin)
git clone --quiet --shared "$SOURCE" "$TASK_DIR/repo"
cd "$TASK_DIR/repo"
git remote set-url origin "$REMOTE"
git fetch origin main
git checkout --detach origin/main
export PYTHONDONTWRITEBYTECODE=1
BASE_SHA=$(git rev-parse HEAD)
STARTED_AT=$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)
GATE=$(python3 -m research.hunter_scheduler_health gate)
echo "$GATE"
PROCESS=$(python3 -c 'import json,sys; print(str(json.load(sys.stdin)["process"]).lower())' <<<"$GATE")
GENERATION=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["generation_id"])' <<<"$GATE")
[ "$PROCESS" = true ] || { echo "ALREADY_PROCESSED generation=$GENERATION"; exit 0; }
if ! python3 -m unittest tests.test_hunter_shadow_trader_v2 tests.test_hunter_position_monitor tests.test_hunter_tail_risk tests.test_hunter_leading_risk tests.test_hunter_scheduler_health tests.test_hunter_monitor_persist > "$TASK_DIR/regression.log" 2>&1; then
  cat "$TASK_DIR/regression.log"
  exit 1
fi
tail -n 4 "$TASK_DIR/regression.log"
python3 -m research.hunter_position_monitor
# Preserve the generation admitted at the gate, even across a time boundary.
python3 -m research.hunter_scheduler_health success --started-at "$STARTED_AT" --trigger-source VULTR_SYSTEMD --state-revision "$BASE_SHA"
python3 - "$GENERATION" <<'PY'
import json, pathlib, sys
p=pathlib.Path('research/results/hunter-scheduler-health.json')
h=json.loads(p.read_text())
h['current_generation_id']=h['last_successful_monitor_generation_id']=sys.argv[1]
p.write_text(json.dumps(h,indent=2,sort_keys=True)+'\n')
PY
python3 scripts/hunter_monitor_persist.py validate --generation "$GENERATION"
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
    # Re-entry inherits cwd. Leave the disposable checkout before removing it;
    # otherwise the next real git clone starts in an unlinked work directory.
    cd "$SOURCE"
    rm -rf "$TASK_DIR"
    exec "$0"
  fi
  exit "$PERSIST_STATUS"
fi
echo "HUNTER_MONITOR_SUCCESS $(date -u +%FT%TZ)"
