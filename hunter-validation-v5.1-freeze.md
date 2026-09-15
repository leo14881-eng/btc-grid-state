# TASK 5 — VALIDATION LAYER (v5.1-freeze)

Status: FROZEN
Role: pre-report automatic quality control for TASK 5 v5.1. Engine logic §0–§17 remains frozen. Run every assertion before generating the finished daily report. Any blocking violation marks the asset BLOCKED with error_code; blocked assets cannot authorize capital action, enter validated rankings, or receive nearest labels. Independently valid upstream research outputs remain visible for audit/research under the score-preservation rules below.

All thresholds are PROVISIONAL because the current six-asset sample has no statistical calibration.

## V1 — SUPPLY_ANCHOR (merged bidirectional dilution validation)

Required inputs; missing input blocks rather than silently passing:
- `valuation_horizon`: required; `today` or target date/range.
- `material_supply_change_exists`: required boolean.
- PROVISIONAL materiality definition: expected new circulating supply within target horizon >= 5% of current circulating supply. The determination must cite an unlock schedule/emission curve source.
- If `valuation_horizon` missing OR `material_supply_change_exists` missing OR (`material_supply_change_exists=true` AND `modeled_future_circulating` missing): `BLOCK`, error=`SUPPLY_INPUT_MISSING:<field_name>`.

Main validation:
- If `valuation_horizon > today` AND `material_supply_change_exists=true`, required supply basis = `modeled_future_circulating`.
- If `supply_basis == today_circulating` AND `material_supply_change_exists=true`: `BLOCK`, error=`DILUTION_IGNORED`.
- If `supply_basis == max_supply` AND `today_circulating < max_supply` AND `modeled_future_circulating != max_supply`: `BLOCK`, error=`WRONG_SUPPLY_ANCHOR`.
- Exception: if `modeled_future_circulating == max_supply` at target horizon, max supply is valid.

Principle: supply-anchor correctness means `supply_basis` equals expected circulating supply at the valuation target horizon. `today_circulating`, `max_supply`, and `modeled_future_circulating` are not inherently right or wrong; mismatch to target-horizon expected circulation is the error.

## V2 — ILLEGAL_DUAL_STATE

Gate must be exactly one of `PASS / MARGINAL / FAIL / BLOCKED / NOT_CONFIRMED` and single-valued.
If it contains `/` or multiple states: `BLOCK`, error=`ILLEGAL_DUAL_STATE`, then rejudge:
- 3x assumptions EXTREME -> FAIL.
- 3x assumptions AGGRESSIVE and permanent loss controllable -> MARGINAL.
- depends on conflicted data -> BLOCKED.

## V3 — REVENUE_BASIS_CONFLICT

Normalize revenue/fees values to the same economic definition AND comparable observation window before comparison. Do not compare a 30d run-rate with a trailing-year annualized figure as if they were duplicate measurements of the same period. Once definitions/windows are harmonized, assertion: `max(annualized) / min(annualized) <= 1.5`. If ratio >1.5: `BLOCK`, error=`REVENUE_BASIS_CONFLICT`; dependent Reverse Valuation becomes BLOCKED; Gate=`BLOCKED`; report sources and definitions.

## V4 — MISSING_CASE

FRM cases must include `BEAR / BASE / BULL / EXTREME_BULL / PERMANENT_LOSS`.
Missing any: `BLOCK`, error=`MISSING_CASE:<case_name>`.

## V5 — INCOMPARABLE_MISUSE

`INCOMPARABLE` is allowed only when data are complete but asymmetry genuinely overlaps under the dominance test. If used while a required component is missing, relabel `DATA_INSUFFICIENT` and list missing components. Required components: six FQ components, FRM, future_circulating, reverse_valuation, crowding, invalidation_geometry.

## V6 — STALE_IN_GATE

Any EA entering BUY/ADD Gate must have `status=FRESH`. If `STALE` or `INVALIDATED_BY_EVENT` is used by Gate: `BLOCK`, error=`STALE_EA_IN_GATE`.

## V7 — FRESHNESS_MISSING

Every FQ must contain `{value, as_of}` and every EA `{value, as_of, expires_at, status}`. Missing any field: `BLOCK`, error=`FRESHNESS_FIELD_MISSING`.

## V8 — CONDITIONAL_UNSTRUCTURED

If Risk Governor uses CONDITIONAL, it must be a WAIT substate with complete structure `{condition, action_if_met, action_if_not_met, review_deadline, evidence, owner}`. Bare CONDITIONAL or any missing field -> downgrade to WAIT, error=`CONDITIONAL_UNSTRUCTURED`. Provisional numeric references without defensible historical baseline may trigger reassessment only; they must not automatically promote Governor to ALLOW or authorize BUY.

## V9 — RESERVED

Merged into V1 SUPPLY_ANCHOR. Do not run a duplicate dilution validator.

## V10 — NEAREST_UNQUALIFIED

Any `NEAREST-TO-*-BUY` asset must pass every corresponding TASK 5 §17 gate. If any gate fails but nearest is assigned: clear nearest label, error=`NEAREST_UNQUALIFIED`.

## VALIDATION OUTPUT SEMANTICS — SCORE PRESERVATION

`BLOCK != DELETE SCORE`.

A validator failure blocks:
- capital authorization;
- BUY / ADD Gate;
- NEAREST designation;
- participation in VALIDATED ranking;
- promotion of execution state.

It does NOT erase independently valid upstream outputs.

### FQ preservation
If all six FQ-required components are valid and current under TASK 5, output the numeric FQ with `FQ_as_of` even when a downstream validator fails. If FQ prerequisites themselves are incomplete, output `FQ=DATA_INSUFFICIENT`.

### EA preservation
If FRM + Reverse Valuation + all EA-required inputs were valid when EA was calculated, retain the numeric EA with `{as_of, expires_at, status}` even when later stale or invalidated. Examples: `EA=77 | status=INVALIDATED_BY_EVENT`; `EA=72 | status=STALE`. Such values are `DISPLAY/AUDIT ONLY` and MUST NOT enter current capital Gate or VALIDATED EA ranking. If EA prerequisites themselves were incomplete when calculated, output `EA=DATA_INSUFFICIENT`; do not preserve an invalid legacy score merely because it exists historically.

### Rankings
Output two concepts distinctly when useful:
- `RESEARCH SCORE VIEW`: may display independently valid FQ and DISPLAY/AUDIT-ONLY EA values, clearly tagged by status.
- `VALIDATED RANKING`: only assets satisfying the required validation/freshness conditions participate. Never mix stale/invalidated/display-only values into validated capital ranking.

Blocked assets remain visible in `BLOCKED / DATA REPAIR QUEUE` with all independently valid upstream scores preserved.

## Execution flow

1. Pull current data and fill frozen TASK 5 v5.1 engine.
2. Run V1–V10 for every asset before finished-report generation.
3. Any BLOCK -> asset cannot authorize capital action, enter validated ranking, or receive nearest. Report `{asset,error_code,missing_items}` and preserve independently valid upstream scores in `BLOCKED / DATA REPAIR QUEUE` / research view.
4. Only validation-eligible assets participate in validated ranking/nearest decisions.
5. Engine logic is not modified because of a daily fill/data error; assertions intercept it.
6. No validator may fabricate missing values merely to obtain PASS.

## FREEZE declaration

TASK 5 engine logic §0–§17 is FROZEN from v5.1. This Validation Layer is FROZEN after this score-preservation clarification. Unfreeze only for a demonstrated structural defect that systematically misses/misclassifies a real opportunity and cannot be explained by data/fill error. Daily execution = data fill + assertions; it does not include logic modification.