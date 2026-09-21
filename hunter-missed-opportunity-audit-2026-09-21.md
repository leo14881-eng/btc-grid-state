# Hunter Missed-Opportunity Audit — HYPE / ZEC — 2026-09-21

Status: RESEARCH AUDIT; NO CAPITAL AUTHORITY
Repository: leo14881-eng/btc-grid-state
Branch: main
Frozen logic preserved: automation-logic-final.md NOT MODIFIED.

## Executive finding
The primary demonstrated defect is execution/data coverage, not a proven scoring-gate defect.

Current Hunter state itself reports:
- universe_size = UNKNOWN
- scanned_count = PARTIAL_WEB_DISCOVERY_PASS
- coverage_ratio = UNKNOWN
- coverage_status = UNIVERSE_COVERAGE_INSUFFICIENT

This violates the operational intent of TASK 5 §-1 Full-Universe Early Discovery. HYPE was first persisted only on 2026-09-18 at 90.13 as POST_MOVE. ZEC has no persisted first-discovery event. Therefore the system cannot claim it searched and rejected these assets earlier; it failed to prove universe coverage.

## HYPE replay audit
Known point-in-time evidence available before the persisted 2026-09-18 discovery:
- 2026-09-06: HYPE printed a prior ATH around 89.6.
- 2026-09-08: Hyperliquid OI was reported around 14.3B; core crypto perps route roughly 97% of fees into HYPE buybacks.
- 2026-09-10..16: HYPE retraced from the prior high into roughly the 77-80 area.
- 2026-09-17: price/volume re-accelerated; 2026-09-18 borrowing against HYPE/BTC became a new catalyst and HYPE made another ATH.

Audit classification: NOT a clean PRE_MOVE low-base winner. It is a QUALITY/RELATIVE-STRENGTH RE-ACCELERATION case after an earlier major repricing. A compliant universe scan should at minimum have kept HYPE in research before 2026-09-18; whether it would have been capital-eligible is NOT proven.

## ZEC replay audit
Known point-in-time evidence available materially earlier:
- 2026-08-22: ZEC reached an eight-year high near 850 amid ETF push; derivatives activity was already very large.
- 2026-09-04: ZEC broke 1,000; ZCSH had at least 34.4M net inflows since Aug 25.
- 2026-09-08/09: Grayscale reported ZCSH above 500M AUM and >550k ZEC held.
- 2026-09-14: ZEC +~9.5%.
- 2026-09-16: ZEC +~20.4%; NU7 governance result became public.

Audit classification: EARLY discovery opportunity existed well before the Sep 16-18 acceleration, but by early September ZEC was already substantially repriced. The failure is that ZEC was absent from the persisted universe/watchlist despite strong market-relative and non-price evidence. This is a MISSED-DISCOVERY/COVERAGE defect, not proof that the frozen Forward Upside Gate should be loosened.

## T-7/T-5/T-3/T-1 audit
A strict numeric T-minus replay cannot be called a valid Blind Replay from web reconstruction because HYPE/ZEC were selected ex post as winners and PIT universe/survivorship-complete data are unavailable. Do not count this audit toward k calibration N_MIN=30.

Indicative detection audit only:
- HYPE: T-7/T-5 already QUALITY WATCH candidate; T-3/T-1 re-acceleration watch. Capital eligibility UNKNOWN.
- ZEC: T-7/T-5 should already be in WATCH/RESEARCH from ETF/AUM + relative-strength evidence; T-3/T-1 strong continuation/re-acceleration. Capital eligibility UNKNOWN.

## Optimization approved under existing freeze
No new metric, gate, score, scenario layer, or automation is added.

1. Enforce the already-mandatory full-universe pass as a runtime contract: a run with universe_size UNKNOWN or coverage_ratio UNKNOWN/insufficient may produce research observations, but must label DISCOVERY_COVERAGE_FAILURE and may not claim NO PRE_MOVE FOUND.
2. Separate MISSED_DISCOVERY from REJECTED_BY_GATE in audit statistics. An asset never evaluated cannot be counted as a correct rejection.
3. Preserve quality leaders through pullbacks: prior ATH/repricing alone does not remove an asset from research; it remains eligible for re-acceleration research while prospective TASK5 gates remain unchanged.
4. ZEC and HYPE are added only as audit cases/hypotheses; do not rewrite immutable first-discovery history.
5. Do not loosen the SHADOW BTC-relative Forward Upside Gate from these two cherry-picked winners. k remains UNSET and capital authority remains zero until the preregistered replay sample is complete.

## Next validation target
Execute the frozen V1 replay with a declared PIT venue/universe and deterministic stage parameters before outcomes. Minimum calibration requirement remains N_MIN=30. Compare against BTC-relative momentum and volume/momentum baselines. Only a reproducible systematic misclassification defect may justify unfreezing logic.
