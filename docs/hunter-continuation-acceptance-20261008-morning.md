# Hunter continuation acceptance — 2026-10-08 morning
Conclusion: PARTIAL_FIX. All times UTC unless explicitly +07. This is a dated evidence snapshot, not a claim of continuous background development.

## A. Authoritative baseline
Fresh main at final evidence read: 2110dd318c19cc630e70f7e126d01f0e3471cde8. Initial work baseline 5ef1e13e; later fixes independently revalidated against advancing main. State writes and other-window commits were preserved through normal PR merges, never branch force replacement.

## B. Confirmed bugs and changes
| PR | Merge commit | Correction | Verification |
|---|---|---|---|
| #76 | d4a75692f40ffa4766d1efc98e571d1fad9c1d07 | Current shadow execution identity admitted by existing authoritative writer from fresh full-quantity Binance receipt; legacy historical entry venue remains UNKNOWN | 8 new tests; 594 full Hunter tests; CI success; five files exact main readback |
| #77 | a086c5b3bddd0cf2e91b60fd23297bd3011b415a | Explicit Bybit/mixed/partial venue cannot be managed with Binance marks, health, fees or depth; HOLD fail closed | New test failed before; 9 identity tests, 595 full Hunter tests; CI success; two files exact readback |
| #78 | 278cde7350200843ef7fdfae0c0cacf28c0d698f | CAS retry changes cwd back to source before removing generation worktree | Real local git fixture reproduced exit128 before; 6 retry tests, 596 full Hunter tests; all three CI workflows success |
| #79 | 5521ac362894debbc28e39cba4fb21a2475f0466 | Missing buckets counted by admitted generation distance instead of rounded completion elapsed time | Four new regressions failed before; 15 scheduler +6 retry tests; 600 full Hunter tests pass; three CI workflows success |

Files: research/hunter_execution_identity.py (new), research/hunter_fast_watch.py, research/hunter_lifecycle_state.py, research/hunter_shadow_trader_v2.py, research/hunter_scheduler_health.py, scripts/hunter_monitor_runner.sh, tests/test_hunter_execution_identity.py (new), tests/test_hunter_monitor_retry.py, tests/test_hunter_scheduler_health.py.

Actual 00:30 natural Monitor FAILED after a safe CAS rejection prevented stale publication. Old launcher removed its current working directory, then restarted git inside an unlinked cwd. Next natural 00:35 restored processing. Historical 00:30 is not relabelled successful. Old 00:35 missed=0 was incorrect: completion gap591.970145s straddled a failed bucket. PR79 now records GENERATION_BUCKET_DISTANCE. Latest counters describe the latest successful interval, not cumulative historical uptime.

Wrapper installed atomically at00:35:19 then updated00:43:58; current SHA256 8829f7765684a797564a496432a96946ef593806da14650f5a98a7ce61cc9b61. Backups retained. Server isolated validation: 9 identity +134 Fast Watch +37 Monitor tests; final wrapper validation15 scheduler +6 real-git retry tests, all pass. No manual run substituted for natural acceptance.

## C. Health/recovery
ALREADY_FIXED: persistent recovery and history separation remain present; no additional recovery strategy or automatic loss exit added. At MONITOR_20261008T005009692912Z all10 open positions share recovery evidence generation. IO is STRONG/NONE with active recovery and persistent counters both0. HUMA is WEAKENING/LOSS_RECOVERY, active recovery count0, not conflicting STRONG. HAEDAL is THESIS_INVALIDATED/LOSS_RECOVERY with persistent count2 and recovery count0; this is active fresh deterioration, not stale NONE recovery count3. Transitions remain retained. Prior recovery reset/invariant tests are included in600-test full suite.

## D. Journal
Journal read permission now works through the existing read-only connector; Monitor actual stdout/stderr was read this session, including the real failure and both successes. Discovery/Watchdog journal read access was already repaired and verified in earlier runtime acceptance; no new root connector permission was added. Current Watchdog service00:37–00:37:12 SUCCESS. This session's scheduler_status and deployment_status were actually called.

## E. Version observability
Actual deployment_status at00:50:
base_checkout_sha=19b7c6bbe63a5f5741f051cf62a5d469290d2703
cached_origin_main_sha=b661483e05ad7c3491a89715b47c88f44bc0fd36 (cached, not live main)
discovery published source=f63d789f7a9e84ab6b0e2fb3cc4d88001c690432
research published source=1e0773bafeb8c02b6f26986d57a87d0bffc9c8f5
monitor admitted source for00:45=b661483e05ad7c3491a89715b47c88f44bc0fd36
watchdog published source=949ccc24ce9b5167bc926dd7045fdf4b9caf3df4
authoritative main readback for00:50=88efe23ebea55814c7470572a79597301743a1be
Fast resident loaded code=403cbf8b3898cbf04aec2a0c74e588cf2de6fbbe; runtime ledger source is a different field. Resident was not restarted in this round. Published job source does not claim a separately captured job-start source for Discovery/Research/Watchdog.

## F. Natural full chain
Discovery/Research generation20261008T001707969039Z.
Research00:17:41.508883–00:22:02.669102 SUCCESS; all33 steps SUCCESS, including capital decision, V1/V2 shadow execution, persist and authoritative readback.
readback1e0773bafeb8c02b6f26986d57a87d0bffc9c8f5 verified=true.
This chain contains earlier PR71–74 fixes. It precedes PR76–79 and is NOT proof of a full hourly chain after all four new fixes. Next natural hourly cycle after these fixes remains pending.

## G. Two consecutive real Monitor cycles
| admitted generation | start | monitor complete | source | main readback | result |
|---|---|---|---|---|---|
| 2026-10-08T00:45:00Z |00:45:02.113481|00:45:51.452857|b661483e05ad7c3491a89715b47c88f44bc0fd36|8502c54963023587fb91f1b1e335cf7a83111b4f|SUCCESS; missed0 duplicate0; journal final00:46:05|
| 2026-10-08T00:50:00Z |00:50:04.135051|00:50:58.411889|cdb3a0b6e62ba1841f840791bd50632ab3970353|88efe23ebea55814c7470572a79597301743a1be|SUCCESS; missed0 duplicate0; journal final00:51:12|
Both source trees include PR76–79; natural timers were retained. Each run executed127 pre-mutation tests. Both journal main readback and primary proof verified. Latest GitHub Monitor health generation00:50 SUCCESS, main_readback_verified=true, authorityNONE_SHADOW_ONLY, real_trading_enabled=false. Final V1 open158, V2 open10. V2 events remain64: no invented V2 SELL/ADD.

## H. Protection, historical replay and candidate
RUNTIME_ARM_EXIT_SAMPLE_PENDING. Existing protection lifecycle remains; no fake new ARM/EXIT or synthetic ledger trade created.
Historical audit freshly rerun against8e3afa028dcb4561c5a64888c9ff669fa3170ebf,807 portfolio versions. ENA first sampled ARM Oct5 11:56:17Z, first sampled nonpositive after ARM13:36:24Z; PENDLE first sampled ARM12:07:30Z, nonpositive14:48:30Z. Neither has a sampled positive protected-floor breach or a historical full executable depth receipt. Both remain UNVERIFIABLE_HISTORICAL_EXECUTION_WINDOW. HUMA753 observations has no sampled ARM. Known ledger profit-protection controls: MOVR202.59, PARTI43.76, ETHFI13.06 USDT, but known ledger events do not prove exchange fills. Chronological observations checked without future decision data. Audit details are in hunter-lifecycle-audit-20261008.json.

Draft PR35 remains disabled/unmerged. Frozen head95a6ff51935c810a1c8f011729110b9760bf922d's13 added files were overlaid only into a separate latest-main worktree b661483e; no main file overwritten. All78 candidate tests passed. Offline replay read809 historical commits with0 read failures,33 recoverable position IDs and0 historically executable-complete cases. Frozen chronological holdout is not independent robust validation. Result: NOT_ENOUGH_EVIDENCE_TO_REPLACE_CURRENT_V2; profitability/comparison metrics UNVERIFIABLE, valueNULL, denominator0. No parameter changes or activation.

## I. V2 current snapshot
GitHub read2110dd318c19cc630e70f7e126d01f0e3471cde8; summarygenerationMONITOR_20261008T005009692912Z.
realized_net_pnl795.79; open_unrealized_pnl -1316.21; estimated_exit_cost24.95; mark_to_market_net_pnl -540.60; open10; closed19; closed_win_rate1.0 applies only closed trades, not the whole portfolio. Open_loss_exposure14000; thesis_invalidated3; loss_recovery9 (includes persistent); persistent1.
MTM uses full-quantity liquidation estimates; reference MTM -520.42 uses marks. These different bases must not be mixed in a simple subtraction.
capital_pool20000;ordinary_cap17000;reserve3000;ordinary_used14000;reserve_used0;reserve_available3000. RISK_OFF max_deployable12477.47 below used14000 explains allocator_available0; reserve still exists. Tail4000 remains STRESS_DIAGNOSTIC_NOT_CAPITAL_ALLOCATION.

## Capture investigation
Known formal ledger at e9b1f6c3ad7e2dd46636b5f08b5719cf1213a946:
| asset | V1 | V2 |
|---|---|---|
| GAIB | no recorded trade | no recorded trade |
| MOVR | BUY Oct4 13:02:36Z; SELL Oct5 10:14:43Z | BUY Oct5 07:06:25Z; SELL10:14:43Z |
| FLUID | no recorded trade | no recorded trade |
| NIGHT | BUY Oct4 09:53:51Z; SELL Oct6 00:05:13Z | no recorded trade |
| CAP | no recorded trade | no recorded trade |
| VTHO | BUY Oct4 14:50:14Z; SELL Oct6 00:15:09Z | no recorded trade |
| FIL | BUY Oct4 17:36:18Z; SELL Oct5 22:36:48Z | no recorded trade |
AXS V1 BUY+2ADD, entryOct4 10:59:16Z, stillopen; V2 no trade. Existing capture audit documents earlier tiny ADD issue; no new ADD manufactured to demonstrate a fix. FLUID/CAP formal Bybit execution/management integration is still unavailable: Fast Watch Bybit adapters do not automatically enable formal BUY. Historical missing gating evidence is not reconstructed by guessing.

## J. Safety
real_order_count=0 in both natural scheduler receipts.
real_trading_enabled=false.
capital_authority=NONE_SHADOW_ONLY.
SHADOW_ONLY; no real order endpoints used. No changes to V1 Broad/EARLY, BUY thresholds, core Discovery, profit parameters, loss strategy, cap, BTC Sentinel strategy or stocks.

## K. Remaining verification and capability boundaries
1. Full natural hourly chain after PR76–79 pending.
2. Fast Watch observation remains OBSERVATION_ONLY. Original24h checkpoint Oct8 15:45:34+07; newest dispatcher24h checkpoint Oct9 05:15:40+07. One-time read-only acceptance task Oct8 15:50+07 is enabled. These are different baselines.
3. Real new fresh ARM/EXIT and venue-matched full-depth/notification sample pending; no actual Bybit primary held-position sample. Binance11 subscriptions; last read had NEXOUSDT REST fallback healthy. Bybit0 NOT_REQUIRED is not Bybit live held-position acceptance.
4. Full Bybit formal Research/BUY/authoritative manager not implemented in this round; explicit incompatible venue now safely HOLDs rather than fake Binance execution.
5. Sentinel server still EVIDENCE_PREVIEW_ONLY; full analysis remains ChatGPT writer. Actual tools expose no callable model inference or server-to-ChatGPT notification endpoint; examined service environments had no configured model/notification/writer-lease variables (not a claim all server secrets absent). Authorized model account/endpoint, verified delivery ACK and cross-platform writer handoff remain missing. Root/SSH access cannot create those capabilities. Old canonical Sentinel task stays enabled; full migration/two server model hours NOT verified.
6. V2 notification task remains enabled, only post-cutoff BUY/ADD/SELL, no holdings spam. No qualifying new V2 event in examined ledger; actual new notification delivery ACK pending. Queue/preparation is not delivery.
7. Required7-day observation statistics and independent complete holdout remain sample/time dependent.

CODE_DONE / TESTS_PASS / RUNTIME / GITHUB_READBACK / NATURAL_TIMER are separate evidence categories above. Successful new Monitor runs do not erase a prior failure or prove missing hourly, model, delivery, Bybit or24h samples.
