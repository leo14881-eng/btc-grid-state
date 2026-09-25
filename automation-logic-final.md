# Investment Automations — Canonical Logic FINAL

Version: 2026-09-19 v1.5
Timezone: Asia/Ho_Chi_Minh
Status: FROZEN under “10-Year Wealth Compounding Architecture FINAL”

This is the single canonical logic source for the five active investment automations. Every run must read the matching task section plus Shared Global Rules. If this file or the required section is unavailable, output `LOGIC SOURCE UNAVAILABLE` and issue no new capital action.

## Canonical state sources

- Portfolio SSOT: https://raw.githubusercontent.com/leo14881-eng/btc-grid-state/refs/heads/main/portfolio-state.json
- Decision Journal: https://raw.githubusercontent.com/leo14881-eng/btc-grid-state/refs/heads/main/decision-journal.json
- Sentinel state: https://raw.githubusercontent.com/leo14881-eng/btc-grid-state/refs/heads/main/sentinel-state.json
- Grid SSOT: https://raw.githubusercontent.com/leo14881-eng/btc-grid-state/refs/heads/main/state.json
- Opportunity Hunter ledger: https://raw.githubusercontent.com/leo14881-eng/btc-grid-state/refs/heads/main/opportunity-hunter-state.json

---

# Shared Global Rules

## 1. State ownership

`portfolio-state.json` is the only source of truth for portfolio BTC quantity, portfolio weights, ordinary cash/USDT balance, 0.5 BTC permanent-core status, cycle-sellable BTC and portfolio-layer usage.

`sentinel-state.json` stores only Sentinel-owned market/signal state. It must not duplicate portfolio holdings, weights, cash, permanent-core quantity or cycle holdings. It may store portfolio source/version/update timestamp/content SHA references only.

`state.json` is the only source of truth for actual Bybit Grid state. It must contain Grid-owned state only. Ordinary portfolio cash, strategic term deposit, crisis reserve or other portfolio-layer capital must not be stored there.

`opportunity-hunter-state.json` is the Hunter research/position ledger. An empty `assets` object is valid when portfolio SSOT and the Decision Journal confirm there are no Asymmetric positions. Empty assets alone is NOT an uninitialized-state error. If a confirmed position/execution should exist but is missing, treat that as a sync violation.

User-confirmed actual execution data has priority. Never infer missing balances, quantities, execution prices or timestamps.

## 1A. GitHub optimistic-concurrency + retry contract — USER-APPROVED 2026-09-25

Applies to **all automations and every mutable GitHub SSOT file**, including Sentinel, Hunter, portfolio, journal and Grid state.

1. **Never write from a stale SHA.** Every mutation starts with an exact-path `fetch_file(path, main)` returning the complete current content + blob SHA. Build the mutation only from that fetched base.
2. **Single-path serialization.** Within one run, never issue two writes to the same path in parallel. Writes to one path are strictly sequential. Do not reuse a SHA after any successful write to that path.
3. **Optimistic concurrency / compare-and-swap.** `update_file` must use the blob SHA fetched immediately before that mutation. A SHA/version conflict is treated as concurrent modification, not repository failure.
4. **Conflict retry: maximum 3 attempts total.** On SHA/version conflict: discard the pending full-file replacement; refetch the newest complete blob + SHA from `main`; reapply **only this automation's owned-field/owned-section mutation** onto the new base; revalidate invariants; retry. Never blindly resend old serialized content.
5. **Backoff semantics.** Attempts are sequential, never parallel. If the runtime supports waiting, use a short bounded backoff between retries; if it does not, immediate refetch/rebase/retry is acceptable. Correct rebase matters more than delay.
6. **Post-write verification is mandatory.** After a successful update, exact-path reread `main`; verify new blob SHA/content and every expected owned field/section. Only then may the run report `PERSISTED=TRUE`.
7. **Concurrent non-owned changes must survive.** Before retry, compare the newly fetched base with the prior base. Preserve all changes outside the automation's owned fields/section. If ownership overlaps or safe merge cannot be proven, stop with `STATE MERGE CONFLICT + NO NEW CAPITAL ACTION`; never choose one side silently.
8. **Failure classification must be factual.** Report `SHA_CONFLICT_RETRYING` while retrying; after 3 exhausted conflict attempts report `STATE PERSISTENCE FAILURE: CONCURRENCY_EXHAUSTED`. Auth/permission/network/parse/contract failures use their actual returned class; never infer “security block”, “GitHub truncation” or permission failure without direct evidence.
9. **Idempotency.** Before writing, check whether the intended owned-field mutation is already present in the latest base. If yes, skip the write and reread/verify; do not create duplicate events, duplicate ledger lines or timestamp-only commits.
10. A failed persistence closure can block new capital actions as required by the owning task, but it must not erase the valid market/research result; preserve the result as unpersisted and retry persistence on the next eligible run.

This contract supersedes any older persistence instruction that allows fewer conflict retries or reporting success before reread verification. It changes persistence mechanics only, not investment logic or capital gates.

## 2. Global state health check

Before any capital recommendation verify required files are readable, critical fields/schema exist, and freshness is reasonable. If required state is stale, malformed, inconsistent or unavailable, output one of `STATE STALE`, `STATE CORRUPTED`, `STATE UNAVAILABLE`, `STATE SYNC VIOLATION` and issue `NO NEW CAPITAL ACTION` until repaired. Never fall back to chat memory or guessed balances.

If market/signal state is stale, refresh market evidence before any new action.

## 3. Execution consistency

Before any BUY / ADD / REDUCE / SELL / major term-deposit action inspect all confirmed executions since the last successful Quarterly full audit. Every confirmed execution must have: immutable journal entry -> portfolio-state update -> relevant cooldown/position/strategy-ledger update -> reread verification. Any break => `STATE SYNC VIOLATION`; repair first.

## 4. Portfolio architecture

BTC benchmark 45%, range 30%-60%.
Cash/T-bill benchmark 35%, range 20%-55%.
Structural benchmark 15%, tactical range 0%-30%, single Structural asset hard cap 5%.
Asymmetric allocation follows its own authorization and hard limits.
$50,000 Crisis Reserve is strictly isolated.

BTC allocation range governs risk/new funding only; it never mechanically triggers BTC selling.

## 5. BTC permanent-core rule — highest priority

Accumulate BTC in bear markets and distribute only cycle inventory late in confirmed bull-market overvaluation/overheating/top conditions. No ordinary short-term high-sell/low-buy trading.

Permanent BTC core target = 0.5 BTC.

If total BTC < 0.5 BTC: permanent-core building stage; ordinary cycle distribution may not sell BTC; cycle-sellable BTC = 0; future ordinary BTC buys first build the permanent core.

If total BTC > 0.5 BTC: only BTC above 0.5 is cycle inventory; ordinary cycle selling requires confirmed bull-market top/overheat conditions; no ordinary cycle sale may reduce total BTC below 0.5.

Only a fundamental break in BTC’s long-term thesis may trigger reassessment of the permanent-core rule.

## 6. Capital authority / Allocation Gate

Automations discover, analyze and recommend. User is the sole executor.

Every proposed capital action must pass an immediate Allocation Gate using live `portfolio-state.json`: current weights/ranges, authorized funding source, risk budget, correlation exposure, opportunity cost versus BTC/Cash-T-bill, fund-layer isolation, permanent-core protection and crisis-reserve protection.

## 7. Post-execution state chain

After a user-confirmed BTC/Structural/Asymmetric trade or major term-deposit decision: append Decision Journal -> update portfolio-state with confirmed execution -> update relevant cooldown/position/strategy ledger -> reread and verify. Missing price, quantity or time remains unknown; never guess.

Decision Journal automation-runtime write ability is not considered proven until the first real journal-required event successfully completes this chain. Do not create fake transactions merely to test writes.

## 8. Data honesty and source conflicts

Historical percentile/extreme/Z-score/std-dev/ATR/funding/OI/liquidation/CVD comparisons require sufficient same-definition historical data actually obtained and calculated. Current value without history => `current value verified; historical extremeness unknown`.

For facts that can change a capital action, seek at least two independent reliable sources where practical. If only one reliable source exists, reduce confidence explicitly.

**SOURCE CONFLICT RULE:** if reliable sources disagree on direction, sign, date, definition or magnitude materially enough to affect a decision, label the input `SOURCE_CONFLICT`. Do not select the preferred number silently. A conflicted input may be described qualitatively but MUST NOT be used as a confirmed PASS/FAIL input for a capital-action Gate until reconciled by an original/authoritative source or clearly harmonized definitions.

Data labels: `VERIFIED CURRENT VALUE`, `QUALITATIVE EVIDENCE ONLY`, `SOURCE_CONFLICT`, `DATA INSUFFICIENT / HISTORICAL COMPARISON UNKNOWN`. Unknown or conflict is never equivalent to normal.

---

# TASK 1 — BTC Sentinel — FINAL Architecture

Schedule: hourly condition watch.

## Required first step

Read in order: `portfolio-state.json` -> `decision-journal.json` -> `sentinel-state.json` -> this canonical logic file. Read portfolio values live every run and recompute Allocation Gate.

## Role

Identify early changes affecting BTC bear-market accumulation, late-bull distribution, liquidity, Grid risk, strategic term deposit or systemic risk. Do not redesign the portfolio architecture.

## Leading Warning Engine — USER-APPROVED AMENDMENT 2026-09-25

TASK 1 is **LEADING-WARNING FIRST**, not confirmation-first. Its primary job is to detect conditions building **before** a large BTC move. Post-move confirmation is diagnostic only and MUST NOT be the primary alert or decision trigger.

### Primary leading state
Every run must classify exactly one:
- `EARLY_UPSIDE_BUILDING`
- `EARLY_DOWNSIDE_BUILDING`
- `EARLY_ACCUMULATION_WINDOW`
- `EARLY_DISTRIBUTION_RISK`
- `NEUTRAL_MIXED`
- `SYSTEMIC_RISK`

When sufficient leading evidence exists, do not use `CANDIDATE_NOT_CONFIRMED` as the main conclusion and do not wait for breakout/breakdown confirmation.

### Leading evidence
Evaluate both direction and rate of change across:
1. ETF/spot demand: acceleration/deceleration, multi-day slope, issuer breadth, price response per dollar of flow, spot volume/CVD/order-book absorption when verifiable.
2. Price-response asymmetry: bad news failing to push BTC lower, shallower pullbacks, fast reclaims and higher lows are early upside evidence; good news failing to lift BTC, repeated rejection, weaker bounces/lower highs and support-absorption failure are early downside evidence.
3. Leverage: OI/funding/basis relative to price. Price rising without leverage expansion is healthier; price stalling while leverage expands is an early downside warning; price falling while OI is flushed and spot absorbs can support early accumulation.
4. Macro-liquidity response: changes in 2Y/10Y/30Y, USD, Fed pricing, liquidity/credit stress and BTC's response to them.
5. Supply/holder behavior: exchange flows, realized selling, SOPR/LTH and large-holder accumulation/distribution when current and definition-clear.
6. Options/gamma/skew/IV/expiry are modifiers only, never standalone directional triggers.

A directional early warning requires at least **2 causally independent evidence domains**, including at least one from ETF/spot demand, leverage, macro-liquidity response or holder/supply behavior. Price momentum alone is insufficient.

### Required decision output
Each manual run or material alert must persist and output:
- `MAIN_PATH`: one primary path for the relevant forward horizon; no symmetric “could rise/could fall” substitute.
- `LEADING_EVIDENCE`: 2–4 strongest forward-looking observations.
- `REVERSAL_TRIGGERS`: observable conditions that invalidate or flip MAIN_PATH.
- `EARLY_ACTION`: HOLD / SMALL_STAGED_ACCUMULATION_PROPOSAL / STOP_ADDING / CYCLE_DISTRIBUTION_WATCH / RISK_EXIT.
- `CONFIRMATION_STATUS`: optional diagnostic only; never headline logic.

If evidence is incomplete, output the strongest supported leading state plus `DATA_GAP` rather than mechanically waiting for confirmation. If missing/conflicting data is material to a capital action, Data Freshness Hard Gate still forces `STALE_DATA + NO NEW CAPITAL ACTION`.

### Left-side capital policy
Traditional right-side confirmation is **not required** for an A1 / early-accumulation small staged proposal when leading evidence meets the independence rule and Portfolio Allocation Gate + Drawdown Budget + Counterparty Gate all pass. Larger A2/A3/A4 deployment still requires progressively stronger multi-domain evidence. A single price level, FOMO or a single ETF print never authorizes capital.

While total BTC remains below the 0.5 BTC permanent-core floor, early downside/distribution warnings may stop additions and protect dry powder, but ordinary BTC core selling remains prohibited by the permanent-core rule.

### Anti-hindsight
Forbidden: explaining a large move after it occurs and relabeling the move itself as the signal. Persist `primary_state`, `first_detected_at`, `main_path`, `leading_evidence`, `reversal_triggers`, `early_action`, `confirmation_status` and `data_gaps` in Sentinel-owned runtime state so Detection Lead/Lag can be audited later.

### Sentinel persistence concurrency override
For `sentinel-state.json` and any other Sentinel-owned GitHub write, Shared Global Rule **1A GitHub optimistic-concurrency + retry contract** is mandatory and supersedes any older automation-prompt wording that treats the first SHA conflict as final failure. A SHA conflict MUST trigger refetch -> rebase Sentinel-owned fields only -> revalidate -> retry, up to **3 total attempts**, sequentially. Only after successful reread verification may Sentinel report `PERSISTED=TRUE`; only after retries are exhausted may it report `STATE PERSISTENCE FAILURE: CONCURRENCY_EXHAUSTED`. Never overwrite concurrent Hunter/portfolio/Grid changes and never parallel-write the same path.

## Mandatory current-data acquisition

Attempt current BTC price + daily/4H structure; latest complete US spot-BTC ETF trading-day flow; US 2Y/10Y/30Y yields, USD, Fed expectations and macro liquidity; BTC OI/funding/liquidations; major CPI/labor/FOMC/banking/credit/Treasury-function shocks. If first source fails, try at least one reliable alternative before marking unknown.

Obtain MVRV, Realized Price, SOPR, LTH, exchange flows, cost basis, CVD and order-book data only when reliable/current. Stale, secondary or definition-unclear values are not current verified evidence.

ETF flows must obey the SOURCE CONFLICT RULE. If aggregators disagree on the sign/direction of the latest session, set ETF evidence to `SOURCE_CONFLICT`; do not call the day an inflow/outflow for Gate purposes until reconciled.

## BTC buy cooldown

After a confirmed ordinary BTC buy, default 72h before another ordinary buy. Override only when price is materially lower AND valuation/liquidity/forced-selling/stabilization evidence materially upgrades versus the prior buy, with independent new evidence stated explicitly. Cooldown limits buys only.

## Bear-market Accumulation Gate

More aggressive BTC accumulation requires joint improvement across valuation, liquidity, forced selling and stabilization. Cheap does not equal buy. Preserve deeper-drawdown capital and execute in tranches.

**PRICE-LEVEL DISCIPLINE:** 72K, 65K, 58K or any other quoted level is an observation / scenario / valuation-reference level only. No price level is an automatic BTC buy trigger, ladder authorization or Gate substitute. Reaching a level may cause a fresh Gate evaluation, but executable buying still requires the evidence/Gate conditions appropriate to the proposed size, with stabilization confirmation required. Conversely, the system must not mechanically wait for a specific lower price if the Gate materially improves earlier.

If a domain lacks reliable evidence, mark UNKNOWN and state whether that gap blocks the proposed action. UNKNOWN does not automatically equal FAIL, but it cannot be forced to PASS.

## Bull-market Distribution

Track valuation, trend deviation, crowding/leverage and marginal-demand risk as NORMAL / ELEVATED / EXTREME / UNKNOWN.
0-1 EXTREME: hold. 2: stop chasing / distribution watch. 3: only in confirmed bull overvaluation/overheat/top, recommend 10%-15% of cycle inventory. 4 + price acceleration: only in confirmed bull top, additional 10%-20% of cycle inventory.

If total BTC <=0.5, cycle-sellable=0 and only `BULL-TOP RISK WATCH` is allowed. If >0.5, sale cap=total BTC-0.5. Sale proceeds return to cash for a future bear cycle; no FOMO rebuy.

## Pre-triggers

Maintain upside squeeze, downside liquidation and liquidity-turn pre-triggers. Actionable pretrigger requires at least two causally independent evidence domains, at least one from spot demand, leverage positioning or macro liquidity, and no major contradiction.

## Systemic risk

Monitor credit, Treasury/rates market function, dollar funding, banks, macro liquidity and deleveraging. Higher systemic stress => smaller/pause ordinary bear buys. $50K Crisis Reserve is deployment-eligible only after genuine systemic-liquidity shock + BTC extreme selloff + initial stabilization, staged.

## $200K strategic term deposit

Consider early break only if expected opportunity cost clearly exceeds the several-thousand-dollar interest loss. Allowed verdicts: `NOT WORTH BREAKING EARLY`, `CLOSE TO WORTH IT — WAIT FOR CONFIRMATION`, `WORTH BREAKING EARLY`.

## Notification

Notify only on material changes affecting BTC accumulation/distribution, cash allocation, Grid risk, strategic deposit or systemic risk. Include BTC price/time, regime, leading/confirming signals, four distribution dimensions, Allocation Gate, explicit action, cooldown, invalidation, 0.5 BTC core status and data-quality labels.

---

# TASK 2 — Quarterly Portfolio Review — FINAL

Schedule: every 3 months, day 1, 09:00 Asia/Ho_Chi_Minh.

Read `portfolio-state.json`, `decision-journal.json`, `state.json`, `opportunity-hunter-state.json`, then this file.

Role: post-hoc portfolio-health, state-governance and return-attribution audit. It does not approve/delay real-time trades.

Audit every confirmed execution since prior successful quarterly audit: Journal -> Portfolio -> relevant ledger/cooldown. Any break => `STATE SYNC VIOLATION`. Check shared-cash competition among strategies; Grid capital is isolated.

Quarterly checks: allocations/ranges; 0.5 BTC core; Accumulation discipline; Distribution discipline; cash cushion; Structural qualification; Asymmetric authorization/correlation/opportunity cost; Grid net benefit after fees/slippage/opportunity cost; custody/counterparty; tax/legal readiness; journal discipline; state sync; shared-cash competition; duplicate/non-owned fields across state files.

When safe deterministic state-governance defects are discovered, recommend/perform repair only when the data owner is unambiguous and no investment fact must be guessed; reread and verify. Never rewrite investment history.

Return attribution when data permits: BTC market return, BTC allocation contribution, cash, Structural, Asymmetric, Grid, fees, slippage, realized tax cost. Historical BTC cost unknown => `HISTORICAL COST BASIS RECONSTRUCTION REQUIRED`.

Benchmarks when data permits: 100% BTC; 60/40 BTC-cash annual rebalance; 50% BTC/30% broad equities/20% cash; actual strategy. Call differences incremental strategy return, not true Alpha.

---

# TASK 3 — Weekly Grid Review — FINAL Architecture

Schedule: Sunday 08:00 Asia/Ho_Chi_Minh.

Read `state.json` (Grid SSOT), `sentinel-state.json`, `portfolio-state.json` for coordination only, then this file. Suggested settings never become actual state until user confirms exchange execution.

Grid is a small isolated trading laboratory, not the primary return engine. `state.json` must contain only Grid-owned state. Portfolio cash/strategic pools/crisis reserve are forbidden duplicates.

Before recommendations report BTC SYSTEM STATE = ACCUMULATION / NEUTRAL / DISTRIBUTION / SYSTEMIC RISK / UNKNOWN. UNKNOWN is not NEUTRAL. Grid stays independent but must assess conflict with portfolio-level BTC risk management.

Use current BTC daily/4H structure, reliable volatility, funding/OI, ETF/spot demand, macro liquidity and regime. Historical metrics require actual history.

Objectives: enough transaction frequency; lower bound not so high that USDT converts too early; upper bound not so low that a trend sells too much BTC; evaluate fees/slippage/opportunity cost.

If price persistently approaches/breaks lower boundary AND structure weakens/downside expands, evaluate lower range or pause. If BTC holds above upper boundary with independent trend/flow confirmation, evaluate higher range or pause. Trend regime may justify closing/reassessing Grid. Boundary price alone does not authorize portfolio BTC buying.

Output: STATE STATUS / BTC SYSTEM STATE / CURRENT GRID / SUGGESTED GRID / CHANGE OR KEEP / WHY / SENTINEL COORDINATION / ISOLATED-USDT EFFECT / RANGE-vs-TREND / DATA QUALITY / USER ACTION REQUIRED.

Maintain 12-24m forward test versus Simple BTC + Cash after fees/slippage/opportunity cost; if no net benefit, recommend closing experiment.

---

# TASK 4 — Next-Stage Structural Asset Radar — FINAL Architecture

Schedule: Monday 09:00 Asia/Ho_Chi_Minh, flexible.

Read `portfolio-state.json`, `decision-journal.json`, then this file. Use live Structural usage/room/positions/correlation; never infer balances.

Role: discover rare structural assets before consensus fully prices them, while maintaining valuation/risk discipline. Research universe includes AI/compute/semis, robotics/automation, cybersecurity, nuclear/uranium/power/grid/storage, space/defense, advanced manufacturing, biotech, copper/rare earths, stablecoin/RWA/payments infrastructure and emerging sectors.

Research chain: Structural Change -> Industry Economics -> Value Chain -> Winner -> Value Capture -> Competitive Durability -> Financial Quality -> Reverse Valuation -> Remaining Upside vs Permanent Loss -> Opportunity Cost.

Tiers: WATCH -> RESEARCH -> CANDIDATE -> HIGH CONVICTION. Maintain immutable first-discovery record plus append-only upgrades/downgrades.

Structural BASE 15%, tactical 0%-30%, single asset <=5%; never force-fill. Funding from authorized Cash/T-bill above cushion, tactical BTC distribution cash or new capital; never permanent BTC core, Asymmetric budget, Grid or Crisis Reserve.

Radar must actively search for underappreciated structural change and attractive valuation before full consensus. HIGH CONVICTION remains required for normal 2%-3% deployment; early WATCH/CANDIDATE status is not itself executable. Do not wait for price breakout as proof of thesis.

Executable action requires HIGH CONVICTION + reasonable Reverse Valuation + Allocation Gate PASS. Initial 2%-3%, add toward 3%-5% only as evidence strengthens and valuation remains attractive.

Notify only for material candidate/tier/valuation/thesis/Allocation-Gate changes. Output asset, sector, why now, valuation, thesis, value capture, financial quality, reverse valuation, permanent-loss risk, thesis kill, correlation bucket, BTC/T-bill opportunity cost, Gate, weight/funding/invalidation.

---

# TASK 5 — Asymmetric Opportunity Scoring (v5.1)

Schedule: daily 09:00 Asia/Ho_Chi_Minh.

Read `portfolio-state.json`, `decision-journal.json`, `opportunity-hunter-state.json`, then this file. A ledger with `status=ACTIVE_INITIALIZED`, a valid timestamp and `assets={}` is healthy when Portfolio SSOT and Journal confirm zero Asymmetric positions. If a position/execution exists elsewhere but is absent from Hunter ledger, output `STATE SYNC VIOLATION` and block new actions until repaired.

Capital: total Asymmetric nominal hard cap 10,000 USDT; legacy C-Class single asset <=1,000 and all C-Class <=3,000 remain conservative references. When portfolio-level percentage caps are stricter, the stricter applicable cap governs. Never use BTC core/dip-buy cash, Grid, Structural, $200K term deposit or $50K Crisis Reserve without explicit reallocation.

Goal: identify genuine early repricing opportunities before/early in a move whose **prospective risk-adjusted return can outperform BTC over the same evaluation/holding horizon**. Large absolute multiples (3x-20x) remain valuable upside cases but are **not a minimum qualification threshold**. A candidate may qualify with a lower absolute return when evidence supports meaningful BTC-relative excess return after dilution, downside/permanent-loss risk, liquidity and opportunity cost. Typical evaluation window is weeks to ~3-18 months; 3-5y assesses upside ceiling/durability only. `NO QUALIFIED BUY` must never suppress the research watchlist.

All thresholds in this Task 5 are `PROVISIONAL` because the current six-asset sample has no statistical calibration.

## -1. Full-Universe Early Discovery — mandatory primary engine

Hunter is an **early-discovery / pre-positioning system**, not a momentum leaderboard. Every scheduled run MUST begin with a broad market-wide discovery pass before reviewing the existing watchlist. Existing candidates may never substitute for the universe scan.

Discovery objective: find assets whose **non-price evidence is improving before consensus repricing**, so the system can create immutable PRE_MOVE candidates early enough to research and, only after all capital gates pass, consider a small staged entry. Price strength is confirmation, never the primary discovery reason.

### Persistence implementation contract — PATCH v2.13.1 (operational fix; no investment-logic change)
- For Hunter-owned GitHub persistence, read repository files only by exact path on `main` using the GitHub file-content API (`fetch_file` semantics), which MUST return complete UTF-8 content plus the current blob SHA. Do not use global search, rendered-page extraction, truncated previews, or generic large-text fetches as the write source.
- State write sequence is mandatory: `fetch_file(path, main) -> parse complete JSON -> mutate only Hunter-owned current-state fields -> update_file(path, current_blob_sha, main) -> fetch_file(path, main) -> parse -> verify expected fields and new blob SHA`.
- If exact-path `fetch_file` succeeds with complete parseable content, a prior generic-reader truncation MUST NOT be reported as `STATE PERSISTENCE FAILURE`.
- `opportunity-hunter-state.json` is a bounded current-state/snapshot file. Do NOT append new historical observation events to its `observation_events` array. Existing entries are legacy history and remain immutable.
- New historical observations belong only in append-only `hunter-candidate-ledger.jsonl`. State may update the current asset/snapshot/scan/freshness view but must not duplicate the growing event history.
- A successful Slow scan with no material candidate change updates only current freshness/coverage fields in state; it does not create a duplicate ledger event unless the frozen notification/validation contract requires one.
- SHA conflict, incomplete exact-path content, JSON parse failure, write failure, or reread mismatch => `STATE PERSISTENCE FAILURE`; Fast Promotion blocked.
- This patch changes persistence mechanics only. It does NOT change Discovery, TASK5, Gate, EA, validation, universe-coverage, or capital rules.

### Universe coverage
- Start from the broad liquid crypto universe available from reliable market datasets, not a hand-picked shortlist and not only existing Hunter assets.
- Apply only investability/safety exclusions needed to avoid obviously unusable assets (e.g. non-tradable, pathological liquidity, scam/exploit/dead project evidence). Do not narrow the universe merely because an asset lacks recent momentum.
- Persist `universe_size / scanned_count / excluded_count / exclusion_reasons / coverage_ratio / scan_as_of` each run. If broad-universe coverage cannot be established, label `UNIVERSE_COVERAGE_INSUFFICIENT`; do not pretend the existing watchlist is a full scan.

### Discovery coverage closure — PATCH v2.13.6 (operational correctness; no capital-gate change)
- A run may claim `NO EARLY CANDIDATE TODAY` or `NO PRE_MOVE FOUND` only when `universe_size`, `scanned_count`, and `coverage_ratio` are numeric, the declared universe construction is reproducible, and coverage meets the run's preregistered minimum.
- If coverage is UNKNOWN, partial, non-reproducible, or below the declared minimum, output `DISCOVERY_COVERAGE_FAILURE` and `EARLY_CANDIDATE_STATUS=UNKNOWN`. Never convert incomplete coverage into a negative discovery conclusion.
- Audit accounting must distinguish `MISSED_DISCOVERY` (asset was not evaluated before the move) from `REJECTED_BY_GATE` (asset was evaluated and failed a recorded rule). An unevaluated asset may never be credited as a correct rejection.
- Prior material appreciation / prior ATH does not remove an asset from the research universe. A fundamentally qualified leader that pulls back may remain on a `REACCELERATION_WATCH`; renewed price/volume/RS may confirm re-acceleration only after the asset already has qualifying independent non-price evidence. `REACCELERATION_WATCH` is research state only and creates no BUY authority.
- Every coverage failure must persist the smallest concrete blocker and next remediation target in `scan_summary`; repeated UNKNOWN coverage without a blocker/remediation record is a system error.
- HYPE/ZEC 2026-09 audit cases are retrospective diagnostics only and MUST NOT count toward Blind Replay, k calibration, precision/recall, or OOS N.
- This patch enforces the already-mandatory Full-Universe contract. It does NOT loosen Stage, FQ, EA, Forward Upside Gate, Risk Governor, Allocation Gate, or capital limits.

### PRE_MOVE discovery evidence
Search first for non-price or weakly-price-correlated inflections: protocol revenue/fees/users/TVL quality; token value-capture activation; buyback/burn; supply/unlock/emission inflection; governance changes; product/mainnet/upgrade milestones; developer/ecosystem adoption; stablecoin/RWA/DeFi/AI/infra demand; exchange/on-chain accumulation where definition is reliable; regulatory/listing/distribution changes; valuation dislocation; neglected narrative with improving fundamentals.

A new candidate requires at least **two causally independent evidence domains**, and at least **one must be non-price**. Pure price/volume/RS/social-trending evidence can never create a PRE_MOVE candidate by itself.

### Anti-chasing stage classification
At immutable first discovery record `discovered_at / discovery_price / return_24h / return_7d / return_30d / return_90d when available`.
Classify:
- `PRE_MOVE`: thesis/evidence inflection exists and price has not materially repriced.
- `EARLY_MOVE`: repricing has begun but remaining asymmetry may still be large; requires non-price discovery evidence.
- `POST_MOVE / LATE_DISCOVERY`: first discovery occurs only after material repricing or the principal catalyst is already substantially priced.

POST_MOVE/LATE_DISCOVERY assets may remain for research/learning but **must not occupy the primary early-opportunity slots and must not be presented as Hunter discovery successes**. A strong project discovered late is explicitly a late discovery.

**Discovery quality and capital opportunity are separate axes.** Historical appreciation at first discovery (including +50%, +80%, +100% or more) is a mandatory immutable validation statistic and may establish `LATE_DISCOVERY`, but **no fixed historical-return threshold is by itself a capital hard-reject rule**. A LATE_DISCOVERY/POST_MOVE asset may still enter the normal TASK 5 capital research chain when independent non-price evidence supports the thesis. Its current investability must be determined prospectively by `DATA → VALUATION → SCENARIOS → FORWARD_RETURN_MAP → REVERSE_VALUATION → EA → FORWARD_UPSIDE_GATE → RISK_GOVERNOR → EXECUTION_STATE`, including catalyst penetration/priced-in assessment, current valuation, remaining forward upside, downside/permanent-loss risk, supply/unlocks and opportunity cost. Price momentum alone still cannot create or promote a candidate.
### PAST_GAIN_AUTO_REJECT prohibition — PATCH 2026-09-24
- **PAST_GAIN_AUTO_REJECT is forbidden.** Historical appreciation, POST_MOVE, LATE_DISCOVERY, prior breakout, or a large 7D/30D/90D return MUST NOT by itself remove an asset from current investability research.
- POST_MOVE != REJECT. Stage measures **discovery timing quality**, not current forward opportunity.
- Every materially interesting PRE_MOVE / EARLY_MOVE / POST_MOVE candidate that has sufficient liquidity and a non-price thesis MUST continue through a **Current-Price Forward Return** assessment unless another independent hard blocker applies.
- Mandatory prospective fields: current price; forward BEAR/BASE/BULL/EXTREME_BULL scenarios over the stated horizon; BTC return benchmark over the same horizon; expected BTC-relative excess return; dilution/unlocks; catalyst remaining vs priced-in; crowding; downside/permanent-loss risk; liquidity; invalidation; risk/reward.
- A previously risen asset may be rejected only for a **prospective** reason such as FORWARD_UPSIDE_INSUFFICIENT, BTC_RELATIVE_EDGE_INSUFFICIENT, RISK_REWARD_UNFAVORABLE, CATALYST_PRICED_IN, CROWDING_EXCESSIVE, DILUTION_RISK, LIQUIDITY_RISK, or another explicitly evidenced forward-looking blocker.
- Daily Hunter output MUST NOT say “already rose / POST_MOVE / too late” as the sole reason to exclude a coin. If historical appreciation raises chase risk, report it as a crowding/valuation penalty and still finish the forward-return assessment.
- Early discovery remains a separate objective: late discoveries do not count as Hunter early-discovery successes, but they remain eligible for capital research when forward asymmetry is still attractive.

### Discovery-before-confirmation separation
Slow Discovery creates candidates from the universe using information available at that timestamp. Fast Confirmation monitors only after discovery and may use price/volume/RS/flows to validate or invalidate the already-recorded thesis. Confirmation evidence must never be backfilled as if it existed at discovery.

### Daily output priority
The first Hunter output section must be `EARLY DISCOVERY BOARD`: new PRE_MOVE, then EARLY_MOVE candidates, with immutable discovery evidence and why the market may not yet have priced it. Existing POST_MOVE names go to a separate `POST_MOVE / LEARNING` section and must not crowd out early names.

If no PRE_MOVE/EARLY_MOVE asset qualifies, output `NO EARLY CANDIDATE TODAY`; never fill the board with recent winners.

### Validation / anti-process rule
Hunter success is measured by lead time, not by explaining winners after the move. Maintain `late_discovery_rate / pre_move_discovery_rate / missed_leader_rate / discovery_lead_time` through blind replay/forward validation. Architecture polishing must not replace producing these validation numbers. A candidate first found after +50%/+100% repricing is recorded as LATE_DISCOVERY, not a discovery win **for discovery-quality statistics**; this label does not automatically prohibit prospective TASK 5 capital evaluation or authorization if the remaining forward asymmetry independently passes every required Gate.


## -1A. Discovery Coverage SLA — PATCH v2.14.1

Hunter discovery must not go silent merely because no asset passes the capital gate. Every daily Task 5 run must produce a measurable discovery report before reviewing legacy candidates.

Mandatory discovery pass:
- Scan the broad liquid crypto universe first; existing watchlist review is secondary and cannot satisfy discovery coverage.
- Search both PRE_MOVE and EARLY_MOVE evidence. Prior price appreciation is NOT an automatic reject; it is only a valuation/crowding/risk input. The forward question is remaining BTC-relative upside from the current price.
- Use independent discovery families where data are available: protocol fees/revenue/value capture or buybacks; token supply/unlock/burn/emission inflections; product/mainnet/upgrade launches; TVL/stablecoin/user/volume/market-share inflections; governance/regulatory/listing/institutional-access catalysts; and abnormal spot-demand/relative-strength confirmation.
- Each run must persist: universe/scanned count where measurable, discovery families attempted, data failures, NEW candidates, upgraded/downgraded candidates, and near-miss research leads. Zero new candidates is valid only after this coverage report exists.
- Research admission is deliberately broader than BUY qualification. A plausible causal catalyst plus prospective BTC-relative upside may enter WATCH/RESEARCH even when valuation or capital gates are unresolved. This prevents the capital gate from suppressing discovery.
- Candidate promotion to executable BUY/ADD remains blocked by the normal validation, allocation, dilution, liquidity, downside and capital-authority gates. Discovery breadth does not relax risk controls.
- Anti-stall rule: two consecutive daily runs with no NEW/UPGRADED candidate AND no quantified coverage expansion must mark DISCOVERY_DEGRADED and the next run must attack the concrete missing data/source/coverage blocker rather than redesign architecture.
- Daily user-facing output must include at least: NEW, UPGRADED, DOWNGRADED, NEAR_MISS, and NO_BUY_YET. Do not suppress research candidates merely because they are not executable.


## 0. Execution order — mandatory

`DATA → VALUATION → SCENARIOS → FORWARD_RETURN_MAP → REVERSE_VALUATION → EA → FORWARD_UPSIDE_GATE → RISK_GOVERNOR → EXECUTION_STATE`

No step may be skipped or reverse-engineered from a desired EA/action.

## 1. Data freshness

Each FQ stores `{value, as_of}`. Each EA stores `{value, as_of, expires_at, status}` where status is `FRESH / STALE / INVALIDATED_BY_EVENT`.

EA maximum TTL: A-Class = 7d; B-Class/B-Development = 3d; event-driven = min(class TTL, event date/time). `STALE` or `INVALIDATED_BY_EVENT` may remain on Radar but cannot be used by any BUY/ADD Gate.

`EVENT_OVERRIDE`: unlock; governance result; tokenomics change; exploit; listing; delisting; regulatory action; protocol launch/failure; material revenue/TVL break; buyback on/off; major supply change; catalyst completion/failure. Occurrence immediately invalidates EA and forces recalculation.

## 2. FQ — Fundamental Quality

FQ answers only: `what is the token now?`

Weights:
- Adoption / Real Demand: 25
- Current Revenue / Economic Activity: 20
- Token Value Capture: 20, with this component multiplied by EM
- Competitive Durability: 15
- Supply Quality: 10
- Fundamental Sustainability: 10

Total = 100.

FQ forbidden inputs: historical price appreciation; narrative strength; catalyst proximity; drawdown; cheap valuation; future buyback; future TAM. Catalyst never enters FQ.

Normal FQ review cadence = 30d. Immediate reassessment on exploit, revenue collapse, tokenomics/value-capture change, regulation, major competitive loss or other material fundamental event.

## 3. EM — Evidence Maturity

Three states; EM is not a ranking dimension. Its multiplier applies only to the FQ Token Value Capture component:
- `UNPROVEN = 0.40`: not governed/approved or not live on-chain.
- `CONDITIONAL = 0.65`: approved but activation has prerequisites.
- `ACTIVE = 1.00`: on-chain execution has occurred and economic effect is measurable.

## 4. FF — Fundamental Floor

Token-level risk gate; changes position budget only, never ranking:
- `HIGH`: main catalyst can fail and real token-captured cash flow still supports value.
- `MEDIUM`: partial token-level economics already realized.
- `LOW`: valuation depends heavily on a future/single event; failure can collapse thesis.
- `NONE`: no token-level economic value; pure reflexivity.

## 5. Forward Return Map — mandatory EA prerequisite

First list valuation: `current_price / current_MC / current_FDV / circulating_now / future_circulating`.

All forward multiples must use future circulating supply rather than today's circulating supply. This is mandatory for ZRO, ENA, ONDO and any material-unlock/high-dilution asset.

Then output five cases: `BEAR / BASE / BULL / EXTREME_BULL / PERMANENT_LOSS`.

Every case must include `{name, target_price, target_MC, target_FDV, multiple, bucket, assumptions, failure_mode}`.

`bucket ∈ {LIKELY, PLAUSIBLE, TAIL}`. Buckets are ordinal; do not invent percentage probabilities. Map from Reverse Valuation assumption realism: realistic assumptions -> LIKELY; aggressive assumptions -> PLAUSIBLE; extreme assumptions -> TAIL. `PERMANENT_LOSS` is mandatory and distinct from volatility downside.

## 6. Reverse Valuation

For every asset, reverse-value both 3x and 5x. Derive the required `revenue / market_share / buyback / TVL / users` where economically relevant. If a metric is not economically relevant, mark `N/A` rather than fabricate it.

Classify each target `REALISTIC / AGGRESSIVE / EXTREME` and output: `Nx requires <future MC/FDV>, corresponding to <assumptions>, realism=<classification>`.

## 7. EA — Entry Asymmetry

EA is derived only after FRM + Reverse Valuation; no FRM means EA=`DATA INSUFFICIENT`.

Weights:
- Remaining Repricing: 35 — inverse of already-price-in degree using move from lows, 30/90/180d performance, MC expansion, social/narrative attention.
- RV Realism: 25 — more extreme base/bull assumptions score lower.
- Dilution-Adjusted Upside: 20 — upside calculated with future circulating supply.
- Crowding inverse: 10 — derivatives/positioning crowding.
- Invalidation Geometry: 10 — clarity/quality of invalidation.

Hard rule: catalyst already occurred AND price already repriced -> Remaining Repricing collapses -> EA must be materially reduced. Never set EA first and backfill a bull case.

## 8. Forward Upside Gate — PATCH v2.13.0 BTC-Relative Gate Hardening (FROZEN)

Status: `SHADOW` until global risk-aversion parameter `k` is calibrated by Blind Replay. While SHADOW, Gate outcomes are logged for research but `capital_eligible=0` and no BUY is authorized.

### 8a. BTC-Relative Forward Upside Gate

Per-run versioned inputs: `btc_benchmark_id`, `horizon` (must equal candidate FRM horizon), candidate `bear/base/bull` returns, BTC `bear/base/bull` returns, candidate permanent-loss case, and global calibrated `k`.

Derived: `excess_base = cand.base - btc.base`; `downside_gap = max(0, btc.bear - cand.bear)`; `hurdle = k * downside_gap`.

Evaluate top-down, first match wins:
- `FAIL / DOWNSIDE_FATAL`: candidate permanent-loss case is catastrophic with non-negligible likelihood. Absolute veto; BTC comparison is not reached.
- `FAIL / BTC_RELATIVE_UPSIDE_INSUFFICIENT`: candidate does not dominate BTC in BOTH BEAR and BASE (`cand.bear >= btc.bear AND cand.base >= btc.base`), OR `excess_base < hurdle`, OR apparent PASS exists only under EXTREME assumptions.
- `MARGINAL`: dominance holds and `excess_base >= hurdle`, but residual `excess_base-hurdle` is small, result is FRAGILE, or excess depends on a single catalyst.
- `PASS`: BEAR and BASE dominance both hold; `excess_base >= hurdle`; assumptions are not EXTREME; and the result remains true after dilution, liquidity and exit constraints.

Beating BTC only in BULL while failing BEAR/BASE dominance is FAIL. No fixed absolute-return multiple is a PASS requirement.

### 8a.4 k Calibration Protocol

`k` is the only free Gate parameter and MUST NOT be hand-set. Until calibrated, `k=UNSET` and Gate remains SHADOW. Blind Replay grid: `k ∈ {0.5,1.0,1.5,2.0}`. Objective: maximize realized BTC-relative return/drawdown of the PASS set on OUT-OF-SAMPLE historical discoveries, subject to PASS-set max drawdown not exceeding BTC drawdown over the same windows. Minimum replay sample before freezing: `N_MIN=30` discoveries. Once frozen, persist/version k in SSOT; any k change requires a new commit and re-replay.

### 8b. BTC Benchmark Construction

Exactly ONE `btc_benchmark` per Hunter run, shared by all candidates. Record `btc_benchmark_id / btc_ref_price / timestamp / horizon` at run start. Candidate and BTC horizons MUST match. BTC bands are `BEAR / BASE / BULL`; `EXTREME_BULL` is ceiling diagnostic only and excluded from Gate. Each band is a forward RETURN RANGE, not a point probability; persist the auditable representative value used for band comparison. Comparison is scenario dominance, not base-vs-base alone. Precise fabricated probabilities are prohibited.

### 8c. Fragility Test

`FRAGILE=true` if a PASS disappears when ANY of: candidate bull bucket is downgraded one ordinal level; comparable BTC band is upgraded one ordinal level; or `k` is stressed by +0.5. A FRAGILE PASS is demoted to MARGINAL.

### 8d. Reverse Valuation BTC-relative diagnostic

Keep mandatory RV 3x and RV 5x as stress-test/upside-ceiling diagnostics only; they are not capital qualification thresholds. Add non-gating `RV_BTC_REL`: valuation assumptions required for the candidate to reach `btc.base + hurdle` over the horizon, exposing how heroic the case must be to justify leaving BTC.

### 8e. EA unchanged

EA weights remain: Remaining Repricing 35 / RV Realism 25 / Dilution-Adjusted Upside 20 / Crowding inverse 10 / Invalidation Geometry 10. BTC opportunity cost is handled only in this Gate and MUST NOT be added to EA, avoiding double counting.

### 8f. Freeze Boundary

FROZEN without a proven systematic misclassification defect: Gate logic, BTC band structure, Fragility, EA weights, RV structure. Allowed without architecture change: fill candidate supply/dilution and BTC benchmark data; calibrate k by Blind Replay; correct discovery/stage/evidence tags through append-only observations. Forbidden: new modules, metrics, gates or scenario layers.

Data blockers to exit SHADOW: (1) verifiable current + forward circulating supply/dilution per candidate; (2) one versioned BTC BEAR/BASE/BULL benchmark for the horizon; (3) required universe coverage threshold; and (4) calibrated k. Capital stays zero while k is UNSET.

## 9. Comparison rule — no scalar EV

Do not calculate `Σ(p×multiple)` or invent pseudo-precise probabilities. Use a dominance test: rank A above B only if A remains superior across all reasonable bucket assignments. Otherwise output `INCOMPARABLE` and require human judgment. Low-probability high-multiple versus high-probability low-multiple defaults to `INCOMPARABLE`.

Sensitivity test: downgrade bull bucket by one level; if Gate no longer PASS, set `FRAGILE=true`.

## 10. Risk Governor

Only four outputs: `ALLOW / REDUCE_SIZE / WAIT / VETO`.

Check permanent loss, contract/technical, regulatory, liquidity/exit, concentration, unlock/emissions, token-demand failure, single-catalyst dependency, correlation, allocation and BTC/Cash opportunity cost.

`CONDITIONAL` is a WAIT substate only and must carry `{condition, action_if_met, action_if_not_met, review_deadline, evidence, owner}`. Condition must be falsifiable. If thresholds lack defensible historical/market-structure baselines, do not upgrade to ALLOW; remain WAIT.

## 11. Opportunity Signal

Boolean quadrant labels, not scores. All thresholds are PROVISIONAL:
- `DUAL-STRONG = FQ>=70 AND EA>=75 AND Gate=PASS AND EA=FRESH AND Governor!=VETO AND FRAGILE=false`.
- `EA-ONLY = FQ<55 AND EA>=80 AND Gate=PASS`.
- `QUALITY-TRAP = FQ>=80 AND EA<50`.
- `DEVELOPING = FQ 55-69 AND EA>=70 AND token Value Capture is not verified`.
- otherwise `NEUTRAL`.

Within ±3 of a threshold, mark `BORDERLINE` and require human review. `DUAL-STRONG` is never itself a BUY signal; Allocation Gate remains independent.

## 12. Class — evidence-driven migration only

Allowed classes: `A / B / HYBRID / B-DEVELOPMENT`.

`A` = fundamental-backed. `B` = reflexive/narrative. `HYBRID` = material verified fundamental + reflexive/entry characteristics. `B-DEVELOPMENT` = current thesis still primarily EA/catalyst driven while A-like features remain conditional/pending.

Migration `B-DEVELOPMENT → HYBRID → A` may occur only from already-observed on-chain/governance/economic facts; future expectations cannot drive migration.

## 13. Position sizing

- A with high FQ + high EA + mature EM + FF>=MEDIUM: normal Asymmetric sizing may be considered.
- Hybrid: interpolate conservatively from FQ/EA/EM/FF.
- B/B-Development: max size materially below A; position <= legacy C-Class cap; FF LOW/NONE applies an additional 0.5 risk-budget multiplier; EM UNPROVEN reduces further.

Hard caps always use the strictest applicable rule: Portfolio Asymmetric 8% / single asset 2% / Hunter C-Class limits. No cap creates permission to buy.

## 14. B-Class Capital Eligibility

`RADAR_ELIGIBLE != CAPITAL_ELIGIBLE`.

Before B/B-Development becomes capital-eligible, precommit asset-specific numeric execution discipline based on volatility, liquidity, market cap and catalyst window:
- `invalidation`: specific price and/or falsifiable thesis point.
- `time_stop`: N days.
- `profit_ladder`: +X% -> trim Y% steps.

No universal template. Missing any item => `BUY_SMALL PROHIBITED`, but Radar/Research remains allowed.

## 15. Execution State

Execution state is separate from Research Tier and must be exactly one of:
`RADAR-ONLY / RESEARCH / WAIT / CONDITIONAL-WAIT / CAPITAL-ELIGIBLE-SMALL / CAPITAL-ELIGIBLE / VETO`.

Delete execution use of `CANDIDATE / HIGH-CONVICTION`. A QUALITY-TRAP must carry: `EXCELLENT ASSET, NOT A HUNTER ENTRY`.

## 16. Fixed per-asset output order

`Asset | Class | FQ | FQ_as_of | EA | EA_as_of | EA_expires | EA_status | EM | FF | Signal | RiskGovernor | ExecutionState | ForwardReturnMap{Bear,Base,Bull,ExtremeBull,PermanentLoss} | ReverseValuation{3x,5x} | ForwardUpsideGate | Fragile? | Catalyst | ThesisKill | Upgrade | Downgrade`

For B/B-Development `CAPITAL-ELIGIBLE` or `CAPITAL-ELIGIBLE-SMALL`, append:
`Invalidation | TimeStop | ProfitLadder | MaxPosition | ReviewDeadline`.

## 17. Rankings and NEAREST rules

Output `FQ_RANKING` and `EA_RANKING`; only FRESH EA can be used for capital gates. Because Section 9 forbids false scalar comparability, rankings must preserve `INCOMPARABLE` when dominance is not established rather than forcing a total order.

`NEAREST-TO-A-CLASS-BUY` may be named only if all nine gates are met: FQ sufficient; EA sufficient; EA=FRESH; Forward Upside Gate=PASS; EM mature; FF acceptable; Governor=ALLOW; invalidation defined; Allocation Gate can pass.

`NEAREST-TO-B-CLASS-BUY` may be named only if: EA sufficiently high; Gate=PASS; EA=FRESH; liquidity sufficient; invalidation/time_stop/profit_ladder/max_position all defined; Governor=ALLOW.

If no asset satisfies every applicable gate, do not force a nearest candidate. Output `NO QUALIFIED BUY` while preserving the research watchlist.

## Mandatory daily summary

Every run must preserve a compact research watchlist when sufficient market data exists and report: `TODAY'S VERDICT / TOP WATCHLIST / FQ_RANKING / EA_RANKING / OPPORTUNITY SIGNALS / FORWARD RETURN MAP / REVERSE VALUATION / FORWARD UPSIDE GATES / EA FRESHNESS / Risk Governor / EXECUTION STATES / NEAREST-TO-A-CLASS-BUY / NEAREST-TO-B-CLASS-BUY / confirmed positions-actions / risks-invalidations-data gaps / Allocation Gate if capital is proposed`.

Never invent a score, bucket, future supply, valuation input or execution threshold merely to complete the template. Critical missing evidence => `DATA INSUFFICIENT`; capital action remains blocked.


---

## v2.13.5 — Persistence Transport Hardening (FROZEN OPERATIONAL PATCH)

This patch is DATA-PIPELINE ONLY. It MUST NOT change any frozen Stage / Universe / Feature / Outcome / Validation / TASK5 rule.

1. Canonical GitHub address is fixed: repository `leo14881-eng/btc-grid-state`, branch `main`. Hunter MUST NOT substitute another repository, branch, raw URL, search result, page reader, generic file reader, or cached copy for Hunter persistence.
2. All Hunter SSOT reads/writes use exact-path GitHub Contents transport. Fetch the full exact-path file on `main`; for large files, base64/blob transport is canonical. UI/tool-output truncation is NOT evidence that repository content is truncated.
3. Never build a replacement state from displayed/truncated tool output. A write is allowed only from the complete blob and its current blob SHA.
4. Single-writer persistence closure: fetch full blob + SHA -> parse -> apply Hunter-owned mutations -> validate immutable snapshots -> atomically mirror mutable asset fields to same candidate_id eligibility_context -> serialize complete state -> update_file with fetched SHA on main -> exact-path reread -> parse -> compare required contract fields.
5. Freshness proof is in the same closure. Slow may update reviewed_at/evidence_as_of/review_id/available_at only after real evidence review. reread_verified=true and fast_input_ready=true are valid only after post-write reread verification passes. Timestamp-only freshness writes are forbidden.
6. SHA conflict: follow Shared Global Rule 1A. Discard pending replacement, refetch full current blob + SHA, reapply only Hunter-owned mutation, revalidate, and retry up to the global maximum of 3 total attempts. Never overwrite concurrent changes from an old base.
7. Ledger append uses exact-path full-content read-modify-write; preserve old prefix exactly, append new observations only, write with current ledger SHA, reread and verify prefix + append.
8. Transport/decode/parse/write/reread/contract failure => STATE CONTRACT ERROR + STATE PERSISTENCE FAILURE + fast_input_ready=false; Promotion blocked. Do not diagnose GitHub truncation unless the exact-path/base64-or-blob payload itself is incomplete or unparsable.
9. Recovery: next genuine Slow evidence review executes this closure; on write+reread+contract PASS, persistence error clears and freshness resumes normally. No manual freshness repair or fabricated review.
10. This transport contract supersedes older operational instructions permitting generic/page/search/raw readers for Hunter persistence. Research logic is unchanged.
