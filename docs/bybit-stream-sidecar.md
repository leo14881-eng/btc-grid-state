# Bybit spot Kline sidecar: review stage only

This PR adds an opt-in sidecar and connects the production discovery consumer to
its read-only cache behind `HUNTER_BYBIT_KLINE_SOURCE=SPOT_CACHE`. The default is
`WORKER`; no deployment configuration enables the new selector. It installs no
service, alters no timer, and changes no capital/strategy rules, ledgers, Worker
JavaScript or Sentinel files. No market requests were made to develop or test
it. An approved code PR is not a deployment approval.

## Scope and protocol

The local roster comes from the existing checksum-validated, fresh Worker
snapshot's `venue_status.signal_expected_bases`, plus BTC. This is the existing
Bybit-only Kline collection set, not an expansion to every exchange symbol.
The immutable manifest digest follows every cache read. Roster changes require
an explicit restart with a newly validated manifest; no discovery runs here.

Only `wss://stream.bybit.com/v5/public/spot` is allowed. Subscribe to
`kline.15.SYMBOL` and `kline.60.SYMBOL`, at most ten args per message; reject a
connection whose serialized total args exceed 21,000 characters. JSON ping is
sent every 20 seconds. ACKs have session-specific IDs. Missing/negative ACKs,
disconnects and per-topic staleness fail closed. Pong does not refresh a topic.
WS `confirm=false` and `confirm=true` remain explicit in stored provenance.

REST bootstrap/repair uses only `https://api.bybit.com/v5/market/kline`, with
`category=spot`, one `symbol`, `interval=15|60`, `limit=25|5`, and current `end`
in milliseconds. No `start` is needed to replace the full bounded rolling
window. Long outages restore only that window, not every missed generation.
No implicit environment proxy, redirect, alternate host, or Worker fallback.

## Cache contract

SQLite stores per-topic ACK, receive/source times, gap status and bar revisions.
REST must validate all 25/5 consecutive rows.
WS updates cannot reopen a confirmed bar or replace a newer observation.
Duplicate packets do not refresh freshness. Reconnect invalidates all ACKs and
requires both a repaired window and a new WS observation per topic. Historical
REST confirmation is marked `REST_INFERRED`, never claimed as a WS confirmation.

`Store.reader().freeze(topics, generation_end_ms, now_ms)` reads ALL selected
symbols and BTC in one SQLite transaction. Both source and receive times must
be <= the cut. It uses a 180-second revision horizon with one older baseline per
bar, a maximum of 256 revisions per bar, 256 readiness events per topic, and a
rolling 25/5 + 3 bar range. Eviction, missing history, stale/current bar, reconnect,
roster drift, global cooldown/halt or any incomplete topic makes the entire
generation UNKNOWN with no windows. A usable generation is at most 90 seconds
old when frozen. Later revisions cannot backfill or overwrite that cut. Frozen
rows are copied into memory before concurrent feature extraction.

The stable digest of the sorted topic roster is checked against the current
Bybit-only assets + BTC on every consumption. The original manifest receipt
hash remains separate evidence: a new ticker generation need not have the same
receipt hash as the earlier manifest. A changed asset set requires a reviewed
manifest refresh/restart; it is never silently subtracted from coverage.

The single-topic `window()` API remains for isolated diagnostics, using latest
values and rejecting later observations. Production uses only `freeze()`.
Both APIs include the forming bar without changing feature formulas.
The normal close-then-open rollover is allowed without a forced REST repair:
while the next forming candle has not arrived, reads are UNKNOWN; an interior
missing candle still marks a transport gap. Missing/corrupt DB reads are UNKNOWN
and never create a replacement DB. A late 403 still persists the global stop
even if its response arrived after the recovery's data deadline.

Per-bar sources are `OFFICIAL_BYBIT_V5_SPOT_WS` / `OFFICIAL_BYBIT_V5_SPOT_REST`.
The new snapshot uses schema `hunter_bybit_stream_snapshot_v1` and source
`OFFICIAL_BYBIT_V5_SPOT_CACHE`; ticker/listing sources remain explicitly
`OFFICIAL_BYBIT_V5_VIA_WORKER`. Cache selection requires the existing Worker
market route for those two inputs and sends ZERO Worker Kline requests. Unknown
selector values fail before any route is called; cache failure never falls back.

The unchanged discovery publisher writes the same `hunter-bybit-worker-snapshot.json`
artifact, through the existing runner output list, single writer, CAS and readback.
The filename is retained for that publication contract; its source/schema are
explicit and strictly validated. No sidecar writes repo results or ledgers.
The early-signals reader checks source, snapshot checksum and generation receipt.
UNKNOWN retains valid ticker/listing coverage with zero signals and explicit
failures for all expected symbols, so downstream reports show unscored assets.

All six asset/BTC provenance packets carry the same generation hash, roster hash,
manifest receipt and session. Their real receive/source times and WS confirmation
are preserved; reading an old unconfirmed bar later never confirms it. The
historical trend reader validates these packets and rejects missing confirmation
instead of inventing history. Existing REST sources keep their existing behavior.

## Shared budget and failure behavior

Live CLI fixes state at `/var/lib/hunter-bybit-stream/state.sqlite3`, with a
host-wide `collector.lock` alongside it. There is no CLI/env per-checkout budget
override. Directory provisioning and permissions are a later deployment step;
the code does not create the directory or grant privileges. SQLite transactions
admit at most one request every 200ms and four outstanding leases, shared across
processes/checkouts. Initialization, retries and repairs all acquire a lease.
Unrelated legacy programs do not use this governor: before rollout, inventory
other same-IP API traffic. This is a cooperative limit, not an IP firewall.

403 containing `access too frequent` persists a >=600-second global cooldown.
Country or unclassified 403 persists a halt requiring manual review. No automatic
clear, fallback domain or proxy is offered. HTTP 429, 5xx, timeouts and API quota
errors get at most three attempts per topic per session, through the same budget.
At most five WS connection attempts are allowed per invocation with 5/10/20/40s
backoff. Permanent denials survive process restart. Leases deliberately do not
expire: a stalled process must not open a fifth request. Crash recovery may need
reviewed cleanup after proving no old request remains; deleting the DB to evade
cooldown is not a supported workflow.

For 141 assets plus BTC: 284 topics/REST bootstrap calls, 29 subscription messages,
an admission-only lower bound of 56.6 seconds at 5rps. Four in-flight requests
with 10-second latency require at least 710 seconds even without retries, so a
600-second recovery budget cannot guarantee a cold start under that latency.
The sidecar is continuous, outside the scan's critical path; deadline failures
leave UNKNOWN. The 600s limit gates request admission and data acceptance, not
a hard wall-clock process timeout: urllib's 10s timeout is per blocking I/O and
a slowly streaming response can take longer. An in-flight request must finish
before its held lease is released; cancellation prevents further retries and
late publication, but never pretends that a socket stopped before it did.
Offline rate tests are not a French-server throughput measurement.
A production switch requires observed cold/warm timings within the actual scan
deadline; this PR makes no such claim.

## Staged acceptance

1. Offline: exact 25/5 fixture rows and existing features; forming/closed bars,
   source tags, no future-generation refill, invalid bars/ACKs/duplicates,
   disconnect/restart recovery, heartbeat vs topic freshness.
2. Offline: six actual processes in different working directories contend for
   the same SQLite budget, with >=200ms spacing and <=4 held requests. Verify
   persisted 403 cooldown/halt, retry exhaustion, clock rollback and stranded
   leases. The guarded runner rejects sockets/DNS/exec and writes outside its
   temporary directory; forked budget workers inherit the guard.
3. Later, separately authorized: preprovision shared directory and the pinned
   optional `websockets==15.0.1` dependency, run a bounded server-side observation,
   compare natural same-generation windows/source chains and startup timing.
4. Offline consumer acceptance in this PR: multi-symbol revisions at the cut,
   concurrent reconnect snapshot isolation, bounded history eviction, roster/source
   drift, BTC or asset gaps, strict provenance and unchanged numerical features.
   Only after review and a natural-cycle comparison consider source deployment.

Local planning only (no network):
`python -m research.hunter_bybit_stream --manifest /absolute/captured-snapshot.json`

Live `--collect` is intentionally not invoked by CI or this change. There are no
service/timer templates in this first PR. Test command:
`python -I -B -X utf8 scripts/check_bybit_stream_offline.py` (Linux).

## Activation proposal (not executed)

The code integration is present but OFF. The following gates must be reviewed
before activation; none are granted by passing offline CI alone.

1. Freeze reviewed code SHA and capture the current authoritative roster hash.
   Recheck exact scope on the server immediately before startup. Do not take a
   stale repository checkout's snapshot as the current Universe. The code's
   15-minute manifest age check will reject it. Inventory other programs sharing
   the same egress/API budget; they are not automatically included in this DB.
2. Through an existing authorized server execution channel, verify the runtime
   account, actual launcher, filesystem access, time sync, dependency version,
   directory location and available disk. Provision only after approval, outside
   all Git checkouts. Pin `websockets==15.0.1`; no secrets or API keys are needed.
   Current available server tools are READ_ONLY and cannot install/start this
   process. A previous launcher-path refusal must not be bypassed.
3. A separately approved shadow observation starts only the sidecar, with a
   singleton lock and no auto-restart storm. Require all selected topics' ACKs,
   independent fresh timestamps, contiguous 25/5 windows and explicit source
   tags. Measure actual cold/warm timing and disk writes across a 15m and a 60m
   boundary and one controlled reconnect. An UNKNOWN or denied topic must not
   become a valid signal. No strategy is manually invoked for this acceptance.
4. Review this PR's consumer selector, atomic generation freeze, revision history,
   source validation and UNKNOWN propagation with the deployed runner. Complete
   natural-cycle comparisons before selecting it. Ticker/listing Worker routes
   still apply and are not a fallback for denied direct Kline access.
5. Only after review activate the selector at a natural generation boundary.
   Keep the current strategy/capital/single-writer contract and timer frequency.
   The sidecar is a market-cache writer, never a portfolio/result publisher.

## Rollback and stop proposal (not executed)

- During sidecar-only observation, production still uses its existing path;
  stopping the sidecar changes no production source. Preserve its SQLite file,
  receipts, cooldown/halt and leases. Do not delete state or auto-clear a denial.
- After a future consumer switch, rollback is a separately reviewed source
  selection at a generation boundary, with no mixed-source window. On a direct
  country/unknown 403, stop that attempt and report UNKNOWN: never automatically
  route the denied request through Worker, a new domain or another egress. A
  legacy-source rollback cannot be used as a denial bypass.
- Preserve generation IDs and all existing ledgers/results. Do not reset main,
  replay strategies, restore historical portfolio snapshots or change V1/V2,
  Sentinel, PR35, capital limits or evaluation schedules.
- Exact systemd unit, runtime account, installed launcher and permission steps
  remain unverified; this proposal deliberately contains no guessed production
  start/stop/install command. Clean lease recovery needs proof that all previous
  HTTP requests/processes have stopped and a separately reviewed state repair.

Integrated main `305c2a412166fa9a7f38a66778e93f25830e97e1` before consumer work.
Its new signal provenance/reentry contracts are preserved and extended only to
validate explicit cache transport evidence. No strategy thresholds, capital
rules, historical results, PR35 or paused Sentinel migration changes are included.

## Independent release review, 2026-10-11 (no activation)

### Exact inputs and price roles

| Consumer quantity | Actual input | WS topic / REST initialization and repair |
| --- | --- | --- |
| Bybit 15m micro features / six-hour range | 25 x 15-minute bars, current bar included; existing formulas use their existing slices | `kline.15.SYMBOL`; `/v5/market/kline?category=spot&symbol=SYMBOL&interval=15&limit=25&end=MS` |
| Bybit 1h return / volume | Last TWO of the five hourly bars: current close vs preceding hourly open, plus both turnovers | `kline.60.SYMBOL`; same REST path, `interval=60&limit=5` |
| Bybit 4h return / volume | FIVE hourly bars: current close vs oldest hourly open, plus five turnovers | Same `kline.60.SYMBOL`; **no** `kline.240` or `interval=240` request |
| BTC-relative comparisons | The same 15/60 windows for `BTCUSDT`, in the exact same frozen generation | Same topics/endpoints, no independent late benchmark fetch |
| Discovery reference prices and 24h change/turnover | Existing bulk Worker ticker `lastPrice`, plus Worker official spot listing/cache | `/bybit/tickers` and existing listing routes, unchanged |
| Existing held-position mark/depth/management evidence | Existing `hunter_bybit_management` direct spot ticker, orderbook, 60/15 REST path | **Outside this cache selector and cooperative budget**, unchanged |
| Binance signals and reference prices | Existing Binance `1h`/`15m` REST paths and ticker | Outside this PR, unchanged |

The 1h/4h labels preserve the current partial-hour formulas; they do not become
strict trailing 60/240-minute interpolations. The cache close is a last-trade
price for that candle, not a mark-price, orderbook execution price, or permission
to trade. Discovery's ticker mark may be newer than the Kline generation cut;
existing ticker freshness permits response time within +/-120s of the capture
time. This PR does not assert ticker and WS observations are simultaneous.
Kline source/receive freshness is <=90s at freeze; downstream snapshot validity
remains the existing <=900s. Measure both lags on the actual server before use.

“France direct” means a proposed direct request from the existing server's
egress to fixed official Bybit endpoints. This review made no such market
request. Prior user-reported single-symbol successes and country-related error
text do not prove current egress geography, all-roster access or sustained
availability. Worker remains the explicit listing/ticker provider, and the
default Kline provider while the selector is unset/WORKER. It is never an
automatic alternative after direct Kline denial.

### Failure review and fixes

- REST warmup must provide complete 25/5 windows; a fresh WS observation and ACK
  are independently required. A newly seen WS timestamp older than a just-written
  REST revision now updates WS liveness without replacing the newer bar. True WS
  duplicates/out-of-order timestamps still do not refresh liveness.
- Disconnect/stop sets a shared cancellation event. Repair checks cancellation,
  current session and deadline while waiting for budget and again after leasing,
  before sending. Running requests may finish, but cannot publish or retry into
  a dead session. A late 403 is still classified/persisted before cancellation.
  SIGTERM/SIGINT uses this stop path; backoff is interruptible.
- A known HTTP403 remains a denial even if its body times out, is incomplete,
  has an I/O error or exceeds the size bound. Unknown classification persists a
  manual-review halt. No body failure downgrades it to an ordinary retry.
- SQLite errors stop the collector instead of reconnecting. If a known denial
  cannot be stored, `DENIAL_STOP_NOT_DURABLE_MANUAL_REVIEW` explicitly means its
  durable stop was **not** written. External automatic restart must stay disabled
  until an operator reviews/recoveries storage and the denial state.
- Storage failure, hard kill or SIGKILL may prevent the disconnect record. The
  last committed cache can remain readable only within the existing <=90s
  freshness bound; there is no claim of instantaneous invalidation after an
  unrecorded crash. Expired/missing/corrupt data becomes UNKNOWN. A readable but
  permanently stale cache is not a recovery mechanism. Do not clear leases or
  delete/recreate the DB as a shortcut.
- Five connection attempts are bounded **per process invocation**, with
  5/10/20/40s backoff. Repeated service restarts could bypass that invocation
  bound; initial observation therefore requires `Restart=no`, one singleton
  instance, and reviewed start-rate controls. No service was installed here.

### What was actually read on the server

At 2026-10-11 21:21 UTC the connected coin-server tools returned READ_ONLY and
repository `/opt/shadow-runner/btc-grid-state`. Discovery and position-monitor
units were loaded/activating; the existing `hunter-market-stream.service` was
active/running. That existing stream is not evidence that this Bybit sidecar is
installed. The tool exposes an approved service list, not a complete unit inventory.

Base checkout SHA was `19b7c6bbe63a5f5741f051cf62a5d469290d2703`; its cached
origin/main was `b661483e05ad7c3491a89715b47c88f44bc0fd36`, explicitly not a live
fetch. Fresh job evidence reported Discovery/Monitor source `ddbfd5b6...`,
Research/readback `7c891916...`; inclusion of this PR was UNKNOWN. Do not infer
job code from the base checkout. Only untracked `research/__pycache__/` was
listed locally; nothing was removed. The read-only status also reported existing
fast-watch degradation, which is outside this PR and not evidence of a new
cache fault. No SSH or denied launcher-path route was retried.

### Production prerequisites and smallest staged release

1. **Before any install:** agree the reviewed commit, operator execution channel,
   actual runtime account and installed launcher path. Verify current job code
   and runtime env by approved reads. Python/SQLite versions, time synchronization,
   disk/inodes/headroom, `websockets==15.0.1`, shared-directory existence/ownership,
   consumer read permissions, complete unit inventory and service restart policy
   are **unverified** with current tools. Read-only status is not install authority.
2. **Prepare an isolated release:** after separate authorization, stage the exact
   code and pinned runtime dependency under a verified release path, outside job
   temporary checkouts. Provision `/var/lib/hunter-bybit-stream` for the actual
   collector writer and consumer reader only (including SQLite journal needs).
   Review a singleton continuous-service unit, bounded stop behavior, `Restart=no`
   and logs/storage alerts. The PR currently contains no installed unit, dependency
   environment or automatic roster refresher. No API key is required.
3. **Sidecar-only observation:** keep the consumer selector unset/WORKER. Capture
   a checksum-valid, <=15-minute authoritative manifest, record its hash and stable
   topic digest, then start only the sidecar through the approved operator route.
   Do not invoke discovery/research manually. Require all topics to become ready;
   measure cold bootstrap, warm reads, disk growth, queue/lock waits, source/receive
   lag, complete 15m and hourly rollover, and one approved controlled reconnect.
   Inventory simultaneous direct Bybit held-position traffic: its requests are
   not counted by this sidecar DB. Offline six-process contention proves only the
   cooperative local governor, **not Bybit online rate-limit acceptance**.
4. **Separate consumer decision:** after natural-cycle evidence is reviewed, apply
   `HUNTER_BYBIT_KLINE_SOURCE=SPOT_CACHE` only to the authorized discovery executor
   at a generation boundary; retain `HUNTER_BYBIT_WORKER_MARKET_ENABLED=1` for
   listings/tickers. Verify env propagation through the actual launcher. Current
   runner code inherits environment and merges workflow step env, but installed
   behavior is not yet verified. GitHub runners without the local DB must retain
   WORKER. Keep all single-writer/executor/CAS gates; verify the normal published
   snapshot source/schema, UNKNOWN handling and remote readback.

Do not enable the consumer on any of: unresolved403, unrecorded durable halt,
clock drift, missing topics, roster mismatch, cache corruption/permission errors,
unbounded disk growth, excessive lock/read lag, repeated reconnects, or a natural
cycle whose cache freeze cannot meet its freshness/window requirements. These
are stop-and-review conditions, not invitations to broaden scope or lower checks.

**Rollback conditions:** During observation, stop only the new sidecar if readiness,
storage, rate or latency checks fail; production remains on the unchanged source.
After any separately approved consumer activation, stop new cache-selected
generations, preserve already published generation/CAS receipts and all ledger
history, then review selection of WORKER for the next natural generation. Never
mix a half-produced generation's sources, replay portfolios, reset main or use
rollback as a country/unknown403 bypass. Preserve DB, cooldown/halt and stranded
leases; establish that old sockets/processes ended before any reviewed repair.

Real server natural-running evidence is still required for public access,
aggregate same-IP budgets, sustained delay/completeness, actual resource use,
service shutdown/restart behavior, manifest turnover, executor env propagation,
and published CAS/readback. Offline fixtures validate failure logic; they do not
fill a production disk or certify those operating conditions.

## Primary references

- https://bybit-exchange.github.io/docs/v5/websocket/public/kline
- https://bybit-exchange.github.io/docs/v5/ws/connect
- https://bybit-exchange.github.io/docs/v5/market/kline
- https://bybit-exchange.github.io/docs/v5/rate-limit
- https://websockets.readthedocs.io/en/15.0.1/reference/sync/client.html

Checked 2026-10-11. Official HTTP limit is 600 requests/5s/IP; 5rps is our
deliberately lower admission budget, not a claim that the official maximum is 5rps.
