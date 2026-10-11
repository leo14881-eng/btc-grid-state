# Remaining server-direct Bybit spot work

Read-only code review, 2026-10-11, against implementation commit
`11d267b878d73c66668f289992a4da7a7a23d74b`. PR101 is **partially complete**:
opt-in Kline cache exists, but public listings/tickers still depend on Worker.
This document proposes the remaining implementation; it changes no runtime
code, selector, service, permissions, credentials, strategy or ledger.

## Verified call sites and budgets

| Caller | Current request | Existing bounds / missing shared coverage |
| --- | --- | --- |
| `hunter_bybit_worker.collect` | `availability.spot_proxy_universe()`; `request('/bybit/tickers')` | Full listing cache24h; ticker once per discovery generation. A cached-list contradiction permits exactly one full-list refresh and one new bulk ticker request. Python ticker socket timeout60s. |
| `hunter_bybit_availability.spot_proxy_universe` | Worker `/bybit/spot` | Python timeout15s; complete full-list validation, BTC/ETH anchors, duplicate/category/cursor checks. Local cache lives in the job checkout; it is not a host-wide refresh lock. |
| `hunter_bybit_availability.main` | Same listing function, independently of Discovery | Called by the research pipeline; replacing only the Discovery fetcher leaves this Worker dependency. |
| Worker public routes | Official `/v5/market/instruments-info?category=spot` and `/v5/market/tickers?category=spot` | AbortController15s includes response body; one upstream request per route. No persistent cross-invocation host/IP governor in this JavaScript. |
| Legacy Worker Klines | Two Python batches in parallel, each <=20 symbols x2 intervals | Worker concurrency4 per batch, AbortController45s per batch; potentially8 upstream requests across two batches, not one host-wide max4. Cache-selected mode sends no Kline batch. |
| New sidecar | Official spot WS plus REST Kline repair | One collector lock, SQLite budget shared by cooperating processes: >=200ms admission, max4 held requests, three attempts per topic/session, durable cooldown/halt. |
| `hunter_bybit_management.collect` | Direct official ticker, two Klines, orderbook per held position; BTC two Klines | Four position workers, socket timeout12s, no common budget. With P valid positions and no early failures:2+4P requests per invocation. Must count alongside sidecar traffic. |

Discovery selects Worker at `hunter_cex_scan.bybit_from_authorized_region`.
The scheduled workflow explicitly sets `HUNTER_BYBIT_WORKER_MARKET_ENABLED=1`
and `HUNTER_BYBIT_DIRECT_DISABLED=1`. `SPOT_CACHE` currently requires the former.
The old `bybit_with_fallback` path is not a candidate for this migration: it has
alternate-host behavior and different validation. Do not reach it on a cache
or direct-request failure.

The authenticated Alpha request in `hunter_bybit_availability.alpha()` also
uses Worker, but is not part of the public spot listings/ticker/Kline plane.
No credentials were read or changed. A public spot migration must not be
described as removal of every Worker dependency while Alpha remains configured.
Moving that signed endpoint would need a separate scope and credential review.

## Candidate: one Kline collector and one shared direct transport

Use a single explicit server-spot selector for the entire public spot bundle;
default remains the exact current Worker path. The final selector name is to be
chosen during implementation. Reject unknown/conflicting selectors before IO.
An explicit `DIRECT_DISABLED=1` remains a veto; its deployed value must be
reviewed separately before activation, not silently overridden in code. A
GitHub executor with no server cache stays on its explicitly selected path.

The smallest code candidate keeps the one resident Kline sidecar and adds a
common direct public transport used by the existing Discovery and availability
one-shot jobs. They fetch only metadata, not a second Kline history. All direct
requests acquire/release the **same existing host SQLite budget**; a separate
semaphore, per-checkout DB or independent 5rps limiter would be incorrect.
Use fixed official host/path/category allowlists, no proxy/redirect/host change,
no private headers, and the same durable403 handling for every endpoint.

The two missing endpoints are:

- `GET https://api.bybit.com/v5/market/instruments-info?category=spot`, without a
  symbol restriction, cursor or limit. Current official spot documentation says
  pagination is unsupported. Unexpected nonempty cursor/partial shape is UNKNOWN.
- `GET https://api.bybit.com/v5/market/tickers?category=spot`, one bulk response;
  preserve `lastPrice`, `turnover24h`, `price24hPcnt`, all-symbol validation and
  existing price/source semantics. Do not replace it with per-symbol REST loops
  or a ticker WS reconstruction in this minimal change.

Keep full-list TTL24h, Trading/USDT filtering, xstocks exclusions, and the
one-time cached-list/ticker contradiction refresh. Move direct-list cache and
refresh coordination into shared host state, with a separate source identity
from the old Worker cache. Concurrent cold refreshes must join one in-flight
request. Tickers are bound to one discovery generation and not reused for a new
generation; retry of the same generation may use only its complete immutable
receipt. Cache claims must not auto-expire while an old request can still run.

This choice shares accounting across processes and does **not** create a second
persistent collector. It does, however, require metadata requesters to write
budget/receipt coordination records. SQLite has no per-table OS permissions:
granting DB write is broader than today's read-only Kline consumer contract.
The actual server accounts and existing rights are unknown. This is a release
prerequisite, not permission to grant new access now. Request clients must open
only preprovisioned state, never initialize a replacement empty budget.

If consumers must retain filesystem read-only access, the alternative is a
typed local IPC request to the **same sidecar**, which alone owns upstream IO
and DB writes. Bound the queue, deduplicate by generation/request kind, enforce
peer/path permissions, and return immutable receipts. It requires more IPC and
shutdown code, but no second daemon, TCP listener or firewall change. Do not
pretend direct multi-process DB writes and this single-owner alternative have
the same permission footprint. Select between them after the runtime-account
read, before implementing the complete plane.

Both designs need a Worker-independent bootstrap manifest. Derive exactly the
current Bybit-only set plus BTC from validated direct metadata and the existing
authoritative Binance set; bind listing receipt, exclusion inputs and roster
hash. Initial lack of Kline history stays UNKNOWN. An explicit roster adoption
invalidates readiness, then requires full25/5 recovery, subscription ACKs and
fresh WS observations before READY. Never manufacture a Worker snapshot just
to satisfy today's `roster()` validator, widen to all symbols, or run an extra
strategy workflow to seed it. Manifest turnover and restart/adoption ownership
must be included; otherwise the migration still has a bootstrap dependency.

Let N be selected Bybit-only assets plus BTC. With no retries, cold metadata
plus Kline repair costs2+2N requests; a warm generation costs one bulk ticker
and zero Kline REST requests. A cached-list contradiction costs three metadata
requests total (ticker, full list, ticker). Three-attempt repair allows up to6N
Kline requests; every metadata retry must also be explicitly bounded and
charged. These are request counts, not latency/online-quota guarantees.
The theoretical5rps admission floor for R requests is(R-1)*0.2s, also subject
to max4 outstanding, storage waits, response latency and cooldown.

To claim a shared budget for **all public Bybit requests on the server**, adapt
`hunter_bybit_management.request` to the same transport too, preserving its
12s timeout, endpoints, per-position validation, fees and protective behavior.
This crosses the strategy-release module hash and needs its existing review
process; it is a separate scope item, not an incidental refactor. Until then,
inventory and measure its2+4P traffic and other server callers explicitly.
Public-spot Worker removal alone does not prove global same-IP accounting.

## Smallest code scope after the design decision

| Area | Intended change |
| --- | --- |
| New shared direct transport module; `hunter_bybit_stream.py` | Extract fixed endpoint admission, denial, retries and response receipts; Kline repair continues through the same budget. Do not expand the existing prefix URL check into an arbitrary URL fetcher. |
| `hunter_bybit_stream_store.py` | Shared metadata receipts/refresh coordination, direct manifest identity and safe roster adoption; migration preserves budget halt/cooldown/leases. IPC option adds a bounded broker in the same service instead of client DB writes. |
| `hunter_bybit_availability.py` | Pure common full-list validation plus explicit direct adapter for both callers; old Worker cache/source and Alpha stay untouched. |
| `hunter_bybit_worker.py` / new direct snapshot adapter | Reuse row/features/publish logic; metadata comes from the selected direct adapter. New explicit direct source/schema instead of relabelling Worker evidence. |
| `hunter_cex_scan.py`, `hunter_bybit_stream_consumer.py` | Atomic bundle selector, explicit source validation, independent bootstrap, fail closed on missing metadata/roster mismatch. |
| `hunter_early_signals.py` | Accept the new verified snapshot contract without changing score, freshness or entry gates. |
| Offline tests, guarded runner, docs | Legacy equivalence, new direct receipts/failure tests and aggregate budget coverage. Existing output filenames/CAS/ledger writer stay unchanged. |

No Worker JavaScript, timer or runtime env change is required to prepare that
code. Actual source selection and account/service configuration remain separate
deployment actions. Holdings transport sharing is additional to this minimum.

## 180-second age: what the current contract actually guarantees

For generation cut G, current-bar source/receive time T and read completion R,
the code independently checks `0<=G-T<=90s` and `0<=R-G<=90s`. Therefore
`R-T<=180s`. The store and provenance validator deliberately reference G;
consumer code rechecks R after SQLite returns. An example with T=G-90s and
R=G+90s is allowed by those checks, not evidence of a90s-at-consumption bound.

The old Worker checks upstream response time within +/-120s of its own current
time, Python bulk ticker time within +/-120s of captured time, and current
Kline window coverage. The snapshot/early-reader gate is<=900s from capture.
These are different reference points, not an existing universal90s raw-price
contract. No contradiction with a frozen strategy threshold was established
in this review, so no runtime threshold was silently changed.

The180s figure applies only at cache-read completion, not final publication or
later use. At the snapshot gate alone, a G-90s source timestamp could be990s
old when a G snapshot is accepted at G+900s. Existing independent policy and
management freshness gates still apply and must not be weakened. Frame/REST
response time is not a last-trade timestamp. Neither180s nor990s is an approval
of that latency for production decisions.

If the required release contract is instead<=90s observation age at read exit,
the focused follow-up is to check R against **every selected current bar's**
source and receive time, retain both old cut checks, persist the read-completion
receipt and validate it downstream. Test89,999/90,000/90,001ms and the two90s
windows together. This tightens data admission; it is a contract decision, not
a justified change to trading policy or an already implemented fix.

## 600-second recovery and socket timeouts

`direct_get` already passes `timeout=10` to urllib. This limits socket blocking
operations, not the entire DNS/connect/TLS/headers/body lifecycle. The single
`response.read(1_048_577)` has a size limit but no monotonic total deadline.
A slowly advancing header/body can outlast10s; DNS resolution is not separately
bounded here. The600s checks prevent admission/acceptance after the recovery
deadline, but do not interrupt an in-flight operation. ThreadPool shutdown waits
for it. WS has open10s, close5s and receive1s settings; SQLite lock wait is10s.
None establishes a total REST shutdown SLA.

The existing code keeps the request lease until the request really returns,
rejects late data and classifies a late403 before cancellation. No failure of
those admission/publication protections was established by this review. It does
have an unresolved availability/stop-time limitation. In particular a403 whose
body keeps progressing can delay global classification; do not claim immediate
denial propagation from these existing socket settings.

Before promising a hard wall-clock recovery/stop SLA, the shared transport needs
a monotonic total deadline, bounded resolution/connect/header/body phases, and
cancellation that actually closes the socket before releasing the lease. A
Future timeout or cancelled thread alone is not a fix and could exceed max4 by
releasing a still-active request. Ensure a known403 becomes a global stop even
when its body cannot finish, preserving UNKNOWN/manual-review classification.
This needs focused transport work and tests, not changing strategy thresholds.
No new runtime library/process or server setting was installed in this review.

## Acceptance before activation

1. Offline default-path equivalence: unset selector issues exactly the existing
   Worker calls; direct mode issues zero public-spot Worker requests, including
   availability, cold bootstrap and listing contradiction. Cache/direct failures
   never invoke the old fallback. Alpha exclusion remains explicit.
2. Metadata fixtures: full vs partial/cursor, duplicate/missing symbol, nonfinite
   price, stale/future response,24h expiry, delist/new-list contradiction and
   concurrent cold refresh. Preserve one fresh ticker receipt per generation.
3. Coherence: listing receipt, requested Bybit-only roster, cache epoch and all
   asset/BTC windows agree. New roster starts UNKNOWN; no mixed source receipts,
   cross-generation ticker reuse, forged Worker labels or second result writer.
4. Budget/fault fixtures: mixed list/ticker/Kline clients in multiple checkouts,
   total >=200ms spacing/max4; cooldown/halt shared across endpoints; late403,
   storage full/corruption, crashed refresh owner and unreleased leases. A held
   position adapter, if approved, must join these tests without policy changes.
5. Transport fixtures before a hard deadline claim: stalled DNS/header/body,
   progress just inside each socket timeout, cancellation and concurrent403;
   prove actual socket termination before lease release and bounded teardown.
   Age tests distinguish cut age, read age, downstream age and last trade time.
6. Only after a separate deployment decision: observe natural server cycles,
   full-roster direct availability, zero public-spot Worker calls, actual shared
   request counts,15m/hour rollover, metadata turnover, delay distribution,
   disk/lock load, process shutdown and unchanged CAS/readback. No real orders,
   manual strategy rerun or online rate stress test is part of this proposal.

Unresolved403, metadata incompleteness, roster/source mismatch, missing budget
coverage, storage failures or unacceptable latency blocks activation. Rollback
must preserve denial state and receipts; it is never an automatic route change.
The read-only server tools and earlier SSH/path denials have not been bypassed.

## References

- [Official spot instruments](https://bybit-exchange.github.io/docs/v5/market/instrument)
- [Official spot tickers](https://bybit-exchange.github.io/docs/v5/market/tickers)
- [Python urllib timeout semantics](https://docs.python.org/3.12/library/urllib.request.html)
- [Python HTTP connection timeout](https://docs.python.org/3.12/library/http.client.html)

Official documentation checked 2026-10-11. No Bybit market endpoint was called.
