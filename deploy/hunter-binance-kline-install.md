# Binance native Kline cache — staged, shadow only

No server installation, account/permission change, service start, consumer switch,
or production acceptance is claimed by this patch. The current coin-server Ops
connector is read-only. Do not retry denied SSH, WSL or stock runtime paths.
Use only an authorized maintenance entry. Independent review and explicit merge /
enable instructions are required. Do not execute a trading or strategy workflow
to manufacture acceptance evidence.

## Exact consumers and preserved semantics

| Consumer | Native interval / retained rows | Semantics |
| --- | --- | --- |
| early signals and BTC benchmark | 1h / 5 (2 for 1h return) | original 1h/4h return formulas include current candle; no native 4h subscription |
| early micro signals and BTC benchmark | 15m / 25 | original volume, compression, momentum and range formulas |
| systemic risk BTC short returns | BTCUSDT 5m / 16 | original last 13 completed closes; current candle excluded |
| rotating forward research | 1d / up to 35 | original minimum 15 candles, last forming candle excluded from completed-day aggregates |

Every Binance pair in a fresh complete authoritative CEX scan receives 15m, 1h
and 1d subscriptions; only BTC needs live 5m. The pinned scan used in development
contained 409 pairs including two non-ASCII names: 1,228 topics / 3 sockets.
These are captured counts, not live deployment results. Fewer than 25 quarters or
5 hourly candles stay UNKNOWN. Newly listed daily series keep the existing 15..35
acceptance. Missing interior candles are never mistaken for a short listing age.

REST remains for directory, tickers, order books, V2 position/checkpoint 5m history,
tail/leading replay 5m history, missed-opportunity 15m day history, and monthly 1d
archives. Those historical consumers do not fit the bounded live windows and are
unchanged here, including PR112 files. This patch does not claim all Binance REST
is governed by the new collector's budget; inspect shared-IP headroom before enable.

## Runtime contract

- Fixed public endpoints: `wss://data-stream.binance.vision/ws` and
  `https://data-api.binance.vision/api/v3/klines`. No credentials, proxies, redirects,
  alternate hosts or Worker fallback. Default consumers remain `REST`.
- Up to 500 topics per socket, 8 sockets maximum; 100 topics per SUBSCRIBE, one
  every 500 ms. websockets answers server PING with matching PONG. Every ACK is
  bound to request and socket. Planned reconnect before the 24-hour boundary.
- Fixed, separate `/var/lib/hunter-binance-stream/state.sqlite3`; collector lock,
  persistent global denial/cooldown/orphan lease evidence and four request slots.
  Each Kline request weighs 2, with >=200 ms start spacing. Recovery's shared
  deadline is 900 s; three bounded HTTP attempts within the request deadline.
  This is a conservative collector ceiling, not measured whole-IP quota headroom.
- Supervised HTTP subprocess includes DNS/TLS/headers/body in its deadline and
  is killed/reaped before release. Lost outcomes remain a durable global stop.
  Both 403 and 418 stop; 429/5xx use bounded cooldown. No denial-state reset.
- Five consecutive WS failures exhaust the circuit; 5/10/20/40 s backoff. Only
  READY samples spanning five minutes with no observed interruption restore its
  budget. Every observed topic gap/staleness, cooldown or session event resets it;
  sampling does not prove absolute continuous health. Restart=no;
  systemd control-group termination covers WS threads and HTTP children.
- A disconnected, stale, unconfirmed closed, gapped, future, mixed-epoch or
  mismatched roster read is UNKNOWN. Frozen reads recheck the 90 s allowance at
  exit. Current candles require fresh WS exchange `E` time and receipt time.
  REST has no event timestamp: its source clock is explicitly
  `LOCAL_REQUEST_CUTOFF`, never relabelled as exchange time.
- Native rows, close confirmations, epoch, immutable manifest/source SHA,
  full-roster hash, content hashes and frozen generation bindings are retained.
  Consumers call the original formulas. No additional result writer is added.
  Journal events are local audit, not an installed external alerting service.
- A bounded queue drains at most 100 observations per SQLite transaction, keeping
  their original receipt timestamps. ACKs are applied in order; bad batches roll
  back. Queue saturation stops the session rather than silently dropping bars.

Protocol references: [official WebSocket streams](https://developers.binance.com/en/docs/products/spot/web-socket-streams)
and [official market data REST](https://developers.binance.com/en/docs/products/spot/rest-api/market-data-endpoints).

## Preflight, staging and initialization

Run only through an approved writable maintenance entry. New account/group,
ownership/mode, package installation and consumer sandbox changes require
action-time authorization; general deployment approval does not run them here.
Do not use `sudo` to bypass an unavailable entry. Preconditions:

1. Existing static user `hunter-binance-stream`, group `hunter-binance-data`.
2. `/opt/hunter-binance-stream` and `venv` owned by root and not group/world
   writable; Python >=3.10 and pinned `websockets==15.0.1` preinstalled.
3. `/var/lib/hunter-binance-stream` owned by that user/group, mode 2770; actual
   consumer users and service hardening reviewed for SQLite/journal/lock access.
4. No existing destination code, release head or unit. Upgrade/replacement is
   deliberately refused; preserve any existing state and obtain a reviewed plan.

```bash
# Read-only. Missing prerequisites are a blocker, not automatically repaired.
bash scripts/hunter_binance_preflight.sh

# SOURCE_REPO and EXACT_REVIEWED_SHA are the reviewed repository and full SHA.
# Stage only with the already-authorized maintenance identity.
bash scripts/hunter_binance_stage.sh "$SOURCE_REPO" "$EXACT_REVIEWED_SHA"
```

Staging copies an exact Git archive, writes release-head, copies/verifies the unit.
It does not install packages, change accounts/permissions, reload systemd, create
state or start anything. After host review and explicit enable authorization:

```bash
cd /opt/hunter-binance-stream/code
PY=/opt/hunter-binance-stream/venv/bin/python
# Execute these as the existing collector identity through the approved entry.
# initialize refuses an existing DB and cannot reset a prior denial/boot state.
"$PY" -B -m scripts.hunter_binance_stream_ops initialize
"$PY" -B -m scripts.hunter_binance_stream_ops prepare \
  --snapshot "$FRESH_CEX_SCAN" --source-head "$EXACT_REVIEWED_SHA"
"$PY" -B -m research.hunter_binance_stream \
  --manifest /var/lib/hunter-binance-stream/manifest.json
```

The preparation archives the complete scan and immutable source/roster receipt.
The plan command above makes no market call. Fresh scan age must be <=30 minutes.
Manifest adoption requires a stopped collector and reviewed restart; it never
changes scope under an active generation. Then, from the approved maintenance
identity, the explicitly authorized commands are:

```bash
systemctl daemon-reload
systemctl start hunter-binance-stream.service
systemctl show hunter-binance-stream.service -p ActiveState -p SubState -p ExecMainStatus -p MainPID
journalctl -u hunter-binance-stream.service --since '-20 minutes' --no-pager
```

No automatic `enable` is supplied. Do not restart automatically after a circuit,
403/418, orphan, boot change or storage error. Preserve DB, locks and logs for review.

## Acceptance before consumer selection

```bash
cd /opt/hunter-binance-stream/code
/opt/hunter-binance-stream/venv/bin/python -B -m scripts.hunter_binance_stream_ops check \
  --snapshot "$FRESH_CEX_SCAN"
```

Require READY with the expected complete roster, source head/manifest hash and
zero missing closed candles; repeat across a natural 5m/15m/hour boundary and
observe a native 1d event. Measure whole-roster read latency and sustained queue /
SQLite load on this server; no throughput SLA is claimed by offline tests. A
newly listed coin without sufficient history remains explicit UNKNOWN and cannot
be silently removed from the roster or replaced by a different source.

Only after that evidence, review the example consumer drop-in for the actual
Discovery, research and `hunter-position-monitor.service` identities. Set
`HUNTER_BINANCE_KLINE_SOURCE=SPOT_CACHE` and `HUNTER_SERVER_ISOLATED_CHECKOUT=1`
in their existing approved configuration. Do not change schedules or trigger jobs.
Observe the next natural cycles: early signal packets all use
`OFFICIAL_BINANCE_SPOT_CACHE` with matching asset/BTC binding; daily observations
carry `market_structure_source`; BTC risk carries `source_provenance`. The check
tool deliberately reports `publication_verified=false`; only those published
natural-cycle receipts establish consumer migration. Stale/error must propagate
without REST/Worker fallback. Keep SHADOW ONLY, real=false, NONE_SHADOW_ONLY,
V2 20000/17000/3000, SingleWriter/CAS/generation, dedup and 5-minute Monitor.

Rollback is an explicitly reviewed selector change to the previous REST route
and a service stop, preserving all cache/audit state; never an automatic failure
fallback. No historical ledger repair/rewrite, Sentinel change, or PR35/98 merge
is part of these steps.
