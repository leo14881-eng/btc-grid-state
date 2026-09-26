#!/usr/bin/env bash
# Install on an authorized regional Linux host, NOT on geo-blocked GitHub US runners.
set -euo pipefail
umask 077
: "${HUNTER_GITHUB_TOKEN:?Fine-grained repo Contents write token is required}"
: "${HUNTER_BYBIT_RUNNER_COUNTRY:?Set expected real egress country, e.g. VN}"
cd "${HUNTER_REPO_DIR:-/opt/hunter/btc-grid-state}"
[[ "$(git remote get-url origin)" == "https://github.com/leo14881-eng/btc-grid-state.git" ]] || {
  echo "UNEXPECTED_REPO_REMOTE";exit 1;
}
exec 9>/tmp/hunter-bybit-regional-publish.lock
flock -n 9 || { echo "COLLECTOR_ALREADY_RUNNING";exit 0; }
askpass="$(mktemp)"
trap 'rm -f "$askpass"' EXIT
cat >"$askpass" <<'ASKPASS'
#!/bin/sh
case "$1" in
  *Username*) printf '%s\n' x-access-token ;;
  *Password*) printf '%s\n' "$HUNTER_GITHUB_TOKEN" ;;
  *) exit 1 ;;
esac
ASKPASS
chmod 700 "$askpass"
export GIT_ASKPASS="$askpass" GIT_TERMINAL_PROMPT=0
git fetch origin main
git checkout -B main origin/main
python3 research/hunter_bybit_regional.py
git config user.name "hunter-regional-collector"
git config user.email "hunter-regional-collector@users.noreply.github.com"
git add research/results/hunter-bybit-regional-snapshot.json
git commit -m "research: fresh official Bybit authorized-region snapshot"
for attempt in 1 2 3 4 5; do
  git fetch origin main
  if ! git rebase origin/main; then
    git rebase --abort || true
    echo "REGIONAL_SNAPSHOT_REBASE_CONFLICT";exit 1
  fi
  if git push origin HEAD:main; then
    echo "BYBIT_REGIONAL_SNAPSHOT_PERSISTED attempt=$attempt"
    exit 0
  fi
  sleep "$((attempt * 2))"
done
echo "BYBIT_REGIONAL_SNAPSHOT_PERSISTENCE_FAILED"
exit 1
