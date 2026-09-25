# TASK 5 — VALIDATION LAYER (v5.1-freeze)

Status: FROZEN
Role: pre-report automatic quality control for TASK 5 v5.1. Engine logic §0–§17 remains frozen. Run every assertion before generating the finished daily report. Blocking violations cannot authorize capital action, enter validated rankings, or receive nearest labels. Independently valid upstream outputs remain visible under score-preservation and generation rules.

All thresholds in this layer are PROVISIONAL unless explicitly stated otherwise.

## V1 — SUPPLY_ANCHOR (bidirectional dilution validation + freshness/conflict guard)

Required inputs:
- `valuation_horizon`: required; `today` or target date/range.
- `material_supply_change_exists`: required boolean.
- PROVISIONAL materiality definition: expected new circulating supply within target horizon >= 5% of current circulating supply. Determination must cite unlock schedule/emission curve evidence.
- Every `today_circulating` and `modeled_future_circulating` value must carry `{value, as_of, source}`.

Missing-input guard:
- If `valuation_horizon` missing OR `material_supply_change_exists` missing OR (`material_supply_change_exists=true` AND `modeled_future_circulating` missing): `BLOCK`, error=`SUPPLY_INPUT_MISSING:<field_name>`.

Freshness guard:
- If any supply input used by valuation has `report_AS_OF - supply.as_of > 30d`: `BLOCK`, error=`SUPPLY_INPUT_STALE:<field_name>`.

Source-conflict guard:
- If a second reasonably reliable source exists for the same supply field, first harmonize economic definition/date/scope.
- If harmonized values still satisfy `max(source_values)/min(source_values) > 1.2`: `BLOCK`, error=`SUPPLY_SOURCE_CONFLICT:<field_name>`.
- While such conflict remains, any materiality calculation dependent on that field is `BLOCKED / UNRESOLVED`; do not assert `material_supply_change_exists=true/false` from the conflicted denominator.

Main anchor validation:
- If `valuation_horizon > today` AND `material_supply_change_exists=true`, required supply basis = `modeled_future_circulating`.
- If `supply_basis == today_circulating` AND `material_supply_change_exists=true`: `BLOCK`, error=`DILUTION_IGNORED`.
- If `supply_basis == max_supply` AND `today_circulating < max_supply` AND `modeled_future_circulating != max_supply`: `BLOCK`, error=`WRONG_SUPPLY_ANCHOR`.
- Exception: if `modeled_future_circulating == max_supply` at target horizon, max supply is valid.

Principle: correct supply anchor = expected circulating supply at the valuation target horizon. No supply anchor is inherently conservative/correct merely because it is today circulating or max supply.

## V2 — ILLEGAL_DUAL_STATE

Gate must be exactly one of `PASS / MARGINAL / FAIL / BLOCKED / NOT_CONFIRMED` and single-valued. If it contains `/` or multiple states: `BLOCK`, error=`ILLEGAL_DUAL_STATE`, then rejudge: 3x EXTREME -> FAIL; 3x AGGRESSIVE with controllable permanent loss -> MARGINAL; conflicted dependency -> BLOCKED.

## V3 — REVENUE_BASIS_CONFLICT

Normalize revenue/fees values to the same economic definition AND comparable observation window before comparison. Never compare a 30d run-rate with trailing-year annualized as duplicate measurements of the same period. After harmonization, if `max(annualized)/min(annualized) > 1.5`: `BLOCK`, error=`REVENUE_BASIS_CONFLICT`; dependent Reverse Valuation=`BLOCKED`; Gate=`BLOCKED`; report conflicting sources/definitions.

## V4 — MISSING_CASE

FRM must contain `BEAR / BASE / BULL / EXTREME_BULL / PERMANENT_LOSS`. Missing any: `BLOCK`, error=`MISSING_CASE:<case_name>`.

## V5 — INCOMPARABLE_MISUSE

`INCOMPARABLE` is allowed only when required data are complete but dominance genuinely cannot be established. If a required component is missing, relabel `DATA_INSUFFICIENT` and list missing components. Required: six FQ components, FRM, future_circulating when required by V1, reverse_valuation, crowding, invalidation_geometry.

## V6 — STALE_IN_GATE

Any EA entering BUY/ADD Gate must have `status=FRESH`. If `STALE` or `INVALIDATED_BY_EVENT` is used by Gate: `BLOCK`, error=`STALE_EA_IN_GATE`.

## V7 — FRESHNESS_MISSING

Every current v5.1 FQ must contain `{value, as_of, generation}`. Every current/retained EA must contain `{value, as_of, expires_at, status, generation}` where applicable. Missing required metadata: `BLOCK`, error=`FRESHNESS_FIELD_MISSING`.

## V8 — CONDITIONAL_UNSTRUCTURED

If Risk Governor uses CONDITIONAL, it is a WAIT substate and must contain `{condition, action_if_met, action_if_not_met, review_deadline, evidence, owner}`. Missing structure -> downgrade to WAIT, error=`CONDITIONAL_UNSTRUCTURED`. Provisional numeric references without defensible baseline may trigger reassessment only, never automatic ALLOW/BUY.

## V9 — RESERVED

Dilution validation is merged into V1. Do not run duplicate dilution logic.

## V10 — NEAREST_UNQUALIFIED

Any `NEAREST-TO-*-BUY` must pass every corresponding TASK 5 §17 requirement. If any fails: clear nearest label, error=`NEAREST_UNQUALIFIED`.

## BLOCK TAXONOMY — mandatory sub_type

Whenever the reporting layer marks an asset BLOCKED, it must assign exactly one sub_type:

### BLOCKED-INCOMPLETE
Missing one or more required current components/inputs such that additional data could materially change the Hunter conclusion. Includes missing/incomplete FRM, required future_circulating, Reverse Valuation, crowding, invalidation_geometry, fresh required inputs, unresolved source conflict, or other prerequisite evidence.

Action: include in `DATA REPAIR PRIORITY`; list `error_code` and `missing_items`. Capital action=$0.

### BLOCKED-CONCLUDED
Required decision inputs are sufficiently complete and a stable non-Hunter-entry conclusion has already been reached. Typical condition: inputs sufficient AND Gate in `{FAIL, MARGINAL}` AND the exclusion conclusion is stable for a new Hunter entry.

Action: include in `EXCLUDED`, NOT in Data Repair Priority; attach explicit `conclusion`, e.g. `DO-NOT-CHASE` or `UPSIDE_INSUFFICIENT`. Capital action=$0.

An unresolved secondary issue that is relevant only to existing-holder management does not automatically convert a stable new-entry `BLOCKED-CONCLUDED` into `BLOCKED-INCOMPLETE`; label that secondary issue separately.

Forbidden: marking `BLOCKED-CONCLUDED` as `DATA_INSUFFICIENT` merely to keep it in a repair queue.

## SCORE PRESERVATION

`BLOCK != DELETE SCORE`.

A validator failure blocks capital authorization, BUY/ADD Gate, nearest, validated-ranking participation and execution-state promotion. It does not erase independently valid upstream outputs.

### FQ preservation + generation
Every displayed numeric FQ must have a generation:
- `v5.1-FQ`: computed from the frozen v5.1 six-component rubric, with `as_of`; eligible for `FQ RESEARCH VIEW` ranking if otherwise valid/current.
- `LEGACY-FQ`: inherited from an older engine/rubric or cannot be proven to have been recomputed under v5.1; audit-visible only, never used in any v5.1 ranking, Signal, Gate, nearest, or other v5.1 decision.

If provenance is uncertain, default to `LEGACY-FQ`, never assume v5.1-FQ.
If FQ prerequisites are incomplete for a fresh v5.1 computation, output `v5.1-FQ=DATA_INSUFFICIENT`; a legacy value may be shown separately in audit.

Mixing FQ generations in one v5.1 ranking is forbidden: `BLOCK RANKING`, error=`FQ_GENERATION_MIXED`.

### EA preservation + generation
If FRM + Reverse Valuation + all EA-required inputs were valid when EA was calculated, retain numeric EA with `{as_of, expires_at, status, generation}` even if later stale/invalidated. Such values are DISPLAY/AUDIT ONLY unless current v5.1 requirements and freshness permit use. If an old EA cannot be proven to have followed v5.1 order/prerequisites, mark `LEGACY-EA / OBSOLETE`; never use it in v5.1 Gate/ranking. If current EA prerequisites are incomplete: `v5.1-EA=DATA_INSUFFICIENT`.

## RANKING SEMANTICS

- `FQ RESEARCH VIEW`: only valid/current `v5.1-FQ` values with as_of. If none or insufficient refresh coverage, output `PENDING v5.1-FQ REFRESH`; do not rank LEGACY-FQ.
- `VALIDATED EA RANKING`: only current v5.1 EA values satisfying required validation/freshness conditions.
- `LEGACY AUDIT`: may display LEGACY-FQ and LEGACY-EA values, clearly labeled OBSOLETE/AUDIT ONLY, never mixed into current rankings.

## Execution flow

1. Pull current data and fill frozen TASK 5 v5.1 engine.
2. Validate supply freshness/conflicts and all V1–V10 assertions before finished-report generation.
3. Preserve independently valid upstream scores with generation/status metadata.
4. For every blocked asset assign exactly one of `BLOCKED-INCOMPLETE` or `BLOCKED-CONCLUDED`.
5. `BLOCKED-INCOMPLETE` -> DATA REPAIR PRIORITY. `BLOCKED-CONCLUDED` -> EXCLUDED. Never place the same asset in both.
6. Only validation-eligible current-generation assets participate in current rankings/nearest/capital decisions.
7. No validator may fabricate missing values merely to obtain PASS or a score.
8. Engine logic is not modified because of a daily data/fill error.

## FREEZE declaration

TASK 5 engine logic §0–§17 remains FROZEN at v5.1. This Validation Layer is FROZEN after the BLOCK taxonomy + V1 supply guard + FQ generation patch. Unfreeze only for a demonstrated structural defect that systematically misses/misclassifies a real opportunity and cannot be explained by data/fill error. Daily execution = data fill + assertions; it does not include logic modification.

## STRUCTURAL DEFECT PATCH — 2026-09-25 — USER APPROVED

Defect demonstrated: Hunter recorded JTO as a PRE_MOVE candidate at 0.4554 with two independent non-price/fundamental evidence domains, yet the architecture could not advance any otherwise valid candidate to a first-tranche human capital proposal because the separate quantitative BTC-Relative Gate had `k=UNSET/SHADOW`. This is a systematic process veto, not a daily data-fill error, and satisfies the FREEZE declaration's structural-defect exception.

Validation semantics after this patch:
- V1–V10, freshness, supply, FRM, Reverse Valuation, EA provenance and Risk Governor validations remain mandatory and unchanged.
- The quantitative BTC-Relative Gate remains SHADOW until frozen Replay calibration requirements are met. SHADOW results cannot independently authorize BUY/ADD.
- `k=UNSET` / incomplete Blind Replay is no longer, by itself, a validation failure or automatic capital=$0 for the separate `FUNDAMENTAL_EARLY_ENTRY_PROPOSAL_ELIGIBLE` lane defined in `automation-logic-final.md §8g`.
- That lane may produce a **human capital proposal only** after complete current-price forward-return work plus Risk Governor, Portfolio Allocation, Drawdown and Counterparty gates. It never creates automatic execution authority.
- Any actual validator failure under V1–V10 still blocks the lane where the failed field is required by §8g.
- First tranche must be staged; subsequent tranches require better price structure or new independent confirmation, not mechanical averaging down.
- $50,000 Crisis Reserve remains excluded. User remains sole executor.
- Track `MISSED_EARLY_ENTRY` separately from `MISSED_DISCOVERY` and `REJECTED_BY_GATE` so system/process latency is measured rather than hidden as an investment rejection.

This patch changes only the coupling between Replay SHADOW status and human-reviewed fundamental capital proposals. It does not retune k, alter the frozen Replay sample, change PRE_MOVE evidence requirements, or weaken V1–V10.
