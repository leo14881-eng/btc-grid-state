# Hunter V2 lifecycle repair — draft, not deployed

## Evidence and deployment boundary

Read-only baseline: `3c8a47a418dbbe13184553f68ed71c9b05aa6d6d` (2026-10-10 18:12:11 UTC). Its portfolio is from 18:11:38 UTC. The branch was rebased after checking each intervening main change; intervening changes were published state/health, not source. This PR does not modify `research/results`, V1 configuration, Sentinel, workflows or production services. PR35 is unrelated and must not be merged as part of this change.

The baseline runtime health records a completed Monitor, verified main readback at `2a25ed82dfd184a6c5431ed0dcc34243365d8dc2`, 304.293 seconds since the previous success, zero missed buckets and zero duplicate triggers. This proves the existing Monitor execution; it does **not** prove this draft's new fields have been deployed.

## A. Proven findings

The former formal position already has `health_state`, `degraded_cycles`, `protection_lifecycle`, `recovery_state` and lifecycle snapshots. The gap is a durable final decision receipt that joins those states with this cycle's evidence, reasons and final action. Bounded decision logs and “still open” are insufficient to answer why a position remains held.

The new `last_monitor_decision` and summary `position_monitor_states` reuse these fields. They distinguish confirmed health from `EVIDENCE_PENDING`, attach generation/source/check times, include complete reasons, and preserve the receipt after a close. A persistence check rejects projection/generation mismatches before the existing CAS writer runs. Missing/freshness-invalid evidence is `PARTIAL_DATA`, never invented deterioration or hard invalidation.

Historic original BUY evidence is attached only after exact shadow ID, asset, opening time and first-tranche identity checks. All ten entries cite an actual first-parent-main BUY commit/blob. Six old entries lack signal timestamps; those timestamps remain absent. No historical fill, event or SELL is created.

## B. Partially proven historical exit gap

Classification: `HISTORICAL_EXIT_GAP / ROOT_CAUSE_PARTIALLY_PROVEN`.

The full historical audit through `3c8a47a418dbbe13184553f68ed71c9b05aa6d6d` examined 1,533 published portfolio snapshots, 3,066 ENA/PENDLE rows and 6,982 unique target decisions, across 21 deployed code/rules combinations. It replayed 670 legacy rows with the corresponding old function and 2,396 persistent-lifecycle rows with the actual previous state and saved liquidation estimate. All 2,396 matched. No published cycle both met the actual positive-net protection SELL condition and failed to exit.

| Asset | First recorded MFE >= 2%, UTC | Peak | Last relevant positive window / subsequent observation |
|---|---|---|---|
| ENA | Oct 5 11:56:17.090224; 0.2604; +3.0471% | 12:01:19.446565; 0.2608; +3.2054% | 12:14:50 at 0.2599; next 13:36:24 at 0.2533, estimated net -0.1590738222 USDT |
| PENDLE | Oct 5 12:07:30.437823; 2.56; +2.0327% | 12:09:53.689543; 2.576; +2.6704% | 13:42:39 at 2.526, giveback 1.992839% (<2), net +4.319298; 13:52:06 at 2.527; next 14:48:30 at 2.464, net -20.3314526 USDT |

The legacy theoretical trigger reference prices were approximately ENA 0.2557460458 (+1.2054% raw) and PENDLE 2.525820336 (+0.6704% raw). They were reference-price/giveback thresholds, not guaranteed executable net floors. The actual legacy rule also required positive estimated net proceeds. At the first saved breach, each was already net negative. Therefore neither recorded breach should have generated a protection SELL under that deployed rule.

The respective 81m34s and 56m24s publication gaps could span a profitable exit window. Actions evidence does not show an actual Monitor execution in those windows. We cannot prove a missing execution, unpublished input or rejected write from an absent record. There were 21 publication gaps exceeding 600 seconds in the full history; none after Oct 8 in that audited range. We do not alter the current five-minute scheduler.

The old implementation used historical MFE directly; a persistent `ARMED` state did not yet exist. The current persistent lifecycle arrived in PR46 (`788b9e0515f9644c84d33539a1ed8ceb425c29d7`, Oct 6 21:28 UTC). Its first recorded state at `6cc6b8f0ea7e7cf2110525484c4f497509b47e45` was already net negative and `UNARMED`. A historical-MFE inference is not proof of a live persistent arm. No peak/giveback/CAS/math bug is proven in a saved executable SELL cycle.

## C. Unverified boundaries

`HISTORICAL_EXECUTION_WINDOW_UNVERIFIABLE`: intragap full order books, actual Monitor invocations and accepted/rejected unpublished generations cannot be reconstructed. OHLC candles must not substitute for actual executable Monitor inputs. Full original refreshed candidate/scan envelopes were not retained historically. Replay adapters faithfully use saved scalars/raw books but are labelled adapters, not complete historical production invocations.

Current main does not yet retain the new 15-minute candle receipts. The all-open adapter replay therefore cannot authorize rebound exits. Fundamental/supply evidence and candidate review freshness also remain explicitly unknown where stale. No unavailable evidence is treated as evidence of recovery or hard invalidation.

## D. Implemented and replay-verified; runtime verification pending

The future Monitor retains actual already-fetched Binance candle rows, generation and receipt time. Only contiguous, valid, complete 15-minute bars can establish structure, demand or a failed bounce. Bars incomplete at capture time stay excluded even if evaluation occurs later. A stale/missing scan or older market observation cannot update financial marks or produce an exit.

Ordinary thesis weakening still cannot cause a loss SELL. Hard invalidation requires current, sourced evidence. The explicitly authorized rebound risk exit is a **strategy extension**, separate from the ordinary-loss flag. All conditions must hold: known original thesis, fresh same-generation signals, persistent thesis invalidation, failed bounce, both BTC-relative windows weak, lower highs/lows, weak completed-bar volume and demand, current spread/depth, executable full liquidation, no recovered thesis/relative/structure, and adverse holding-risk comparison. Recovery cancels review. Missing evidence blocks exit.

The risk comparison is an explicit observed-level scenario, not an expected-return forecast: quantity times distance from executable VWAP to observed support must exceed the existing admission RR margin (1.5) times the greater of distance to resistance and immediate exit friction. Support/resistance come from the last eight completed bars. This modelling choice needs review before merge; it is not statistically validated expected risk. There is no fixed loss percentage, holding-time exit, fixed bounce percentage or capital-pressure liquidation.

Profit Protection parameters and arithmetic are unchanged. A real PROM sequence supplies a positive protection test: nine published cycles (eight Monitor, one Research), exact saved full raw books; actual arm, updated peak, and the one actual SELL are reproduced. The exit at Oct 10 04:31:25 UTC estimates +16.20245091 USDT; peak net +39.30538881 gives a 19.30538881 USDT floor. Repeating every cycle does not duplicate a SELL. The older Research observation is rejected by the new source-time guard without changing the peak. The saved audit JSON covers the pure protection function; `test_hunter_lifecycle_actual_replay.py` additionally exercises manager event emission with the explicitly labelled adapter.

The current all-open adapter replay emits zero SELLs, leaves historical events, closed ledger and tranches unchanged, and is identical on repeated execution. Full per-position fields, reasons, timestamps and evidence gaps are in `hunter-v2-lifecycle-evidence/current_all_open_replay.json`.

## Validation and safety

Local verification: 192 targeted tests passed; after later guards, 116 core tests and 54 persistence/generation/runtime/scheduler regressions passed. The two real PROM replay tests passed. Eight V1 before/after synthetic scenarios produced identical state, summary and result hashes. Independent review exercised the state machine, liquidity/venue paths and all rebound gates. Counts overlap and must not be summed.

The initial full suite on Windows could not pass Linux-specific `fcntl`, `O_NOFOLLOW` and Bash assumptions (and initially uncovered fixtures subsequently corrected). Full Ubuntu PR CI is required; its result must be read separately before claiming the full suite passes. Runtime/main readback of the new fields remains pending an approved merge and actual normal Monitor execution.

The replay asserts historical event/ledger preservation and duplicate immutability. Runtime safety fields are `real_order_count=0`, `real_trading_enabled=false`, `capital_authority=NONE_SHADOW_ONLY`; capital remains 20,000 / ordinary 17,000 / strategic reserve 3,000 USDT. Tests and replay perform no real orders, production workflow dispatch, service restart or main write.

## Reproduce

Run the normal Hunter unittest discovery on Linux. For an offline current snapshot adapter, with at least two portfolio commits available:

```sh
python scripts/replay_hunter_v2_lifecycle.py --ref 3c8a47a418dbbe13184553f68ed71c9b05aa6d6d
python -m unittest discover -s tests -p 'test_hunter_lifecycle*.py' -v
```

Primary sources: immutable portfolio/code commits listed in the evidence JSON and fixture. Real PROM exit: https://github.com/leo14881-eng/btc-grid-state/commit/ca41d741d43519281cf8bdb53e246d36f95c2b46 . Historical ENA first arm-reference: https://github.com/leo14881-eng/btc-grid-state/commit/daf86392e1da811e8939f2dd95fe68173b71edd7 . PENDLE: https://github.com/leo14881-eng/btc-grid-state/commit/1fd7c1d94283e69f29a0a54a7044ba7c8be959a1 .
