# Stock position collection: observation and offline incremental prototype

Baseline: `ddbfd5b6cd97cb825b3f674ffc512ec796d40836` (origin/main fetched on
2026-10-11, after PR104). The original collector frozen in
`tests/fixtures/stock_monitor_legacy.py` is unchanged between `0541d6b9` and
this baseline. No AGENTS.md or .agents rules are tracked at this baseline.

## Activation and scope

The production `main()` call still uses the original full three-day collector.
There is **no environment toggle, persistent cache, service change or activation**.
Only an explicit Python call with `window_cache=QuoteWindowCache()` uses the
experimental path. A new cache instance or process starts cold. This is a staged
review artifact, not a deployed performance fix.

Normal runs gain additive collection counters and stock monitor stage timers in
the existing health payload. Daily discovery, SEC/fundamentals, strategy, quote
validity, exchange calendar, state mutations, ledger, writer/CAS/readback and
scheduler authority are unchanged. Tests only simulate shadow decisions.

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
Failure discards that batch and its cached cursors; a later call cold-starts the
affected symbols. No immediate network retry policy is added. Successful other
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
preserved; arbitrary provider ordering or a mutable multi-page snapshot remains
outside the verified contract.

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
python -m pytest -q tests/test_stock_position_collection.py tests/test_stock_quote_safety.py
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

The new `Stock position collection offline` CI runs these tests and existing
stock safety/strategy/server regressions on Linux with read-only repository
permissions and no provider secrets. Existing stock migration CI also applies.

## Remaining validation before activation

No persisted cross-run cache lifecycle is implemented or approved by this PR.
The memory lock handles shared callers within one process; separate processes
have independent cold caches. Cross-process locking, cache ownership, atomic
storage and invalidation require a later design without changing the portfolio
writer. Production retirement/correction behavior across a moving provider
snapshot, authorized recorded-response replay, actual stage latency and exit
freshness at scale still require review and evidence. A gap can increase the
window back to three days, and missing warmed symbols can cost an extra pass.
There is no claim of universal request savings or exact network-failure
granularity equivalence. Keep the production path on the full-window collector
until those questions are resolved.
