# Stock position collection: default-off persistent candidate

Baseline: `ddbfd5b6cd97cb825b3f674ffc512ec796d40836` (origin/main fetched on
2026-10-11, after PR104). The original collector frozen in
`tests/fixtures/stock_monitor_legacy.py` is unchanged between `0541d6b9` and
this baseline. No AGENTS.md or .agents rules are tracked at this baseline.

## Activation and scope

The deployment template sets `STOCK_SHADOW_INCREMENTAL_QUOTES=0`. With the flag
absent or anything other than exact `1`, `main()` uses the original full three-day
collector and never opens a cache. This PR contains the real runner/engine
integration candidate, but **does not change a deployed environment, activate a
service, merge, or deploy**. The explicit `QuoteWindowCache()` API remains useful
for in-process differential replay; the opted-in runtime uses SQLite across fresh
checkouts and Python processes.

Normal runs gain additive collection counters and stock monitor stage timers in
the existing health payload. Daily discovery, SEC/fundamentals, strategy, quote
validity, exchange calendar, state mutations, ledger, writer/CAS/readback and
scheduler authority are unchanged. Tests only simulate shadow decisions. The
runner changes only cache environment binding and preflight environment scrubbing;
its admission, commands, deadlines, portfolio publication and readback stay intact.

## Storage and runner binding

The existing launcher passes `$STOCK_SHADOW_STATE_ROOT/runtime` to the runner;
the default is `/var/lib/stock-shadow/runtime`. Existing units already run as
`stock-shadow`, have `UMask=0077`, and allow writes beneath `/var/lib/stock-shadow`.
No service unit, permission, directory creation policy or launcher is changed.
These facts are verified from repository deployment definitions; no live server
inspection or write was performed.

When and only when the monitor job is opted in, the runner replaces any inherited
cache path/scope with `stock-quotes-preview-v1.sqlite3` or
`stock-quotes-publish-v1.sqlite3` in its validated persistent runtime directory.
The two modes cannot warm or overwrite each other's file. Main/replay jobs and
preflight pytest receive none of the three cache environment variables. A direct
engine invocation must supply an absolute cache path outside its current checkout;
missing, relative or checkout paths fall back without creating files.

`quote_window_store.py` persists one snapshot containing symbol-to-timestamp
cursors, an opaque publication token, revision, delayed query endpoint, canonical
configuration, and a checksum. No price, credential, trade, position, balance or
ledger is stored. Configuration identity includes cache/schema and selection
versions, endpoint/source, timeframe, SIP feed, `adjustment=all`, the 20-minute
delay, three-day bound, request/page limits, delay-endpoint enforcement and scope.
Changing any of these causes a cold query. Changes to selection semantics must
bump the selection version; unrelated runtime Git commits do not invalidate it.

Capacity is bounded to 2,048 symbols, 256 KiB of serialized cursors and a 1 MiB
SQLite database. Above-capacity cohorts bypass the cache entirely. A successful
snapshot contains only current held symbols with valid observations; exited or
missing symbols disappear instead of accumulating. SQLite uses DELETE rollback
journaling and FULL synchronous transactions, without accumulating a WAL. A
transient rollback journal may take approximately another database-sized file
plus SQLite headers; normally it is removed on commit. Two scope files are the
only cache files created by this implementation.

Reads and writes use a 250 ms SQLite lock timeout. Network work never holds a
database transaction. Publication obtains a write transaction, compares the
snapshot's opaque token, checks that query time does not move backward, then
atomically replaces the snapshot and token. A competing process gets `CONFLICT`
and keeps its freshly fetched quotes without overwriting the winner. A new token
also fences old readers after a file is externally recreated. This transaction
is solely for query cursors; the portfolio writer/CAS/readback is untouched.

Checksum/data corruption, expired/future snapshots or configuration mismatch
cause cold reads. Valid headers with corrupt payload can be repaired by replacing
the snapshot after collection (or by publishing an empty snapshot after a failed
batch). Corrupt SQLite files, unknown schema or foreign databases are not deleted,
truncated or repurposed: collection stays on full queries until a separately
approved operator repair. Permission, lock and disk-full failures do not suppress
fresh response quotes. An interrupted transaction rolls back; a publication
failure can retain an older cursor, but the next process always re-fetches from it
within the same bounded horizon and can never return an older cached price.

## Incremental selection

The cache stores the timestamp of each latest valid observation, never a price.
Warm holdings are batched from the earliest cached timestamp, inclusively, to
the unchanged `now - 20 minutes` endpoint. Cold, new, expired or corrupt cursors
use a separate batched three-day window. A missing holding therefore does not
force all other holdings back to a full scan. Both cohorts retain the 7,000
character request-target batching policy and 10,000 bar page limit.

Every returned observation comes from this call's HTTP responses. Inclusive
overlap re-reads the last timestamp, so a same-timestamp close revision, a new
later bar, and a late insertion after the cursor can change the selected quote.
If a warmed symbol disappears from the narrowed response, the complete logical
batch is fetched once more over the original three-day interval, replacing the
narrowed result. This covers a deleted latest bar whose replacement is earlier.
An older correction that cannot change the latest bar is not downloaded.

All pages must finish before any observations from a logical batch are exposed.
Failure discards that batch and its cached cursors. The persistent path publishes
an empty snapshot on a collection error, so a later process cold-starts. No
immediate network retry policy is added. Successful other
batches remain available, just as in the original multi-batch collector. Since
the experimental warm/cold partition can change batch boundaries, failure
coverage, error batch indexes and request counts need not equal the full-window
collector under injected transport failures. Fresh cached values never substitute
for a failed request. Full successful-response quote equality is the comparison
contract, along with existing fail-closed quote validity and decision gates.

The provider contract used by the fixture is documented in
[Alpaca historical bars](https://docs.alpaca.markets/us/reference/stockbars):
inclusive interval endpoints, symbol-then-time ascending order by default,
and pagination over the total number of bars across symbols. No provider probe
was performed. The original per-page selection and cross-page overwrite are
preserved. Enabled incremental collection additionally discards a batch if its
selected bar is later than the unchanged `now - 20 minutes` request endpoint;
the diagnostic is `BAR_AFTER_DELAY_ENDPOINT`, never a new usable quote/cursor.
The disabled legacy path is unchanged. This is a conservative difference for an
out-of-contract provider response, not a change to the 20-minute delay. Arbitrary
provider ordering or a mutable multi-page snapshot remains outside the verified
equivalence contract.

## Measurement definitions

- `http_requests`: attempted `urlopen` calls, including failed calls.
- `pages`: responses successfully decoded as JSON, before bar extraction.
- `bars`: entries in list-valued response bar arrays, including overlaps.
- `response_bytes`: bytes returned by completed `read()` calls, including invalid
  JSON. Not wire bytes, headers, or partially read bodies that raised an error.
- `http_seconds`: wall time opening, reading and decoding the responses.
- `elapsed_seconds`: collection call wall time, including a shared-cache lock wait.
- `cache_hits`: usable cursor count; zero cached prices are returned.
- `fallback_batches`: narrowed logical batches retried once at the full horizon.
- `symbol_coverage`: presence of returned symbol observations, not freshness.
- `interval_coverage`: always `UNKNOWN`; collection does not prove that every
  five-minute interval exists. It neither synthesizes missing bars nor certifies
  gap completeness. Existing health `invalid_quotes`/`valid_quotes` retain their
  freshness and action-admissibility meaning.
- `disk_cache`: read/publish status, cursor counts and read/write seconds. It
  distinguishes cold/config/corruption/capacity fallback, lock/write failures,
  clock regression and concurrent-publication conflict without logging secrets.

The existing top-level `requests` remains backward compatible, including its
historical accounting on malformed responses; use `http_requests` for actual
attempts. The `windows` list records cohort size/start, with `full_start` and
`query_end` recording the bound and delay endpoint.

`stage_timings` measures load/validation, calendar lookup, quote collection,
evaluation/validation, and local state/summary save. `through_summary_seconds`
spans those stock monitor steps. It excludes the final health write/print and
outer runner, Git writer/CAS/readback, scheduling, and all crypto jobs. These
names intentionally do not claim complete production job end-to-end timing.

## Reproducible offline evidence

Run:

```sh
python -m pytest -q tests/test_stock_position_collection.py tests/test_stock_persistent_quotes.py tests/test_stock_quote_safety.py
```

The test suite compares outputs against the frozen original collector and
intercepts all HTTP with dummy fixture credentials. A deterministic 536-symbol
regular-session fixture spanning October 6-9 measures the following actual
mock response/request counts for the 17:00 UTC call, after warming at 16:55 UTC:

| Measurement | Original three-day query | Warm incremental query |
| --- | ---: | ---: |
| Logical batches | 1 | 1 |
| HTTP/page calls | 13 | 1 |
| Response bars | 125,960 | 1,072 |
| Response body bytes | 5,660,786 | 54,171 |
| Equal per-symbol price/timestamp/source outputs | 536 | 536 |

Cold start takes 13 fixture calls. Adding a persistently missing symbol to the
warm cohort takes two calls (one warm group, one cold missing-symbol group),
retains `PARTIAL` symbol coverage, and preserves every returned quote.
These are **synthetic offline results, not observed production savings**.
The reported production 62 HTTP pages / 54.716 seconds was task input, not
re-measured here. Local fixture wall time is not a provider latency estimate.

Tests cover pagination/failure, alias normalization, delay endpoint, same-time
revision, late insertion, deleted anchor, missing/new/removed holdings,
corrupt/future/naive/expired cursors, restart, recovery after failed incremental
pages/backfill, invalid prices, malformed payloads, weekend/holiday and both DST
boundaries, independent/shared concurrent caches, default-off production entry,
and identical portfolio/events/summary and shadow exit decisions in a frozen run.

Persistent tests additionally cover configuration/schema/checksum invalidation,
capacity limits, unwritable paths, disk-full simulation at update and commit,
SQLite lock contention, competing processes, abrupt process exit during a write,
old leases after file recreation, empty holdings, future/backward clocks and
multi-cycle corrections/withdrawals/missing data/recovery over 536 symbols.
Two independent Python processes verify cold 13 / warm 1 calls after restart.
An integration test exercises `runner.run` with real child environments and actual
monitor child processes (Git/provider/publication are isolated test fixtures).
Across both cycles, all 536 quotes and complete portfolio/events/summary outputs
match separate disabled-path child processes; preview creates only its own cache.

The four-cycle 536-symbol persisted replay measures:

| Cycle | Original calls | Candidate calls | Full-window fallbacks |
| --- | ---: | ---: | ---: |
| Cold start | 13 | 13 | 0 |
| Warm cycle with corrected latest close | 13 | 1 | 0 |
| Withdrawn latest bar and missing holding | 13 | 14 | 1 |
| Recovery in next process/call | 13 | 2 | 0 |

Every cycle compares each returned price/timestamp/source with the frozen
collector, including the missing symbol. The 14-call case deliberately pays for
bounded backfill rather than returning a withdrawn cached observation.

The new `Stock position collection offline` CI runs these tests and existing
stock safety/strategy/server regressions on Linux with read-only repository
permissions and no provider secrets. Existing stock migration CI also applies.

## Separate change review

The full baseline-to-candidate diff was reviewed separately from implementation.
An AST comparison confirms the nine existing ledger/calendar/P&L/profit-floor
helpers and the complete per-position decision/mutation loop are unchanged; the
frozen collector oracle matches the pinned baseline. Content comparison also
confirms unchanged daily scanner, quote safety, calendar, SEC observer, portfolio
publisher, runtime authority, launcher and monitor service unit. The review
tightened rejection of unknown SQLite versions before applying database pragmas.
No existing stock strategy/security test was changed. This execution review does
not replace an external reviewer or authorize deployment.

## Remaining validation before activation

The persistent lifecycle and actual runner binding are now implemented and
tested as a default-off release candidate. No new service permission is indicated
by the checked-in deployment definitions. Remaining release gates are external
review, an approved check of the real runtime filesystem/SQLite locking/ownership,
authorized recorded-provider replay across correction scenarios, and actual
monitor latency/exit-freshness measurement. None was replaced with a live bulk
probe or an assumed production speedup. SQLite is intended for the existing local
filesystem, not a shared network filesystem or a distributed multi-host cache.

Missing warmed symbols can add a full-horizon pass, and warm/cold grouping can
change partial availability under network failure. Selected bars outside the
requested delay endpoint are now rejected only in the enabled path. Those
conservative/error-path differences need explicit review alongside the verified
normal-response equivalence. Interval completeness remains UNKNOWN. Keep the
deployment flag at 0 until review and separately authorized activation; reverting
it to 0 uses the original collector without opening or needing to delete SQLite.
