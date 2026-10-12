# Bybit Kline server deployment candidate — not installed or enabled

Baseline: `9b60d9f867e4b9f9324fce3cf92c6ab1012cbf87` (2026-10-12).
Use the draft PR's exact tested head for release review. Do not merge/enable as
part of these offline checks. No PR35/98/111/112, Sentinel, portfolio, historical
ledger, capital allocation, strategy threshold, CAS, generation or monitor
schedule is changed. V2 stays 20000 / 17000 / 3000, SHADOW ONLY, real=false,
capital authority NONE_SHADOW_ONLY.

## Verified gap and patch boundary

07:17–07:21 UTC coin-server Ops logs show `hunter-market-stream.service` with
Binance 9 quote/trade subscriptions, Bybit NOT_REQUIRED/0. Its Fast Watch feed
is not Kline collection. The pinned main snapshot captured at 07:19:30 UTC still
reports `OFFICIAL_BYBIT_V5_VIA_WORKER` and 8 Kline batches. 141 successful symbols
does not establish migration. PR101 is merged but its direct bundle is default
off. This candidate adds the missing service/dependency, explicit state init,
full roster receipt, source audit, frozen-read acceptance and workflow routing.

Existing PR101 code already provides bounded REST initialization/repair (25 x
15m, 5 x 60m), shared >=200ms / <=4 in-flight admission, cancellable Linux child
processes, durable 403/orphan stops, ACK/ping/topic freshness and five reconnect
attempts with 5/10/20/40s backoff. These are retained. The collector now opens
existing state only; a missing DB cannot recreate a fresh budget. A missing
historical candle-close confirmation fails frozen reads and packet validation;
after 90s grace it is a repairable gap. The current forming candle is retained
in the existing formulas. No Worker fallback is added.

The two workflow call sites now go through a small route wrapper. By default,
Discovery still sets DIRECT_DISABLED=1 and uses Worker. Only the explicit
SERVER_DIRECT route, all three matching selectors, Linux, an isolated server
job and no GITHUB_ACTIONS=true can select the installed cache. The same existing
modules, result filenames, publishers and CAS/readback execute. A denied or
missing cache remains UNKNOWN; the wrapper never retries via Worker.

## Actual consumer intervals (do not infer these from quote subscriptions)

| Venue / consumer | Actual input | This candidate |
| --- | --- | --- |
| Bybit-only Discovery signals and BTC benchmark | 25 x 15m, 5 x 60m; 1h uses last 2 hourly rows and 4h uses all 5 | Full captured roster + BTC, both subscriptions per symbol |
| Binance early signals (`hunter_early_signals.rolling/micro`) | 25 x 15m, 5 x 1h; no native 4h subscription needed | Existing REST remains; WS cache consumer is **not implemented** |
| Binance systemic risk (`hunter_tail_risk._btc_short_returns`) | BTCUSDT 5m, latest 16 rows filtered to completed bars | Existing REST remains; 5m WS consumer is **not implemented** |
| Binance opportunity backfill (`hunter_shadow_trader_v2._fetch_binance_kline_page`) | Bounded historical 5m pages to position-specific cutoffs | Retain REST history; a current rolling cache cannot replace historical coverage |
| Binance tail/leading replay and blind replay | Historical 5m windows / monthly archives | Retain historical REST/archive transport; do not touch PR112 files |

Thus **all-crypto WS migration is incomplete**: Binance needs reviewed 15m/1h
full signal-roster subscriptions and BTC 5m with its own venue-matched cache,
budget and consumers. Do not subscribe meaningless 4h/1d bars, label quote
health as Kline readiness, or activate a blanket “migration complete” gate.
Bybit directories/tickers, held-position depth and historical initialization
remain HTTP where required. Alpha and held-position requests still use their
existing paths and are outside PR101's cooperative public-spot budget. This is
not an all-IP quota guarantee or complete elimination of every Worker endpoint.

Protocol references checked 2026-10-12: [Bybit Kline confirm semantics](https://bybit-exchange.github.io/docs/v5/websocket/public/kline),
[Binance Kline streams](https://developers.binance.com/docs/binance-spot-api-docs/web-socket-streams).

## Required host review — actual blocker

Current coin-server Ops tools expose only read-only status, logs and approved
runtime files. They provide no installation/configuration write operation.
Previously denied SSH/WSL and stock-unit/runtime paths were not retried or
bypassed. The actual Discovery/Research service user, unit hardening, Python
dependencies, write access and complete service inventory are not verified.
No command below was run on the server.

The proposed permissions are a dedicated static system user, a shared group,
root-owned immutable code/venv, and group write access to this one state
directory. Metadata clients need SQLite/journal/request-lock writes, not just
read access. Review these rights and actual existing unit users before install.
No automatic chmod/chown of existing state, broad ACL, credential change or
reset of denial/budget/leases is permitted. Ensure NTP, disk/inodes, Python
>=3.10, SQLite and all same-IP traffic are suitable. Keep boot-change/legacy DB
stops for explicit operator review; do not delete the DB to recover.

## Fresh installation only (for a later approved write session)

Run from an authorized Linux admin session. Substitute the reviewed 40-character
PR head; it must be available locally. These commands intentionally stop if a
deployment or account already exists; review an upgrade separately.

```bash
set -euo pipefail
REPO=/opt/shadow-runner/btc-grid-state
RELEASE_SHA=REPLACE_WITH_EXACT_TESTED_40_CHARACTER_PR_HEAD
test "$(git -C "$REPO" rev-parse "$RELEASE_SHA^{commit}")" = "$RELEASE_SHA"
test ! -e /opt/hunter-bybit-stream
test ! -e /var/lib/hunter-bybit-stream
sudo groupadd --system hunter-bybit-data
sudo useradd --system --gid hunter-bybit-data --no-create-home --shell /usr/sbin/nologin hunter-bybit-stream
sudo install -d -o root -g root -m 0755 /opt/hunter-bybit-stream /opt/hunter-bybit-stream/code
git -C "$REPO" archive "$RELEASE_SHA" | sudo tar -x -C /opt/hunter-bybit-stream/code
printf '%s\n' "$RELEASE_SHA" | sudo tee /opt/hunter-bybit-stream/release-head >/dev/null
sudo chmod 0644 /opt/hunter-bybit-stream/release-head
sudo python3 -m venv /opt/hunter-bybit-stream/venv
sudo /opt/hunter-bybit-stream/venv/bin/python -m pip install -r /opt/hunter-bybit-stream/code/deploy/hunter-bybit-stream-requirements.txt
sudo install -d -o hunter-bybit-stream -g hunter-bybit-data -m 2770 /var/lib/hunter-bybit-stream
cd /opt/hunter-bybit-stream/code
sudo -u hunter-bybit-stream sh -c 'umask 0007; /opt/hunter-bybit-stream/venv/bin/python -B -m scripts.hunter_bybit_stream_ops initialize'
sudo install -o root -g root -m 0644 deploy/systemd/hunter-bybit-stream.service /etc/systemd/system/hunter-bybit-stream.service
sudo systemd-analyze verify /etc/systemd/system/hunter-bybit-stream.service
sudo systemctl daemon-reload
```

The code archive includes repository readback files but the unit cannot write
them. The collector has no strategy/job invocation or exchange credentials.
Verify the installed code is the archive for RELEASE_SHA and retain pip freeze,
unit inspection and release-head alongside the review evidence.

## Capture full roster without running Discovery or writing a ledger

Use a **fresh existing natural-cycle** `origin/main` snapshot, not a manually
rerun strategy. Readback head must be recorded separately from release head.
The manifest validator checks the snapshot checksum, source, expected bases
and <=900s age. Failure is a stop, not permission to relax age or use another
source. If no fresh natural snapshot exists, wait for the normal cycle.

```bash
git -C "$REPO" fetch origin main
SNAPSHOT_SHA=$(git -C "$REPO" rev-parse origin/main)
SNAPSHOT_FILE=$(mktemp /tmp/hunter-bybit-readback.XXXXXX.json)
git -C "$REPO" show "$SNAPSHOT_SHA:research/results/hunter-bybit-worker-snapshot.json" >"$SNAPSHOT_FILE"
chmod 0644 "$SNAPSHOT_FILE"
cd /opt/hunter-bybit-stream/code
sudo -u hunter-bybit-stream sh -c 'umask 0007; exec /opt/hunter-bybit-stream/venv/bin/python -B -m scripts.hunter_bybit_stream_ops prepare --snapshot "$1" --source-head "$2"' sh "$SNAPSHOT_FILE" "$SNAPSHOT_SHA"
/opt/hunter-bybit-stream/venv/bin/python -B -m research.hunter_bybit_stream --manifest /var/lib/hunter-bybit-stream/manifest.json
```

The last plan command must be run as a state-reading account (use
`sudo -u hunter-bybit-stream` if the admin shell lacks group read access).
`prepare` holds collector.lock, archives the full source snapshot and hashed
roster receipt, then atomically adopts manifest.json. It refuses while the
collector owns the lock. Stale manifests block restart. A roster change requires
stop, fresh validated prepare and reviewed restart; no silent hot adoption.
Same-roster natural snapshots need not share the manifest hash: topic digest
binds scope; each generation retains the original manifest receipt hash.

## Observation and source switch are separate review steps

After installation/market observation approval, start **only** the new sidecar:

```bash
sudo systemctl start hunter-bybit-stream.service
sudo systemctl status --no-pager hunter-bybit-stream.service
sudo journalctl -u hunter-bybit-stream.service -n 30 --no-pager
```

No timer or automatic restart is installed. systemd supervises the process
group, sends SIGTERM and allows 45s before SIGKILL. Python cancels/reaps repair
children and marks the session disconnected; interrupted requests retain their
durable stop. An exhausted five-attempt connection budget leaves the service
failed, rather than resetting the budget via Restart=always. Preserve state
and logs on failure. Do not automate `reset-failed`/restart or boot-gate resets.

Refresh SNAPSHOT_FILE/SNAPSHOT_SHA using the previous readback commands whenever
the file is >900s old. Local acceptance is read-only:

```bash
sudo -u hunter-bybit-stream /opt/hunter-bybit-stream/venv/bin/python -B -m scripts.hunter_bybit_stream_ops check --snapshot "$SNAPSHOT_FILE"
```

Require READY at full roster, source/receive ages <=90s at generation cut,
read completion <=90s after cut, current candles in the current interval,
contiguous history, confirmed closed bars, no missing ACK/gap/halt/cooldown,
and matching source/manifest/session receipts. The existing cut/read contract
can be 180s end-to-end; this does not claim 90s freshness at publication.
Observe natural 15m/hour rollover, sustained readiness and real cold/warm
latency. No offline fixture establishes French-server throughput or geography.

Only after that evidence and a reviewed source-switch decision, install the
example drop-in for the existing Discovery and Research units. Inspect their
actual users, hardening and inherited environment first; verify both can access
the same DB/journals/locks. Do not change the monitor unit or start any strategy
job manually. A later `daemon-reload` affects subsequent natural timer cycles.

```bash
sudo install -d /etc/systemd/system/hunter-discovery.service.d /etc/systemd/system/hunter-research.service.d
sudo install -m 0644 deploy/systemd/hunter-bybit-server-direct.conf.example /etc/systemd/system/hunter-discovery.service.d/40-bybit-source.conf
sudo install -m 0644 deploy/systemd/hunter-bybit-server-direct.conf.example /etc/systemd/system/hunter-research.service.d/40-bybit-source.conf
sudo systemctl daemon-reload
```

Do not do this before the wrapper changes are merged into the job runner's
latest-main source; the collector release alone cannot change existing jobs.
Exact tested deployment code and natural-job source head may differ and must
both be recorded. Review intervening main changes; do not overwrite them.

## Publication acceptance and stop

For subsequent natural cycles retain the exact main readback SHA, generation,
job source SHA and source fields from the existing output:

- snapshot schema `hunter_bybit_direct_snapshot_v1`, source
  `OFFICIAL_BYBIT_V5_SPOT_DIRECT_CACHE`; Kline source `OFFICIAL_BYBIT_V5_SPOT_CACHE`;
- `kline_batch_requests=0`, full expected bases + BTC, signal_complete=true,
  cache_generation READY, its manifest/roster/session/cut/window receipts;
- direct metadata receipt hashes and actual source/receive timestamps;
- existing generation/CAS/main-readback verification and unchanged capital,
  real=false, 5-minute Monitor, dedup and SingleWriter behavior.

Use natural logs to confirm no `/bybit/early-klines` Worker requests. A local
READY check prints publication_verified=false deliberately. Partial/stale data
must remain UNKNOWN without fallback; 141/141 alone is insufficient.

Stop with `sudo systemctl stop hunter-bybit-stream.service`, inspect process
group termination and retained DB/leases/logs. Do not erase state. Disabling a
selected collector leaves explicit UNKNOWN for its consumers. Returning to
Worker is a separately reviewed generation-boundary source decision, never an
automatic recovery or workaround for a country/unknown403 stop.
