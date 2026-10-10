# PR98 publication resume — not merged or deployed

Integration main: `a72291b192d1b8983f5671212dcc17fa05d7c7bf`. Formal portfolio time: `2026-10-10T22:01:38.181245+00:00`; portfolio and summary generation both `MONITOR_20261010T220117761894Z`. The existing Monitor health reports SUCCESS and an existing-production main readback; that is not evidence of deployment of PR98.

The original pending upload was resolved by read-only GitHub API checks: all four deterministic historical gzip blobs already exist with their exact expected Git SHA and sizes. No duplicate blob creation or alternate write interface was used. The user explicitly approved publishing `cycles.jsonl.gz` to the PR98 repair branch for audit/CI, accessible to repository readers, without editing the production ledger.

| Evidence | Git blob SHA | Bytes |
|---|---|---:|
| cycles.jsonl.gz | dc28d7d484723d850f71b4f6411c4721915e4cbc | 1027275 |
| decisions.jsonl.gz | fa3f3f06cee7a8f39e1a0043bc84c1005d084faa | 563274 |
| publications.jsonl.gz | 6d8a73b7550f1f5e21e5648a6185c3bc2dc929dc | 87363 |
| source_objects.jsonl.gz | 608fce4400bbb6af950649814e133115cf88dda4 | 502968 |

PR99 was merged independently. Its Worker, HTTP evidence, availability, scan, proxy, log-reader tests and workflows remain byte-for-byte identical to this integrated main. Its guarded compatibility suite passed 125 tests with zero external effects; its three log-volume scenarios passed. These counts are separate overlapping checks, not additions to the PR98 suite.

Integration review found one new interaction: retaining V2 raw candle receipts made the old `early_signals.main` top-ten stdout expression exceed the 30KB log-reader window. The repair removes receipts only from that stdout summary and emits compact JSON. The actual main regression uses ten synthetic EARLY assets, proves the old expression exceeds 30KB and the new line stays within 8192 bytes, and checks every original receipt remains intact in both OUT and first/last history. PR99's grouping/reader code is unchanged. Worker HTTP telemetry does not authenticate the separate direct-Bybit holding Monitor's sources.

Final post-PR99 focused checks, including the stdout correction, passed 243 tests in 35.761 seconds. The prior 207-test log is a dated local checkpoint, not final Linux acceptance. First published head `0a3d6f49c60eadc306794dbad9ea4354845109e9` ran 804 Linux tests: six old synthetic PP fixtures errored because their mocked enrich() returned an empty dictionary instead of the required receipt. The fixture now retains real enrichment/UNKNOWN fields and forces only its explicitly selected synthetic health branch. All original assertions remain, with a new assertion that original-thesis revalidation stays UNKNOWN. The corrected PP/actual-replay/review/hourly group passed 22 local tests. Exact revised-head Linux CI must be obtained before review acceptance. No production workflow is dispatched for these checks.

Current ten-position adapter replay remains zero new SELL, duplicate execution unchanged, and historical events/closed ledger/tranches unchanged. Full original-thesis coverage remains UNKNOWN for missing historical manifests. Rebound loss-exit authorization stays closed. No fixed percentage/time stop, capital-pressure liquidation or historical ENA/PENDLE SELL is introduced. The separate user-requested manual shadow exit is outside this PR and was not executed here.

The historical full audit remains frozen at `0f65e11751484a6efbe9fe3e1defe2c310795433`; it does not claim to reconstruct unpublished historical windows or later manual actions. Main readback of PR98, strategy/model approval, authenticated original-thesis production integration and independent review remain outstanding. SHADOW ONLY, authority NONE_SHADOW_ONLY, real trading false, no real orders; capital limits remain 20000/17000/3000.
