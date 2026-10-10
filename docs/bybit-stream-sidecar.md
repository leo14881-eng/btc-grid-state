# Bybit spot Kline sidecar: review stage only

This PR adds an opt-in sidecar and a read-only cache API. It does not wire the
production scan to that API, install a service, alter a timer, or change any
capital, strategy, ledger, Worker or Sentinel file. No market requests were made
to develop or test it. A separate reviewed rollout and consumer integration are
required; an approved code PR is not a deployment approval.

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

SQLite stores per-topic ACK, receive/source times, gap status and latest bar
versions keyed by (topic,start). REST must validate all 25/5 consecutive rows.
WS updates cannot reopen a confirmed bar or replace a newer observation.
Duplicate packets do not refresh freshness. Reconnect invalidates all ACKs and
requires both a repaired window and a new WS observation per topic. Historical
REST confirmation is marked `REST_INFERRED`, never claimed as a WS confirmation.

`Store.reader().window(symbol, interval, generation_end_ms, now_ms)` returns
READY with ascending seven-column rows and per-bar provenance, or explicit
UNKNOWN with no rows. It includes the current forming bar, exactly as the
existing feature extractor does. Readiness requires continuous coverage, a
live session, ACK and topic observation age <=90 seconds. Every selected bar's
source AND receive time must be <=generation end. This latest-only cache cannot
recreate a past current-bar value after a later revision; it returns UNKNOWN.
It never uses today's recovery response to repair an old decision.

Sources are `OFFICIAL_BYBIT_V5_SPOT_WS` and `OFFICIAL_BYBIT_V5_SPOT_REST`, never
`OFFICIAL_BYBIT_V5_VIA_WORKER`. The old manifest source describes roster origin
only. Production consumers still use their existing Worker adapter in this PR.

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
leave UNKNOWN. Offline rate tests are not a French-server throughput measurement.
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
4. Separate PR: consume the cache with explicit UNKNOWN propagation; no strategy
   evaluation cadence or indicator change. Only after review consider deployment.

Local planning only (no network):
`python -m research.hunter_bybit_stream --manifest /absolute/captured-snapshot.json`

Live `--collect` is intentionally not invoked by CI or this change. There are no
service/timer templates in this first PR. Test command:
`python -I -B -X utf8 scripts/check_bybit_stream_offline.py` (Linux).

## Primary references

- https://bybit-exchange.github.io/docs/v5/websocket/public/kline
- https://bybit-exchange.github.io/docs/v5/ws/connect
- https://bybit-exchange.github.io/docs/v5/market/kline
- https://bybit-exchange.github.io/docs/v5/rate-limit
- https://websockets.readthedocs.io/en/15.0.1/reference/sync/client.html

Checked 2026-10-10. Official HTTP limit is 600 requests/5s/IP; 5rps is our
deliberately lower admission budget, not a claim that the official maximum is 5rps.
