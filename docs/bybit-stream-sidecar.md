# Bybit public spot and Kline sidecar: review stage only

PR101 provides a default-off public-spot candidate: one Kline sidecar, a frozen
cache consumer, and direct instruments/bulk-ticker adapters for Discovery and
research availability. The default Worker behavior remains. Production has not
been enabled, the PR has not been merged, and no service/timer, account right,
credential, ledger, Worker JavaScript or strategy threshold was changed.

The full selector, metadata, cancellable transport and deployment-gap contract
is in [the direct-plane review](bybit-direct-plane-review.md). This document
specifies the Kline consumer and what its time bounds actually mean.

## Scope and protocol

The roster comes from a fresh, checksum-validated direct or Worker snapshot's
`venue_status.signal_expected_bases`, plus BTC. It is the existing Bybit-only
collection set, not all exchange symbols. A stable topic digest and separate
manifest receipt bind every frozen read. Roster changes need an explicit new
validated manifest and restart; no automatic roster adoption is installed.

Only `wss://stream.bybit.com/v5/public/spot` is allowed, with
`kline.15.SYMBOL` and `kline.60.SYMBOL`, at most ten args per subscription message,
and a serialized total argument limit of 21,000 characters. JSON ping is sent
every 20s. Session-specific ACKs, topic timestamps and contiguous history are
required independently. Pong does not refresh a topic. Missing/negative ACK,
disconnection or topic staleness makes its generation UNKNOWN.

Production bootstrap/repair goes through the supervised shared HTTP client to
`https://api.bybit.com/v5/market/kline`, with `category=spot`, one symbol,
`interval=15|60`, `limit=25|5`, and current `end` in milliseconds. There is no
alternate hostname, proxy, redirect or Worker fallback. Long outages restore
only the bounded current window. The legacy injected `get` seam is for offline
fixtures; production repair defaults to the cancellable client.

## Frozen generation and unchanged formulas

SQLite records per-topic ACK, receive/source times, gap status, bars and revisions.
REST requires all 25/5 consecutive rows. WS updates cannot reopen a confirmed bar
or replace a newer observation. Duplicate packets cannot refresh freshness.
Reconnect invalidates ACKs and requires repaired windows plus fresh WS observations.
REST confirmation is explicitly `REST_INFERRED`, never labelled WS confirmation.

`Store.reader().freeze(topics, G, now)` reads every asset and BTC in one transaction.
Both source and receive times must be at or before G. Storage keeps a 180s revision
horizon with one older baseline per bar, at most 256 revisions per bar, 256 readiness
records per topic, and a rolling 25/5+3 bar range. Missing history, eviction, roster
mismatch, stale/current bars, cooldown/halt, corrupt cache or incomplete topics
makes the entire generation UNKNOWN with no windows. A later revision cannot
backfill the earlier cut. Feature extraction uses copied frozen rows.

| Quantity | Existing input retained |
| --- | --- |
| 15m micro / six-hour features | 25 fifteen-minute bars, forming bar included |
| 1h return / volume | Last two hourly bars, current close vs preceding open and both turnovers |
| 4h return / volume | Five hourly bars, current close vs oldest open and all five turnovers |
| BTC-relative comparisons | Same 15/60 windows and frozen generation for BTCUSDT |
| Discovery price /24h turnover/change | Bulk ticker from the explicitly selected Worker/direct adapter |
| Held-position price/depth/management | Existing direct management requests, outside this candidate budget |

There is no interval 240 request. The 1h/4h labels retain existing partial-hour
formulas. A candle close is a last-trade value, not an execution price. All six
asset/BTC provenance packets retain the same generation/roster/manifest/session
binding, actual source/receive times and confirmation evidence. Historical trend
readers reject missing confirmation rather than inventing history. Existing
non-cache sources retain their behavior; no score, fee, position or entry gate
is lowered.

Cache-only snapshots use `hunter_bybit_stream_snapshot_v1` /
`OFFICIAL_BYBIT_V5_SPOT_CACHE` with explicitly Worker metadata. Direct-bundle
snapshots use `hunter_bybit_direct_snapshot_v1` /
`OFFICIAL_BYBIT_V5_SPOT_DIRECT_CACHE` with separately validated direct metadata.
The early-signals reader checks source, snapshot checksum and generation receipt.
UNKNOWN retains valid metadata coverage with zero signals and explicit failures
for expected symbols. The existing result filename, runner output list, CAS,
readback and single-writer contract remain. The sidecar never writes repo results.

## Time bounds, without a stronger freshness claim

At generation cut G, a current bar's source/receive time T must satisfy
`0<=G-T<=90s`. Read completion R separately must satisfy `0<=R-G<=90s`.
Consequently `R-T<=180s`, not 90s. This bounds cache read completion, not final
publication or later use. The existing downstream snapshot age gate is 900s;
that gate alone could accept a G-90s timestamp at age 990s. Other existing policy
freshness gates still apply unchanged. Neither 180s nor 990s is an approval of
that delay for production decisions.

WS source time is frame generation time; REST source time is response time.
The WS last-trade timestamp is checked at ingestion but is not a separate
retained price-age field. These checks do not guarantee a recent trade in an
illiquid asset, nor measure network RTT. Ticker response time may differ from
the Kline cut within the existing +/-120s check. Real acceptance must distinguish
cut age, read latency, publish age and last-trade age. Tightening admission to
90s at use would be a separate reviewed contract change.

The recovery admission/data horizon remains 600s. Each production HTTP request
now has a monotonic total deadline of at most 30s (or the remaining recovery
horizon), then actual kill/reap before normal lease release. Slow streaming
cannot keep a trusted result alive past the deadline. Unknown completion retains
its marker/lease and blocks further requests. See the direct-plane contract
for 1s reap handling, 403 and storage-failure behavior. This is not a hard real-time
OS or whole-service shutdown guarantee.

For 141 assets plus BTC, 284 Kline requests have an admission-only floor of 56.6s
at 200ms spacing. At four held requests and 10s latency each, at least 710s is
needed before retries; a 600s recovery cannot guarantee that cold start. Offline
admission tests do not measure French-server throughput. The sidecar runs outside
the scan critical path; incomplete recovery remains UNKNOWN.

## Observation and operating boundaries

The fixed state path is `/var/lib/hunter-bybit-stream/state.sqlite3`, with one
`collector.lock`. No per-checkout/env budget override exists. Five WS connection
attempts per invocation have 5/10/20/40s backoff. Repeated service restarts could
circumvent that invocation bound, so initial observation needs `Restart=no` and
reviewed start controls. Leases, orphan markers, cooldown and halts never expire
or reset automatically. A boot-clock change/nonempty legacy clock blocks startup
and cache reads. Storage failure does not justify reconnecting around a stop.

A crash without a committed disconnect and without an in-flight HTTP marker may
leave last committed data readable only under the existing freshness checks;
there is no claim of instantaneous cache invalidation after every hard kill.
An interrupted tracked HTTP request separately creates a global orphan stop.
Preserve state; prove old processes/sockets ended before reviewed recovery.

At 2026-10-11 21:21 UTC, approved coin-server read tools reported READ_ONLY,
repository `/opt/shadow-runner/btc-grid-state`, Discovery/position-monitor units
loaded/activating and existing `hunter-market-stream.service` active/running.
That service is not evidence this Bybit sidecar is installed. The tool's approved
unit list is not a complete inventory. Base checkout was
`19b7c6bbe63a5f5741f051cf62a5d469290d2703`, with cached origin/main
`b661483e05ad7c3491a89715b47c88f44bc0fd36`; these were not a live fetch.
Reported job code differed from that base checkout. Current PR inclusion and
runtime env remain unverified. Untracked `research/__pycache__/` was left alone.

Actual account/launcher, time sync, Python/SQLite/dependency versions, disk/inodes,
state-directory rights and complete service/restart inventory remain production
prerequisites. Metadata clients need coordination writes beyond a read-only Kline
consumer; no such right was granted here. Read-only status is not installation
authority. No SSH/launcher denial was bypassed and no market request was made.

Before any activation, review the exact code SHA and rights, then separately
observe natural full-roster cycles, 15m/hour rollover, metadata/manifest turnover,
cold/warm latency, real cancellation/stop behavior, total same-IP traffic and
unchanged CAS/readback. Do not manually rerun strategy jobs or change portfolio
writers for acceptance. Preserve all results/ledgers, denial state and receipts
on stop. A later source rollback requires a reviewed generation boundary and
cannot act as a country/unknown403 bypass.

The candidate is still OFF and unmerged. CI and independent review establish
code/fixture behavior, not live access, geography, rate-limit acceptance or
permission to install a service.
