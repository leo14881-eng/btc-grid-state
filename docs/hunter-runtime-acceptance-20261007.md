# Hunter runtime acceptance — 2026-10-07

This report separates merged code, executed tests, observed services, GitHub
readback, natural scheduler evidence, and sample-dependent acceptance. It is not
permission to enable a new strategy or a Fast Path portfolio writer.

## Delivered changes

| PR | Commit / merge | Result |
|---|---|---|
| #58 | `29c401c8f0f79be4a78f219a3082e1e946251e41` / `056af72583a7e580b66010d10f83bc6ea87a06d0` | Private public-GitHub readback cache, DynamicUser service, sanitized runtime health, coherent concurrent reads; deployed |
| #60 | `e726d4791ecb250b9b916b8578b39347e87f3caf` / `c430781c2dfc61d3bd6fa5d71c9baa637ac63610` | Bounded read-only MCP health and distinct job/version evidence; deployed |
| #61 | `853f65ee652ff9ee31e0189d32c0983c5abc00b1` / `afe59a744e604876c2a1a3acf6a85b79fcadd94e` | Per-venue subscription observation in the authoritative Monitor; naturally executed at 09:10 UTC |
| #62 | `687305d03b8bfab5ad42f616b7a1c0be9dd7109c` / `6e583529c530159d3f23c99a659c8e246e3c6925` | Align Sentinel evidence collector with existing BTC-only scope; 27 tests and Sentinel CI passed |
| #63 | `652ccff2a99f92621bad3b8c5b9ae29b3c03d1e2` / `b372774737c187319c1b96a7d0b6d7a83545ef57` | Structured public Sentinel evidence, required 15m input and read-only model-consumer bridge; deployed and actual MCP read verified |

| #64 | `dda63fec18e02e97282b6a87cb66ba693e1cc3ca` / `e541bd6eac8eabf5faabeef4a88ddd7b6870e388` | Lossless private venue-native review depth receipts; 550 local Hunter tests and CI 37605099348 passed; deployed |

The #61/#64 changes are **OBSERVABILITY_FIXED_BEHAVIOR_UNCHANGED**. Existing stream
subscription self-healing remains responsible for recovery. It neither writes
stream state nor changes BUY/SELL. Each PR used a freshly fetched main, touched
only its listed code paths, and merged with an expected head SHA. Concurrent
Hunter/stock state commits were retained.

Changed code: `scripts/hunter_market_stream.py`,
`scripts/hunter_fast_watch_bootstrap.py`,
`deploy/systemd/hunter-market-stream.service`,
`scripts/hunter_ops_health.py`, `scripts/hunter_ops_connector_patch.py`,
`research/hunter_fast_reconciliation.py`, and
`research/hunter_position_monitor.py`, and `scripts/sentinel_runtime.py` (BTC-only
evidence scope). New tests are in
`test_hunter_fast_watch_deployment.py`, `test_hunter_ops_health.py`, and
`test_hunter_fast_reconciliation.py` and `test_sentinel_btc_scope.py`.

## Server evidence

`hunter-market-stream.service` started at **08:45:34 UTC** on October 7.
Observed main PID 621077, zero restarts, DynamicUser `hunter-fast-watch`,
NoNewPrivileges and ProtectSystem=strict. Loaded code is `056af725…`; the
readback repository independently follows fresh main. No GitHub write
credentials are supplied to this public readback cache. The private runtime
state and minute archives are separate from formal portfolios.

Binance has 11 subscriptions: BTCUSDT plus the ten admitted current V2 model
routes. Legacy formal positions lack explicit historical execution identity.
Admission uses matching generation, full-quantity raw depth receipts, existing
fee model and exact liquidation recomputation **on a copy**. This proves a
current forward shadow model route; it does not prove or rewrite historical
entry venue. Historical entry venue remains UNKNOWN. There are currently zero
explicit Bybit execution positions; the Bybit resident component reports
NOT_REQUIRED. No Bybit position-level ARM/EXIT runtime sample is claimed.

At 08:58:35 UTC Binance had 288,933 accepted events, 16,926 old-event drops,
zero invalid events, zero reconnects, 63 REST fallback activations, 208 fallback
requests and zero both-source failures. Median reported price latency was
127.23 ms; median REST takeover was 240.92 ms. Session WS uptime was 99.73%.
These are a short live observation window, not 24-hour reliability or profit
improvement evidence. No review sample existed; improvement metrics are UNKNOWN.

At 09:11 UTC, both venue minute archives contained 27 records from 08:45 UTC;
all records retained OBSERVATION_ONLY, real_order_count=0 and formal_writer=false.

### Bybit public connectivity and fallback

A separate reference-market smoke test used official public Spot WebSocket
`orderbook.1.BTCUSDT`, V5 Spot ticker and 200-level REST orderbook. All succeeded;
REST HTTP=200 and retCode=0. This test made no position or trade.

An isolated real disconnect test at **09:06:03 UTC** used the production Watch
and BybitREST adapters with a reference symbol and an empty portfolio:
WebSocket accepted → socket closed → REST_FALLBACK_ACTIVE in **174.53 ms** →
new WebSocket → three valid events → recovery validated at 09:06:04.850691 UTC.
Two connections, one reconnect, one fallback, zero invalid/old/duplicate events,
zero real orders, no formal portfolio mutation. This verifies transport takeover,
not a Bybit holding's execution or a synthetic profit-protection SELL.

### Journal access

The connector previously lacked journal access. Installed only the service
read-group drop-in `/etc/systemd/system/shadow-ops-mcp.service.d/20-journal-read.conf`
with `SupplementaryGroups=systemd-journal`. Connector still runs as
`shadow-ops-mcp` with NoNewPrivileges; its Hunter/Sentinel unit allowlist is
retained. No root shell or write tool was added.

Actual MCP service_logs succeeded with returncode=0 and empty permission error
for Position Monitor, Watchdog, Discovery and market-stream. Logs included real
stage timings, success, persist validation and main readback messages, not only
systemd start/stop lines.

### Deployment version evidence

Actual MCP deployment_status after #60 distinguished:

| Field | SHA |
|---|---|
| base_checkout_sha | `19b7c6bbe63a5f5741f051cf62a5d469290d2703` |
| cached_origin_main_sha | `8ad1ef60ee78967f9e16d15c904cedf78f4b54eb` |
| discovery source/published job head at read | `654443631df248aca2936e624fbf13e9e829dc57` |
| research source/published job head at read | `7c5be6a7e981e3c3615a213c33bdb0a40f702dcc` |
| monitor source at read | `1b8563be2dc6d1b908a2bcee98698bd781bfd8e9` |
| watchdog source/published job head at read | `8bbf368fbf28538b5c559a88e625453dd6283bd2` |
| authoritative main readback at read | `7be30dd3f1cdbeedccd0439d7c9d592c96e74032` |
| evidence snapshot | `c430781c2dfc61d3bd6fa5d71c9baa637ac63610` |
| loaded Fast Watch code | `056af72583a7e580b66010d10f83bc6ea87a06d0` |

The endpoint does not fetch GitHub. Cached origin is explicitly not live. Job
source provenance is retained: discovery/research/watchdog currently publish a
job head, not a separately captured start head. Monitor uses the matching
scheduler state_revision and scheduler hash. Runtime freshness, source coherence
and shadow boundaries are checked before exposing evidence; missing/stale data
returns UNKNOWN instead of PASS.

## Natural Monitor acceptance

| Scheduled generation | Start UTC | Evaluation complete UTC | Main readback | Missed / duplicate |
|---|---|---|---|---|
| 2026-10-07T08:50:00Z | 08:50:16.675715 | 08:51:07.419352 | `93dca59ce337500c4de72d4949d390c17e74964d` | 0 / 0 |
| 2026-10-07T08:55:00Z | 08:55:18.147893 | 08:56:06.249803 | `7be30dd3f1cdbeedccd0439d7c9d592c96e74032` | 0 / 0 |
| 2026-10-07T09:10:00Z | 09:10:19.656165 | 09:11:00.790087 | `0765831c5e45dc526aa4afba4f5a2063e67fb50c` | 0 / 0 |
| 2026-10-07T09:15:00Z | 09:15:16.304858 | 09:15:57.692277 | `5495c17381ec8732b8e46e9762358775791af7b5` | 0 / 0 |

All were VULTR_SYSTEMD SUCCESS with verified readback. At 09:10 the newly merged
subscription observation returned MATCHED, all eleven Binance expected/actual
symbols equal, no missing/extra symbols, no unroutable positions. The new Monitor
source and Fast Watch readback source both included `afe59a74…`. The next natural
09:15 Monitor also returned MATCHED with verified readback, giving two consecutive
cycles after #61.

Natural 09:07 Watchdog persisted SUCCESS and observed per-symbol Binance REST
fallback healthy. The authoritative monitor was unaffected. Scheduler MCP
reported Monitor timer retained every five minutes, Discovery next at 09:17 UTC,
Blind Replay at 09:31, Watchdog at 09:37 and Sentinel evidence at 09:43. An inactive
successful oneshot is not a stopped schedule.

## Complete post-deployment natural hourly chain

Readback pinned main: `b809d25d894a72146f4e00915549ccde0f32800b`. Discovery and Research both use generation
`20261007T091721843154Z`. Discovery started `2026-10-07T09:17:20.652700+00:00` and completed `2026-10-07T09:17:40.611496+00:00`;
Research started `2026-10-07T09:18:03.497712+00:00` and completed `2026-10-07T09:21:54.564344+00:00`. Both report SUCCESS and
main_readback_verified=true. Discovery readback is `d444f59e8b642405c9a70ecb562fc56ea281632b`;
Research persisted and read back `d8ccc617c6bd1a79c94c1d0dedc30e1adab4c538` (28 accepted output files).

All 33 Research steps succeeded: complete regression, signal/research/identity/
liquidity/supply, authoritative capital decision, latest Monitor binding before
execution, V1 Broad/EARLY execution and integrity, V2 shadow execution and
integrity, mixed-generation rejection, closed-loop acceptance, persistence and
main readback. This natural cycle started after #58/#60/#61 were merged, and
ran with the resident Fast Watch. It is not a manual run or a pre-fix Research.

At the same pinned Research generation:

| Metric | V2 | V1 Broad Discovery |
|---|---|---|
| Realized net PnL USDT | 795.79 | 6406.95 |
| Open reference unrealized USDT | -1122.07 | -12456.26 |
| Estimated exit cost USDT | 24.34 | UNKNOWN |
| Full-depth MTM net PnL USDT | -375.09 | UNKNOWN |
| Open positions | 10 | 154 |
| Closed win rate | 100% | 90.7357% |
| Open loss capital exposure USDT | 14,000 | 167,000 |
| Thesis-invalidated open | 8 | 23 |
| Loss/recovery active open | 8 | 118 |

V2's 100% figure is **closed trades only**; its current full-depth portfolio MTM
is negative. MTM is calculated independently from complete liquidation receipts;
reference unrealized marks and same-receipt-mid exit-cost diagnostics are not
synchronous inputs to a fabricated arithmetic identity. V1 unknown depth/cost
remains UNKNOWN; V2 entry rules were not imposed on V1.

## Lifecycle and historical replay

Current code contains persisted protection states, generation ordering,
consecutive health/recovery observations, NONE-counter clearing with recovery
history, 20K/17K/3K allocator semantics, and full-depth MTM. These existing fixes
were not reimplemented. A fresh open-position audit at main `4213d995…` found
zero STRONG/reason contradictions and zero NONE active-counter residue. HUMA was
STRONG/NONE with both counters zero; HAEDAL had legitimate active persistent
invalidation. This is current evidence, not a permanent future guarantee.

The chronological read-only replay was re-executed against pinned
`c430781c2dfc61d3bd6fa5d71c9baa637ac63610`, covering **610 portfolio versions**, ENA,
PENDLE, HUMA and controls MOVR/PARTI/ETHFI. No future observation participates in
past evaluation. Ledger observations and existing exits are KNOWN; a 10bps
reference net model is INFERRED; historical depth/fillability is UNVERIFIABLE.

| Asset | First sampled arm UTC | First nonpositive after arm UTC | Positive sampled giveback | Existing ledger profit-protection exit |
|---|---|---|---|---|
| ENA | Oct 5 11:56:17, 0.2604 | Oct 5 13:36:24, 0.2533 | None observed | None |
| PENDLE | Oct 5 12:07:30, 2.560 | Oct 5 14:48:30, 2.464 | None observed | None |
| HUMA | None observed | Not applicable | None observed | None |
| MOVR | Oct 5 07:46:15 | None observed | Observed | Oct 5 10:14:43, +202.59 USDT |
| PARTI | Oct 5 10:04:22 | None observed | Observed | Oct 5 10:15:15, +43.76 USDT |
| ETHFI | Oct 6 12:35:17 | None observed | Observed | Oct 6 13:20:13, +13.06 USDT |

Legacy rows had no real persisted armed lifecycle. Historical MFE or retrospective
armed flags cannot prove timely live arming. ENA's first sampled breach was already
net-negative in the inference model (~-0.16 USDT); PENDLE's was ~-20.33 USDT. Sampling
gaps, missed timers, historical depth and exact execution windows cannot be
reconstructed from these references. Both remain
**UNVERIFIABLE_HISTORICAL_EXECUTION_WINDOW**. No historical SELL, realized profit
or principal release was created.

Loss Recovery stays diagnostic/evidence-driven; persistent invalidation does not
automatically add a loss SELL. No new stop %, holding timeout, BUY threshold or
runner/protection parameter was introduced. A seven-day evidence period remains
required before strategy conclusions. Fresh ARM/EXIT runtime acceptance is
**RUNTIME_ARM_EXIT_SAMPLE_PENDING**. All ten audited current positions were UNARMED.

## Tests and replay qualification

Executed baseline 525 Hunter tests before deployment work; #58 passed 532, #60
passed 540, #61 passed **544**. #61 GitHub CI succeeded: runs 37598603595 (stability)
and 37598603754 (server schedules). Sentinel runtime's **25 baseline tests** also passed; the BTC-only collector fix
passed **27 tests** including two new scope cases and CI run 37600869384. The
structured evidence channel then passed **34 Sentinel tests** and **8 ops tests**
(42 together); Hunter remained **544 passed**. #63 Sentinel CI run 37602365760
also succeeded.
Tests are not a substitute for natural runtime or live ARM/EXIT samples.

Natural Blind Replay at 08:31 UTC succeeded and read back
`31e8d089700dedc55a8692dced3c31e7adc88cce`. It reconstructs a partial archive
universe and has frozen calibration/OOS windows. The 90-day relative result is
negative; verified universe coverage and calibration remain incomplete. It does
not establish an overall improvement of the PR #35 lifecycle candidate. That
candidate remains unenabled; no strategy activation is approved by this report.

Seven-coin capture, AXS ADD and capital semantics investigation is in
`hunter-capture-capital-audit-20261007.md`; its explicitly pinned historical
findings are not claims about today's prices.

## Notification and Sentinel continuity

Actual automation lookup found the Hunter V2 notification and canonical Sentinel
analysis tasks disabled. Restored one of each, preserving their saved strategy
and notification prompts. Old duplicate Sentinel task stayed disabled. Both were
requested to run immediately; a request is not a successful scan/delivery.
Hunter notification remains hourly at local :20. Sentinel model analysis is now
hourly at :52, after the server :43 evidence timer and its bounded jitter; the
next configured start is October 7 16:52 Vietnam time.
The scheduling API returns next_run_time=null; natural trigger acceptance remains
pending rather than inferred from enabled=true.

The notification consumer really checked main and persisted
`.github/hunter-notification-runtime.json` at 08:59:51.762 UTC (commit `64269b4a…`).
Its source was `c430781c…`, no pending batch, no failure, and zero new qualifying V2
BUY/ADD/SELL after the user cutoff. It correctly stayed silent, without a holdings
report or historical backfill. A later actual cursor readback records
last_successful_check_at=09:42:48.360Z and preserves V2_NEW_TRADE_EVENTS_ONLY
with event_not_before_utc=2026-10-06T18:26:30+00:00. Delivery/phone push has no new-event sample and is
not claimed verified.

Sentinel server unit remains **evidence/preview only**. Current registry still has
writer=CHATGPT_AUTOMATION, full_leading_analysis_ported=false and the explicit
model transport, server-to-ChatGPT notification and cross-platform writer-lease
blockers. The server is denied formal Sentinel publication. Restoring the original
analysis task preserves that single writer; it does not migrate reasoning to the
server. Full migration and two genuine hourly analysis/persist/readback cycles
remain unverified. This window did not disable a task to manufacture migration. A fresh lookup
found the canonical task disabled again at 09:13:54 UTC after a last_run timestamp
of 09:12:50 UTC. The cause and completed analysis could not be retrieved. It was
restored again with an explicit prohibition on self-disabling before verified
handoff; its strategy prompt was preserved. This recurrence remains a scheduling
acceptance gap, not a completed migration.

### Structured model-input bridge (not full server migration)

#63 fixes a real input-channel gap: the long multiline journal preview could
not be consumed as a reliable complete JSON object. The collector now writes
only current public evidence to `/var/lib/sentinel-evidence/preview.json` with
an atomic replace and exact byte readback. It does not copy portfolio/account
state or publish formal Sentinel analysis. The MCP fixed-path alias
`sentinel-evidence-current.json` checks the preview/shadow boundary, aware clock,
10-minute artifact age, source/main SHA syntax, JSON scope, regular-file status,
size and no-follow open. Existing connector auth and write permissions are unchanged.

Actual deployed source `a8245c845e42b9c2002a233a3e157875e28a670b` includes #63.
Run `sentinel-20261007T094356.888171Z` produced the artifact at 09:43:56.888171 UTC.
The actual MCP runtime_file call fully parsed it: BTC spot, 15m/1h/4h/daily,
OI/history, funding and depth source gates were true. Treasury/ETF daily/date-only
gates were false and their original session/date evidence remained explicit;
this did not become synthetic intraday freshness. Missing DXY/liquidation/holder
proof remains a real gap. The collector status is ANALYSIS_FAILED and
confirmation_status=ANALYSIS_NOT_PORTED because no full server model engine was
introduced. It did not advance formal last_successful_scan_at.

The canonical model task was updated to consume the validated artifact first,
perform its existing full reasoning, independently complete/gate missing sources,
then retain CHATGPT_AUTOMATION admission, exact-path CAS and readback. The original
strategy and notification conditions were retained. Only this model task writes
formal Sentinel state; server publication stays denied. One immediate consumer
run was requested, and natural hourly cadence was retained at :52 after collection.
A requested or started task is not a completed model scan, readback or delivery.

Subsequent actual GitHub readback at main `60cf8feeacb60ffd910cc38cd39da9b7c7b0fb8d`
verified formal run `sentinel-20261007T164737+0700`, started 09:47:37 UTC,
last_successful_scan_at=10:01:18.945Z, run_status=PARTIAL_DATA and
last_persisted_at=10:02:04.209Z with persist_verified=true. It persisted
server_evidence_consumed.consumed=true and the exact server run/source SHA above,
plus a content summary hash. Main history contains the CAS retry and separate
verification commits `840349b9…` and `60cf8fee…`. This is evidence that the
original model task consumed the bridge and completed its formal state loop;
it is not a server model-engine migration or proof of notification delivery.
Account/fee sizing, second-source ETF confirmation, complete liquidation/systemic
sources and the latest 15m/1h refresh remain explicit data gaps. No executable
capital action was authorized. The scan began from the immediate request, so
it is not counted as two natural hourly cycles.

Two full natural model-analysis cycles and server model-engine migration remain
unverified. The operator evidence run is not used as natural analysis acceptance.

### Later current-state readback

A later fresh fetch pinned main `28f388f42d3df47f126085af2a744a356ceb9606`. V2 summary at `2026-10-07T09:50:43.084427+00:00` reports realized
net=795.79, reference open unrealized=-1200.52, estimated exit cost=25.35, full-depth
MTM net=-432.71 USDT; open positions=10, thesis-invalidated=8, active
loss/recovery=8. These supersede the earlier 09:21 hourly snapshot for
current PnL; they do not change the historical natural-chain proof.
Scheduler `2026-10-07T09:50:00Z` remains `HEALTHY`, missed=0, duplicate=0.
Monitor status=SUCCESS, main_readback_verified=True.

## Safety and outstanding acceptance

Observed real_order_count=0, real_trading_enabled=false,
capital_authority=NONE_SHADOW_ONLY, formal_writer=false for Fast Watch.
V2 capital_pool=20,000, ordinary cap=17,000, reserve=3,000. Ordinary used=14,000,
reserve used=0 and available=3,000 in the audited summary. Tail 4,000 remains
STRESS_DIAGNOSTIC_NOT_CAPITAL_ALLOCATION. RISK_OFF allocator availability=0 is
consistent with the existing regime cap; the strategic reserve still exists.

Earliest 24-hour Fast Watch observation completes **October 8 at 08:45:34 UTC /
15:45:34 Vietnam time**, provided service/archive coverage is actually checked.
No automatic formal activation occurs then. The service is enabled under systemd;
it does not depend on leaving the operator PC connected. Real ARM/EXIT, paired A/B profit
capture, Bybit holding execution, verified notification delivery, complete
Sentinel server migration, two real hourly Sentinel cycles and seven-day recovery
outcomes are still pending. Nothing in this report declares VERIFIED_COMPLETE.

Overall conclusion: **PARTIAL_FIX**. Hunter code/runtime/persistence acceptance above
is verified; sample-dependent observation and full Sentinel server migration are
not completed by these changes.

## Final post-restart acceptance

The #64 service update used a new immutable release, not an edit of the active
base checkout. It restarted at **10:07:29 UTC**, PID 656190, active/running,
NRestarts=0; the service validated **124 Fast Watch tests** before restart.
The actual MCP health read at 10:24:35.685983 UTC confirmed loaded code
`e541bd6eac8eabf5faabeef4a88ddd7b6870e388`, 11 expected/actual Binance symbols,
zero expected Bybit positions, formal_writer=false and all shadow boundaries.
Counters retained over process restart: 1,784,341 accepted events, 73,279 old-event
drops, zero duplicate/invalid events, 310 REST fallbacks / 1,224 requests and
zero both-source failures. WS session uptime=99.79%, median trade detection
latency=127.50ms, median REST takeover=243.46ms. The process restart is a real
coverage boundary; session uptime is not a claim of uninterrupted 24-hour uptime.
No live review/ARM/EXIT existed; improvement/capture metrics remain UNKNOWN.
Raw depth receipts are only for future accepted observation reviews and remain
private compressed runtime/archive records, never GitHub per-tick commits.

Two consecutive natural Monitor cycles after restart:

| Generation | Start UTC | Evaluation complete UTC | Readback | Missed/duplicate |
|---|---|---|---|---|
| 2026-10-07T10:10:00Z | 2026-10-07T10:10:18.313240+00:00 | 2026-10-07T10:11:01.668761+00:00 | `1bcfe6b64fb03fffddb1232edf93a911813ef7e7` | 0/0 |
| 2026-10-07T10:15:00Z | 2026-10-07T10:15:18.287985+00:00 | 2026-10-07T10:16:04.304801+00:00 | `a72bfc92ef2b0dc0267e411bf56cb9ddee7eba47` | 0/0 |
| 2026-10-07T10:20:00Z | 2026-10-07T10:20:14.945022+00:00 | 2026-10-07T10:20:58.965249+00:00 | `699bed2c6ff0f2dfc3aa6d25a59f7f09c4c4c05e` | 0/0 |

Each corresponding later health commit was read separately and verified against
that exact state commit, generation and scheduler hash; a prior generation
health file inside the state commit was not used as same-cycle proof.

A second complete natural Hunter cycle started **after #64 deployment**:

- discovery: generation `20261007T101719441380Z`, start 2026-10-07T10:17:18.187903+00:00, complete 2026-10-07T10:17:40.218471+00:00, status=SUCCESS, source/job head `6a8b639fcda952a8328b9732d3d41f420c3d6906`, verified main readback `ee464d9dad4c6a6c5c4ba0156e9d8ee1926cdcc9`.

- research: generation `20261007T101719441380Z`, start 2026-10-07T10:18:01.196007+00:00, complete 2026-10-07T10:21:59.111130+00:00, status=SUCCESS, source/job head `6c7e6421bbdea532859cd944991a6b66a73255f9`, verified main readback `6c7e6421bbdea532859cd944991a6b66a73255f9`.

All 33 Research steps again succeeded, including Capital Review, V1/V2 execution,
latest Monitor binding, generation guard, persist and authoritative readback.
The actual server regression ran 550 tests with one environment-specific skip;
local full regression had 550 passes without skips. Published job heads contain
the #64 merge (actual ancestry check). Start-head provenance is still not
separately recorded for Discovery/Research.

At pinned main `2285a480d608ffab0fd677839a8f3531bc5a80d5`, V2 as_of=2026-10-07T10:21:36.480314+00:00:

- realized_net_pnl_usdt: 795.79
- open_unrealized_pnl_usdt: -1205.04
- estimated_exit_cost_usdt: 24.65
- mark_to_market_net_pnl_usdt: -425.36
- open_positions: 10
- thesis_invalidated_open_count: 8
- loss_recovery_open_count: 9
- open_loss_exposure_usdt: 14000.0

Recovery breakdown: PERSISTENT_INVALIDATION=8, LOSS_RECOVERY=1, NONE=1.
HUMA is currently WEAKENING/NONE with both active counters=0. HAEDAL is
THESIS_INVALIDATED/PERSISTENT_INVALIDATION with active recovery_observations=0;
its invalidation count is an active fresh lifecycle count, not NONE residue.
All ten protection states are UNARMED. No confirmed health contradiction or
NONE active-counter residue was found. V2 closed_win_rate=100% describes only
19 closed winners; full-depth MTM is negative. Capital remains 20K/17K/3K,
ordinary used 14K, reserve used 0, reserve available 3K; allocator=0 in RISK_OFF.

Final operational classification: journal/structured-input/private-depth/version
issues are corrected; existing lifecycle and capital fixes are ALREADY_FIXED
with new code/test/runtime checks. Full server model migration is still
RUNTIME_PENDING; actual model-input consumption has now been persisted.
No new qualifying notification event exists, so silence is NOT_A_BUG; actual
delivery receipt remains sample-dependent.

Conclusion remains **PARTIAL_FIX**.
