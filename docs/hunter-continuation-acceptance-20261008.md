# Hunter continuation acceptance — 2026-10-08 (+07)

Conclusion: **PARTIAL_FIX**. This report distinguishes code, tests, installed runtime, GitHub readback and natural scheduling. No strategy activation is authorized by it.

## A. Baseline and commits

Initial authoritative main: `68fad96def04e1ab9c0f4b69085ce9cfa6d90f96` (read and freshly fetched at execution).
Recovery/readback evidence main: `77399a8f9ba8b0675e9b340983bc85e6c5dd861f`.
Main continuously advances with legitimate server and other-window state commits. Each changed path was re-read against current main; only selected code/test paths were committed, with normal expected-head PR merges.

| PR | Feature head | Merge | Change |
| --- | --- | --- | --- |
| [71](https://github.com/leo14881-eng/btc-grid-state/pull/71) | 85b23b18170a10682080da3f97d441298154966c | eaf65b1455eab49a75b9f839a452f616388004a1 | Coalesce pending reviews to newest validated quotes; withdraw expired unarmed windows |
| [72](https://github.com/leo14881-eng/btc-grid-state/pull/72) | 787812f65382dc5de41663b3ecafee1e13dadbce | 403cbf8b3898cbf04aec2a0c74e588cf2de6fbbe | Independent bounded probe/depth dispatch; armed priority, stale rejection and shutdown cleanup |
| [73](https://github.com/leo14881-eng/btc-grid-state/pull/73) | 6aacf5bf20f2570cf465fc01158de065f92c07fe | d508662162edd361f7f006810cee2ac15cd5dfa3 | At most two complete fresh-main Monitor reruns after exact protected-input CAS conflicts; validate CI against pinned Monitor publication |
| [74](https://github.com/leo14881-eng/btc-grid-state/pull/74) | bd89797f234bd9dfee4a6403533f48a29302ec5b | b03b59fa4a19583270be629778a2bdb6aa1f5c11 | Recognize the exact main-ref SHA lock race, retaining bounded clean-checkout CAS retry |

Changed code/test files:
`research/hunter_fast_watch.py`, `scripts/hunter_market_stream.py`,
`tests/test_hunter_fast_watch.py`, `tests/test_hunter_fast_dispatch.py`,
`scripts/hunter_monitor_runner.sh`, `tests/test_hunter_monitor_retry.py`,
`.github/workflows/hunter-vultr-runner-validation.yml`,
`scripts/hunter_job_runner.py`, `tests/test_hunter_aux_persist.py`.
All merged file contents were exactly read back.

## B. Findings

**CONFIRMED_BUG**: queued triggers retained the first tick despite newer valid evidence; all stale-symbol probes blocked depth reviews; Monitor had no immediate complete fresh rerun after safe CAS rejection; exact main-ref concurrent-update errors were incorrectly classified UNKNOWN. These behaviors were corrected.

**OBSERVABILITY_GAP**: historical position execution venue remains UNKNOWN in the formal legacy ledger; read-only Fast Watch routes use fresh full-quantity current-model receipts with explicitly unknown historical execution. The current ten positions route to the current Binance shadow model, not to a venue guessed from asset or Bybit availability labels. Forward formal venue admission and a real Bybit primary-position sample are not proven here.

**RUNTIME_PENDING**: 24-hour paired A/B; actual new ARM/EXIT; natural full chain after all four patches; a naturally occurring production CAS/ref-lock retry branch; complete server Sentinel model/notification/writer migration.

**NOT_A_BUG**: CAS refused old Monitor output after a protected input changed; RISK_OFF allocator availability zero despite a 3K reserve; no V2 trade notification without a qualifying new ledger event; WS degradation with fresh same-venue REST fallback is not a Hunter failure.

## C. Lifecycle and recovery

Persistent protection, health generation ordering, distinct fresh deterioration cycles, active NONE counters, historical recovery separation, MTM and 17K/3K separation remain existing implementations, covered by the full regression suite. No redundant lifecycle/strategy rewrite was made.

The fixed-main V2 ledger at `3e0a755d007504a4155daad204807f7593adac47` was read through its actual blob `03d952f3f3031cb78e1b641477cc53c80656245e` because fetch_file returned an empty large-file body. It parsed completely: 10 open positions, 64 actual events, generation MONITOR_20261007T222509197894Z. No STRONG multi-factor conflict was present. ENJ was NONE with both active counters zero; HAEDAL was LOSS_RECOVERY with recovery observations zero. Historical transitions were not erased or converted into SELLs.

ENA/PENDLE remain **UNVERIFIABLE_HISTORICAL_EXECUTION_WINDOW**. No historical profitable exit was fabricated. Historical replay/holdout improvement is not newly proved by this operational continuation; Draft PR35 stays inactive.

## D. Journal and operational recovery

Actual MCP reads of Monitor, Watchdog and Discovery journals returned code 0 and real stdout/errors. Existing minimal read-only journal access remained in place; the connector did not acquire root write/shell privileges.

Natural Monitor 22:15 UTC failed when PR72 added a protected test during its execution:
`SHADOW_STATE_CAS_REJECTED_STALE_WRITER tests/test_hunter_fast_dispatch.py`.
Old results were not published. Authorized manual fresh-main rerun started **22:18:26**, completed service **22:19:28**, and read back generation 22:15. This is manual recovery, not a successful original natural run.

Blind Replay 22:31 computed and persisted its five output files successfully, but health push failed because main changed from expected c415daba6b757cbb304a364a0b855dafbf930ebd to actual 00acc98b059c0388967940c3722266d1244b3016 during ref update. This newly captured diagnostic does not establish the cause of older historical failures.

After PR74, authorized manual recovery started service **22:41:35**, runner **22:41:38.655615**, completed runner **22:42:26.967124**, service **22:42:33**. Main health was actually SUCCESS/readback_verified=true; source b03b59fa4a19583270be629778a2bdb6aa1f5c11, readback 0a4ce8030d105e3aa1705998fb130552c4ccc1f5. No production failure was injected.

## E. Deployment evidence

MCP deployment_status read at approximately 22:44 UTC:

| Field | Observed SHA |
| --- | --- |
| base_checkout_sha | 19b7c6bbe63a5f5741f051cf62a5d469290d2703 |
| cached_origin_main_sha | d508662162edd361f7f006810cee2ac15cd5dfa3 |
| discovery source | 403cbf8b3898cbf04aec2a0c74e588cf2de6fbbe |
| research source | ef5605c34474747fb907e6437034d19fc28b58ce |
| monitor source | bade38470f557edc64fc070db49ceec2bed70e0d |
| watchdog source | 2d0bcdd96dba3239ebb87e17bb673a4c801902f6 |
| authoritative job main readback | a108f65ba79c807b84a43059c9ad9c2c7eada612 |
| loaded Fast Watch code | 403cbf8b3898cbf04aec2a0c74e588cf2de6fbbe |

Cached origin is not a live GitHub lookup. Discovery/Research/Watchdog source values retain the view's explicit **PUBLISHED_JOB_HEAD; START_HEAD_NOT_SEPARATELY_RECORDED** caveat; do not silently treat them as independent start-SHA proofs.

PR71 resident release passed 128 server Fast Watch tests and loaded **22:09:17**, PID 1339181.
PR72 release passed 134 and loaded **22:15:40**, PID 1350048. It remains the resident Fast Watch code.
PR73 installed the root-owned Monitor launcher atomically **22:28:20**, after 27 server related tests; SHA256 b967ea862607e7ca6c743ae3068df01467bc0f9c1f057819000dc716bc1c3c56. Existing 240s timeout, single host lock, generation admission, protected-input CAS and exact readback remain.
PR74 is loaded by each scheduled job's existing fresh-main isolated checkout; its manual Blind Replay source was actually verified.

## F. Complete natural hourly chain

Post-PR71/72 natural generation **20261007T221704461181Z**:
Discovery completed **22:17:22.690559**; Research **22:17:31.195797–22:22:04.202313**.
All **33** actual steps succeeded, including Capital Review, V1, V2, mixed-generation guard, persist and main readback. No manual hourly invocation was used.

This full chain preceded PR73/74; it is not mislabeled as full-chain evidence after those later operational patches.

## G. Two natural Monitor cycles after PR73 installation

| UTC generation | Admission start | Monitor completion | Verified readback completion | Status | Missed / duplicate |
| --- | --- | --- | --- | --- | --- |
| 2026-10-07T22:30:00Z | 22:30:02.860316 | 22:30:49.684048 | 22:30:59.359031 | SUCCESS | 0 / 0 |
| 2026-10-07T22:35:00Z | 22:35:07.155489 | 22:35:51.999084 | 22:36:00.999647 | SUCCESS | 0 / 0 |

Main proofs: 1d8932f840e1b4b077016b04885909789dc7c0d3 and 47428ea238a0c6d2c9f6cb286cf57ab3b49a5b62.
These were observed, not scheduled promises. PR74's next natural Replay/retry acceptance remains separate.

## H. Fast Watch A/B and profit protection

At **22:40:33.802592 UTC**: Binance 11 expected/actual symbols (ten positions plus BTC); Bybit **NOT_REQUIRED**, zero actual primary positions. Binance WS degraded for individual symbols but same-venue REST was fresh; aggregate PARTIAL_FAST_PATH_DEGRADED. Do not claim a live Bybit-held-position acceptance from adapter tests.

Counters persist across controlled deployments; session connection-time uptime is not a verified 24-hour market-data coverage measure.
Events 10513926; connections 4; reconnects 0; duplicate drops 0; old-event drops 344847; stale incidents 3275; REST fallback episodes 3133; REST requests 11078; both-source failure count 1. Median timestamped price-event latency 119.02046203613281ms; recent median REST takeover 236.42659187316895ms.

Real observation review count **0**. **RUNTIME_ARM_EXIT_SAMPLE_PENDING**. Capture delta, arm/exit improvement and missed-profit windows remain UNKNOWN, not zero. False-trigger counter zero without review samples proves no accuracy improvement.

Actual gzip archive audit at 22:07 UTC: Binance 792 records, 08:45:38.348802–22:07:40.642352, largest gap 62.00831s; Bybit 802 records, 08:45:37.326213–22:07:28.332504, largest gap 60.52983s; zero parse errors. This is approximately 13h22m, not 24h.

Earliest 24h since actual first deployment: **2026-10-08 08:45:34 UTC / 15:45:34 +07**. A real one-time read-only acceptance task was created for **15:50 +07**, is enabled, and has not yet run. Task creation is not acceptance or an always-running development agent. It must inspect real evidence and remain observation-only. Archive windows must be segmented by loaded code version: PR72 did not load until 22:15:40 UTC, so the first-deployment 24h checkpoint will still be less than 24h under that revised dispatcher. A complete 24h window of PR72 itself cannot occur before 2026-10-08 22:15:40 UTC (2026-10-09 05:15:40 +07); do not conflate them.

## I. V2 snapshot and notifications

Fixed main b03b59fa4a19583270be629778a2bdb6aa1f5c11; summary as-of **22:40:31.409156 UTC**:

| Metric | USDT / count |
| --- | --- |
| realized_net_pnl_usdt | 795.79 |
| open_unrealized_pnl_usdt | -1375.31 |
| estimated_exit_cost_usdt | 24.48 |
| mark_to_market_net_pnl_usdt | -597.97 |
| open positions | 10 |
| thesis invalidated | 3 |
| loss recovery / persistent total | 9 |
| ordinary used | 14000 |
| strategic reserve used / available | 0 / 3000 |

MTM uses full-quantity liquidation estimates; ticker marks and depth-mid exit costs have different reference observations, so do not fabricate a simple arithmetic equality between those separate fields. Realized wins are not whole-portfolio profitability.

Hard principal cap 20,000; ordinary cap 17,000; reserve 3,000. Tail 4,000 remains **STRESS_DIAGNOSTIC_NOT_CAPITAL_ALLOCATION**. RISK_OFF allocator availability zero is not a missing strategic reserve.

The fully parsed 64-event V2 ledger contains no event after the user's **2026-10-06T18:26:30Z** cutoff (latest actual event is 16:30:20.146257Z that day). Current silence is correct. The notification consumer was enabled and actually checked at 22:03:33.816Z; last task run 22:03:50.476827Z. Delivery of a future qualifying message still requires an actual message/batch receipt, not a queued or prepared status. This continuation sent no holdings notification or historical batch.

## J. Tests and safety

Baseline Fast Watch **124** passed. PR71 added four reproductions, then **128 / 573 full Hunter** passed.
PR72 reproduced two dispatcher faults and added six integration tests, then **134 / 579** passed.
PR73's unchanged old runner failed three new assertions; **27 related / 584** then passed. The first Infrastructure CI snapshot check failed MONITOR_OPEN_MISMATCH; its corrected pinned-snapshot rerun and the other two CI workflows succeeded. CI full regressions reported one skipped optional transport test; resident server Fast Watch transport tests were actually run.
PR74's two regression tests reproduced failure; after fixture corrections **26 related / 586 full Hunter** passed. CI 37697631720 succeeded.

No test fixture was installed as production price/trade evidence. Code and installed-file/readback checks are separate from runtime samples.

**real_order_count=0; real_trading_enabled=false; capital_authority=NONE_SHADOW_ONLY.**
V1 remains Broad Discovery/EARLY. No BUY gates, selection, profit/recovery parameters, price/time stops, 20K ceiling, BTC strategies or unrelated stock code were changed.

## K. Still unverified

- Full server Sentinel analysis/notification/writer transfer remains **EVIDENCE_PREVIEW_ONLY / full_leading_analysis_ported=false**. The existing canonical ChatGPT task stays enabled; it actually consumed live server evidence and persisted PARTIAL_DATA scans, most recently 22:33:42.258Z. That is hybrid operation, not complete server migration or two server-model SUCCESS hours.
- No callable authorized server model transport, verified server-to-ChatGPT notification transport or acknowledged cross-platform writer lease is proved. Root/SSH/GitHub access does not supply them; no second formal Sentinel writer was enabled.
- Actual new protection ARM/EXIT, venue-matched hypothetical/full-depth event-consumer end-to-end samples and paired profit-capture improvements remain pending.
- Real Bybit primary-held-position sample and historical/formal venue provenance are not proved; secondary prices are not execution substitutes.
- Draft PR35 remains inactive; complete holdout/historical performance improvement is unproven.
- No production failure was intentionally created to claim automatic CAS/ref-lock recovery; manual recoveries and isolated fault tests are explicitly separate.
- 24h observation and natural whole-chain acceptance after the last operational patch are future evidence, with a real scheduled check rather than a fabricated completion.

**PARTIAL_FIX**
