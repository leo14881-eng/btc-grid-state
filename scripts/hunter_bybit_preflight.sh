#!/usr/bin/env bash
# Read-only host checks. No account, permission, service or state mutations.
set -euo pipefail
failed=0
check() { if "$@"; then return 0; else echo "PREFLIGHT_BLOCKED $*"; failed=1; fi; }
check test "$(uname -s)" = Linux
check getent passwd hunter-bybit-stream
check getent group hunter-bybit-data
for path in /opt/hunter-bybit-stream /opt/hunter-bybit-stream/venv /var/lib/hunter-bybit-stream; do
  check test -d "$path"
  if [ -e "$path" ]; then stat -c '%n owner=%U group=%G mode=%a' "$path"; fi
done
if [ -x /opt/hunter-bybit-stream/venv/bin/python ]; then
  check /opt/hunter-bybit-stream/venv/bin/python -B -c 'import sys,sqlite3,importlib.metadata as m; print(sys.version,sqlite3.sqlite_version,m.version("websockets")); assert sys.version_info >= (3,10); assert m.version("websockets")=="15.0.1"'
else
  echo 'PREFLIGHT_BLOCKED preprovisioned venv with pinned dependency required'; failed=1
fi
systemctl show hunter-discovery.service hunter-research.service hunter-bybit-stream.service \
  -p Id -p LoadState -p ActiveState -p User -p Group -p SupplementaryGroups \
  -p DynamicUser -p ProtectSystem -p ReadWritePaths -p FragmentPath
timedatectl show -p NTPSynchronized
df -h /var/lib /opt
df -i /var/lib /opt
echo 'PREFLIGHT_REVIEW: actual job users need shared SQLite/journal/request-lock write access.'
echo 'PREFLIGHT_REVIEW: existing denial/lease/boot state must be preserved; no reset is authorized here.'
echo 'PREFLIGHT_REVIEW: output is inventory, not approval or a production acceptance result.'
exit "$failed"
