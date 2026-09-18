# Hunter Market-Data Validation Freeze — V1

Status: FROZEN
Frozen at: 2026-09-18
Scope: research/validation only; NO capital authority.

## 1. Purpose
Test whether a reproducible market-data Hunter adds out-of-sample information beyond simple BTC-relative momentum and volume/momentum baselines. A positive result validates only the market-data layer. Fundamental Hunter remains UNVALIDATED until genuine point-in-time fundamental data exist.

## 2. Architecture freeze
Until V1 produces a real OOS result table with valid N and baseline comparison, do not add a new automation, task, state engine, evidence family, or discretionary score. New ideas go to a research-hypothesis queue for a later untouched version.

## 3. Point-in-time universe
Primary research venue: one spot CEX universe per replay; prefer Binance USDT spot when historical archives are available.
For every historical observation, construct the universe only from information available then.
Must retain assets that later delist/fail when historical listing/trading records are available.
Exclude stablecoins and leveraged tokens.
Universe formation thresholds (minimum history/liquidity and exact ranking N) MUST be declared in the replay manifest before outcome calculation. They may not be changed within a run after outcomes are inspected.
If a survivorship-complete universe cannot be built, label the run SURVIVORSHIP_INCOMPLETE_MVP; it cannot validate full Discovery Alpha.

## 4. Allowed V1 features
Only point-in-time market data:
OHLCV, quote volume, trade count, taker-buy/spot-demand proxy, BTC-relative strength, market structure, realized/rolling volatility.
Funding/OI/liquidations may be added only if reliable historical PIT data exist and the feature-set version is frozen before evaluation.
Revenue/fees/users/TVL/unlocks/catalysts are UNKNOWN in this V1 replay and may not be backfilled from current web pages.

## 5. Deterministic Stage contract
Stage must be machine-reproducible from frozen PIT inputs; LLM/manual override is forbidden in replay and in new live Candidate Snapshots after this freeze.
Inputs may include trailing returns, rolling volatility, peer-relative percentile, volume ratio, distance to trailing high, BTC-relative return and crowding when PIT-available.
The exact formulas and numeric cutoffs are experiment parameters: freeze them in the run manifest BEFORE outcomes are calculated. Claude's example z=2 / volume=3x / 85th percentile values are NOT production truth and are not silently adopted.
Missing required stage input => UNKNOWN_STAGE, not discretionary classification.
Existing 2026-09-18 snapshots remain historical legacy records and are not rewritten.

## 6. Candidate append-only audit contract
For every first discovery persist immutable event fields:
candidate_id, asset, discovered_at, available_at, discovery_price, BTC_price_at_discovery, universe_size, universe_rank, percentile_in_universe, peer_group_id, peer_relative_metrics, stage, market_regime, btc_regime, liquidity_regime, data_completeness, feature_version, rule_version, data_sources/source_timestamps, snapshot_content_hash.
Unavailable fields = UNKNOWN.
First-discovery fields never update; later information is a new observation event.

## 7. Baselines
Every replay must include:
A. BTC Buy & Hold reference.
B. Simple BTC-relative momentum baseline.
C. Simple volume/momentum baseline.
Exact lookbacks/thresholds are frozen in each run manifest before outcomes.
Primary question: incremental value of Hunter versus the BEST SIMPLE BASELINE in the same OOS window.

## 8. Walk-forward
Research/calibration target: 2022-01-01 through 2023-12-31 where data coverage permits.
OOS Evaluation #1 target: 2024-01-01 through 2024-12-31.
OOS Evaluation #2 target: 2025-01-01 through 2025-12-31.
If listing/PIT coverage forces a shorter window, record the exact change BEFORE outcome computation.
No random temporal shuffle. Once an OOS window is evaluated under a frozen run version, do not tune that version on the same window and call it OOS again.

## 9. Outcomes
Preserve continuous outcomes at 30D/90D/180D/365D where available:
absolute forward return, BTC-relative return, peer-relative return, MAE, MFE, Time-to-MFE, Detection Lead/Lag.
A categorical Winner label may be used only if its exact threshold/horizon/base-rate definition was preregistered in the run manifest before outcomes. If not: UNDEFINED_BY_FREEZE. Never invent a winner threshold after seeing winners.

## 10. Dependence/statistical honesty
Report Raw N and Effective N. Account for overlapping horizons, time clustering, sector/peer clustering and common BTC risk-on exposure using block/bootstrap or cluster-aware uncertainty where feasible. If not feasible, label CI/effective-N UNKNOWN and do not claim statistical validation.

## 11. Mandatory result table
Each completed OOS run reports at minimum:
Universe Assets; Raw N; Effective N; Promotions; Precision/Recall/FDR if labels defined; median 30D/90D/180D BTC-relative return; MAE; MFE; Time-to-MFE; Detection Lead/Lag; Research Load; data coverage/UNKNOWN%; Hunter result; RS baseline; Volume/Momentum baseline; Hunter minus Best Simple Baseline.

## 12. Validation language
Allowed statuses:
UNVALIDATED
RESEARCH_SIGNAL
MARKET_DATA_LAYER_VALIDATED
NO_INCREMENTAL_VALUE

Never call full Hunter validated from this V1.
Market-data layer can be called validated only after reproducible OOS results on at least two non-overlapping evaluation windows show stable incremental value over simple baselines with informative effective sample size/uncertainty and positive ex-ante detection lead.
If Hunter is indistinguishable from/worse than the best simple baseline, precision is near base rate, edge disappears after BTC/sector dependence correction, or discoveries are predominantly POST_MOVE, classify NO_INCREMENTAL_VALUE for that frozen version.

## 13. Persistence
Replay manifests and results are append-only research records. Every GitHub write: fetch current SHA when updating -> write -> reread main -> verify content/hash/version. Git history alone is not treated as cryptographic immutability; commit SHA and snapshot_content_hash must be recorded when available.

## 14. Priority
Data > architecture. First blocker is a usable PIT universe/dataset. N=0 must identify the smallest concrete blocker and next run must attack it rather than redesigning the system.
