# Original thesis review: offline draft, execution gate closed

This is a proposed source contract and a tested pure evaluation kernel. It is not a deployed schema migration, an authenticated historical thesis, or an approved strategy. `research/hunter_thesis_contract_review.py` has no network, order, writer or production imports. No Monitor or manager calls it. Both hard-exit and rebound-exit authorization are unconditionally false. Production integration, original-entry capture, source authentication and strategy approval remain unfinished.

The existing repair at local commit `251f3d55b3fe5928ef61efbc84023c3a70853d5f` and its separate Git bundle are preserved. The subsequent main snapshot is `112b9740d53dd7351756548e76e3d1e67efdd3c5`, with portfolio time `2026-10-10T19:31:37.865890+00:00`. No related source-code changes occurred between those main snapshots; runtime publications were merged without altering their contents.

## Three independent questions

Every dimension must report three propositions separately:

1. `entry_condition_still_holds`: does the exact recorded original continuing condition hold now?
2. `deteriorated_since_entry`: is the explicitly defined comparison worse than the original observation?
3. `hard_invalidation_confirmed`: does an independently approved hard predicate have sufficient evidence?

Each proposition has TRUE / FALSE / UNKNOWN, an exact reason, predicate ID/version, operand values/units, and source receipts. A relative-strength value declining from 3 to 2.5 still satisfies an original minimum of 2. A weaker measurement is not by itself failure of the thesis. Failure of an ordinary entry gate is not automatically hard invalidation.

`FALSE AND UNKNOWN` is logically FALSE, while evidence coverage remains incomplete. The kernel therefore separates logical result from recursive `evidence_complete`. Even complete synthetic coverage cannot set `original_thesis_authenticated=true`. Hashes show input integrity; they do not prove a venue supplied the data or that the original policy was approved.

## Dimensions and evidence contract

| Dimension | Original evidence to retain | Current evidence required | Unknown / exclusion boundary |
|---|---|---|---|
| Execution conditions | Exact predicate code/parameters, executable venue/symbol/type, fee and execution model, entry source IDs | Same identity, actual quantity, fresh executable book and cost evidence | An entry-only chase, new-capital, reserve or ADD-better-price rule is excluded from holding thesis |
| Relative strength | Original measured asset/BTC values plus underlying bars, exact formula and windows | Actual asset and BTC 1h/4h rows aligned to the same closed windows; recomputed metrics | Missing BTC data, wrong market, mismatched windows or refreshed wrappers cannot validate it |
| Market structure | Exact continuing structure predicate, raw entry windows and level definitions | Complete current 15m windows, explicit weak/recovered result; partial recovery observation recorded separately | No original structure predicate means original-condition comparison UNKNOWN; a live partial bar cannot confirm completed-bar weakness |
| Liquidity | Original book, quantity, spread/depth/slippage predicates, cost model | Source-identified depth covering the whole exit quantity, fresh bid/ask, fees, executable estimate | A failed ordinary liquidity condition is distinct from the approved hard rule; missing full depth makes cost UNKNOWN |
| Volume / demand | Original volume and demand definition, raw windows, numerator and denominator | Like-for-like complete windows; actual taker-buy demand where supported | Bybit total volume cannot substitute for unavailable taker-buy demand; partial-bar volume is not a full-bar comparison |
| Candidate evidence | Immutable candidate/dossier and supporting claims as known at entry | Fresh, identity-bound corroborating evidence and explicit contradiction or continued support | Absence from a shortlist is UNKNOWN, not disappearance of the thesis or hard failure |
| Fundamental / supply | Original published claims, source publication/effective/observation times, unlock/supply exposure and risk predicate | Rechecked source versions and new risk facts with those separate times | Updating `verified_at` on a static file does not establish source freshness; a future announcement is not an already-effective event |
| Venue identity | Original venue/symbol/type and identity proof, scope of proof | Current exact execution identity and public venue status; explicit planned/effective times | Execution-channel label alone is not proof; current admission cannot retroactively prove historical entry venue |

The current ten formal positions have no retained immutable predicate manifest covering these dimensions. The historical BUY records retain five numeric signal fields and some execution scalars. Only GIGGLE, ENA, PENDLE and ENJ retain the additional original signal/book metadata identified in the entry audit; that still does not supply original raw candles, full books, narrative thesis or all dimension predicates. BUY time must not be substituted for missing source-observation time. The current-position coverage artifact therefore returns UNKNOWN for every missing original proposition rather than manufacturing a condition from today's constants.

## Draft schema and validation

The contract binds `shadow_id`, asset/venue/symbol/type, original BUY source, entry generation/time, frozen policy reference and typed policy values plus their hash. It contains versioned predicates, dimension, proposition and applicability (`CONTINUING_THESIS`, `HARD_CONDITION`, `ENTRY_ONLY`, `ALLOCATION_ONLY`). Each dimension/proposition must explicitly declare `all` or `any`; there is no default hard AND/OR.

Each operand chooses `entry`, `current` or frozen `policy`, an exact field and a unit. Arithmetic comparisons reject booleans, strings, null and nonfinite numbers. Equality requires matching types. Current policy fallback is absent. Only the small explicit `all`/`any`/comparison expression language is evaluated; no Python expression execution occurs.

Source specs explicitly declare POSITION or BENCHMARK. POSITION identity must equal the contract identity. BENCHMARK must match a separately declared benchmark identity and purpose. This manifest still needs external policy authentication before production use. Merely editing the spec and packet together does not authenticate a new benchmark.

Receipts carry source reference, receipt ID, kind, subject identity, generation, observed/fetched times, typed values, payload hash and full-metadata hash. Current generation must match the evaluated generation; entry receipts match the original generation and entry time. A required closed window has a declared duration, actual start/end and `complete=true`. Freshness checks compare both original observation and window end directly against evaluation time: two individually valid age intervals cannot add to twice the TTL. Alignment checks independently validate every referenced benchmark packet.

For documentary/venue events, `source_published_at`, `source_observed_at`, `fetched_at`, `effective_from`/`effective_until` and wrapper time have distinct meanings. An explicit `ALREADY_EFFECTIVE` rule rejects future or expired events; `SCHEDULED_FACT` permits a future effective date only as a previously published scheduled fact. An actual external read and raw-document/version verification still need a production adapter. TTL values and predicate meanings require approval; the kernel has no policy defaults.

Consecutive confirmations bind a position/predicate/version series and count new complete source windows, independent of generation wrappers. Duplicate and old windows do not advance the counter. Gaps, FALSE and UNKNOWN break the streak. Conflicting observations invalidate any current streak containing that window; an unrelated old conflict is audited without erasing a newer independent streak. Confirmation does not authorize a trade.

## Required production capture, still unresolved

An eventual future BUY must preserve the exact approved predicate manifest, policy/source code hashes and original input receipts immutably. ADD has its own allocation evidence and must not replace the original thesis. Capture must happen in the current Single Writer transaction, with portfolio/summary generation consistency, existing CAS/ledger guards, and a verified main readback. No existing BUY or SELL event may be rewritten to retrofit such evidence.

Existing positions can only attach historical evidence that is independently matched to the exact original shadow ID, asset, opened time, tranche and source generation. Missing evidence remains explicit. Current maintenance policy, if adopted for legacy positions, must be labelled a new version with an effective date and user approval; it cannot be called the original thesis.

Before activation, the source adapters must authenticate raw venue/document responses and recompute derived claims, the original maintenance predicates must be approved, and the actual Monitor/hourly invocation must exercise the evaluator through final allocation and persisted main readback. The offline kernel does not provide those missing pieces.

## Risk comparison proposal and approval boundary

See [the detailed risk model proposal](hunter-v2-rebound-risk-model-proposal.md). It compares the same quantity and USDT baseline under immediate full-cost exit and an explicitly specified future holding policy. It requires conservative uncertainty bounds to support both expected-net-value and tail-risk improvement. Bid VWAP already includes spread and depth impact; those costs cannot be deducted again. Missing execution depth, delay-error calibration, future labels or original thesis coverage produces UNKNOWN.

No holding horizon, tail confidence, minimum improvement, effective sample size or exit threshold has been selected. The user must decide whether non-hard loss exits are permitted at all, approve the continuing-thesis/holding policy and evaluation horizon, select loss/tail/confidence/margin criteria, and approve cost uncertainty, sample eligibility, repeated-review and out-of-sample acceptance rules. Entry RR=1.5 supplies none of those decisions. The evaluation horizon is a research label, never a timed SELL rule. ENA and PENDLE are two correlated position trajectories, not 1,538 independent training examples.

The current implementation continues to record LOSS_RECOVERY / REBOUND_EXIT_REVIEW with reasons while `risk_reduction_authorized=false`. No proposed model, UNKNOWN dimension, static percentage loss, elapsed holding time or capital shortage can enable a rebound SELL.

## Offline verification

`PYTHONPATH=tests python -m unittest test_hunter_thesis_contract_review -v` runs 34 synthetic tests. They cover three-valued semantics, missing original manifests, immutable policy version/units, source/position/generation binding, dual-hash integrity, empty/null/nonfinite inputs, source and bar age, benchmark alignment, source publication/effective time, entry-only exclusions, ordinary-versus-hard separation, explicit hard OR, recursive coverage, duplicates/gaps/conflicts and immutable repeated evaluation.

`hunter-v2-lifecycle-evidence/offline_thesis_contract_current_positions.json` records the ten actual current shadow IDs with their missing-contract results, before/after portfolio hashes and zero production mutations. This is a fail-closed coverage check, not a full original-thesis replay. Full Linux CI, source authentication, strategy approval and deployed main readback remain pending.
