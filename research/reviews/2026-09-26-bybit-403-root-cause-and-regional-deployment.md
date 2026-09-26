# Bybit HTTP 403: verified cause and supported production remediation

**Diagnosis (2026-09-26, GitHub Actions run 36234461481):** GitHub x64
ubuntu-latest egress Cloudflare `loc=US, colo=IAD`; GitHub ARM64 ubuntu-24.04-arm
egress `loc=US, colo=ORD`. Both `api.bytick.com` and
`api.bybit.com` returned HTTP 403 with CloudFront body
`The Amazon CloudFront distribution is configured to block access from your country`
for both `/v5/market/time` and `/v5/market/instruments-info?category=spot`.
Bybit's official V5 guide explicitly restricts US and mainland-China source IPs:
https://bybit-exchange.github.io/docs/v5/guide

**This is an egress geo restriction, not an API-key bug or a parameter bug.**
Trying another hostname or GitHub ARM runner does not fix it. Do not use a VPN,
rotating proxy or a different regional Bybit site to evade service restrictions.

## Implemented in the repository

- `research/hunter_cex_scan.py`: GitHub US runner has
  `HUNTER_BYBIT_DIRECT_DISABLED=1`; it never loops futile 403 requests. It
  accepts a fresh complete snapshot only from the official Bybit V5 collector.
  If no valid snapshot exists, `bybit_complete=false`, Binance remains live.
- `research/hunter_bybit_regional.py`: stand-alone collector and strict
  ingestion. Checks independently observed actual country against an explicitly
  configured expected country, obtains the real official active USDT spot
  instrument list + tickers, rejects incomplete/duplicate/non-finite data,
  writes timestamped snapshot and integrity checksum. A checksum is not
  cryptographic source authentication: the authorized repository writer and
  regional machine are the trust boundary.
- Snapshot maximum age: 45 minutes. The main hourly GitHub scan is at minute
  11, so the regional collector should run around minute 03. Stale, partial,
  mismatched or missing snapshots never count as Bybit coverage.
- `scripts/hunter_bybit_regional_publish.sh`: serialized publisher, rebases
  and retries safely, pushes only the Bybit snapshot to main.

## One-time regional infrastructure required

An authorized always-on Linux machine in a jurisdiction where **the machine's
actual egress IP is permitted by Bybit** and the user is eligible to use the
service. Vietnam is an example only if the machine and user's Bybit access are
permitted. This requires infrastructure under the user's control; a GitHub
US-hosted runner cannot be configured in code to have a permitted egress IP.
Avoid adding a self-hosted Actions runner to this public repository: untrusted
workflow execution can create security risks.

On the regional machine:

1. Create a dedicated unprivileged Linux user `hunter`. Install Python 3.12,
   git, curl, and `flock` (util-linux).
2. As `hunter`, clone
   `https://github.com/leo14881-eng/btc-grid-state.git` to
   `/opt/hunter/btc-grid-state`. Ensure the directory is owned by `hunter`.
3. Create a **fine-grained GitHub token** restricted to this one repository
   with **Contents: read and write** only. No exchange API keys or trading
   permissions are required. Do not send the token in chat or commit it.
4. Store a root-owned mode-0600 systemd environment file at
   `/etc/hunter/bybit.env`:
   ```sh
   HUNTER_BYBIT_RUNNER_COUNTRY=VN
   HUNTER_REPO_DIR=/opt/hunter/btc-grid-state
   HUNTER_GITHUB_TOKEN=<set privately on regional host>
   ```
5. Create `/etc/systemd/system/hunter-bybit-regional.service`:
   ```ini
   [Unit]
   Description=Authorized-region official Bybit market data collector
   [Service]
   Type=oneshot
   User=hunter
   EnvironmentFile=/etc/hunter/bybit.env
   WorkingDirectory=/opt/hunter/btc-grid-state
   ExecStart=/usr/bin/bash /opt/hunter/btc-grid-state/scripts/hunter_bybit_regional_publish.sh
   TimeoutStartSec=180
   NoNewPrivileges=true
   PrivateTmp=true
   ```
   Create `/etc/systemd/system/hunter-bybit-regional.timer`:
   ```ini
   [Unit]
   Description=Refresh official Bybit market data every hour
   [Timer]
   OnCalendar=*-*-* *:03:00
   Persistent=true
   Unit=hunter-bybit-regional.service
   [Install]
   WantedBy=timers.target
   ```
   Run `sudo systemctl daemon-reload`,
   `sudo systemctl enable --now hunter-bybit-regional.timer`,
   then `sudo systemctl start hunter-bybit-regional.service`.
6. Check `sudo journalctl -u hunter-bybit-regional.service -n 50` for
   `BYBIT_REGIONAL_SNAPSHOT_PERSISTED`, then check the next GitHub hourly
   `research/results/hunter-health-and-queue.json`:
   `bybit_complete=true` and `snapshot_consistent=true`.
   If the actual region is disallowed, collection fails before contacting
   Bybit. If Bybit returns 403 from that host, choose a properly authorized
   hosting location or request access clarification from Bybit support.

**Do not call the incident resolved until the regional collector is actually
running and a GitHub production scan records `bybit_complete=true`.**
Without regional infrastructure, the source restriction remains an explicit
external blocker; no automated code patch can change a server's country.
