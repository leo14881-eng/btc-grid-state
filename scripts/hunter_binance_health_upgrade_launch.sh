#!/usr/bin/env bash
# REVIEW CANDIDATE. Execute only after approval of this exact package and actions.
set -euo pipefail
if [[ "${1:-}" != --approved-collector-upgrade-once || $# != 1 ]]; then
  echo 'PLAN_ONLY: needs exact package/root-supervisor/config/start approval; no actions performed'
  exit 0
fi
[[ "$(id -u)" == 0 && "$(uname -s)" == Linux ]] || { echo 'EXISTING_AUTHORIZED_ROOT_LINUX_TERMINAL_REQUIRED'; exit 1; }
bundle="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
[[ "$bundle" =~ ^/tmp/hunter-binance-health-upgrade-[0-9a-f]{40}$ ]] || { echo 'FIXED_PACKAGE_LOCATION_REQUIRED'; exit 1; }
[[ "$(stat -c '%u:%a' "$bundle")" == '0:700' ]] || { echo 'EXISTING_PACKAGE_OWNER_MODE_INVALID'; exit 1; }
cd -- "$bundle"
sha256sum --check --strict SHA256SUMS
# This checks only current stopped state, guards, immutable code and dependencies.
# Refusal never falls through to systemd-run and never resets the previous attempt.
/usr/bin/python3 -I -B "$bundle/entry.py" --check
systemd-run --quiet --unit=hunter-binance-health-upgrade-33abf19c.service --service-type=exec \
  --property=Restart=no --property=RuntimeMaxSec=1800 --property=TimeoutStopSec=240 \
  --property=KillMode=control-group --property=StandardInput=null \
  --property=StandardOutput=journal --property=StandardError=journal \
  --property="ExecStopPost=/usr/bin/python3 -I -B $bundle/entry.py --cleanup" \
  /usr/bin/python3 -I -B "$bundle/entry.py" --run
echo 'SUPERVISOR_DISPATCHED_NOT_ACCEPTED: keep the bundle and all evidence; do not rerun'
systemctl --no-pager show hunter-binance-health-upgrade-33abf19c.service \
  -p ActiveState -p SubState -p MainPID -p InvocationID -p Result
echo 'Read-only follow-up: journalctl --no-pager -u hunter-binance-health-upgrade-33abf19c.service -n 120'
