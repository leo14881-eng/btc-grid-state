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

## 8g. Fundamental Early-Entry Capital Proposal Lane — USER-APPROVED PATCH 2026-09-25

Purpose: repair the systematic misclassification/inaction defect where a valid PRE_MOVE/EARLY_MOVE fundamental candidate can be discovered before repricing but can never reach a human capital proposal solely because the separate Blind-Replay BTC-Relative Gate remains SHADOW / k=UNSET.

This patch **does not unfreeze or validate the quantitative BTC-Relative Gate**. Its outputs remain SHADOW and may not independently authorize capital. It creates a separate, human-reviewed proposal lane for fundamental early opportunities.

A candidate may reach `FUNDAMENTAL_EARLY_ENTRY_PROPOSAL_ELIGIBLE` only when ALL are true:
1. stage is PRE_MOVE or EARLY_MOVE at the decision timestamp; POST_MOVE may enter only if prospective current-price asymmetry independently remains sufficient under the same tests;
2. at least two causally independent evidence domains exist and at least one is non-price;
3. current-price FRM is complete on one explicit horizon with BEAR/BASE/BULL/EXTREME_BULL, same-horizon BTC benchmark, dilution/unlocks, catalyst remaining/priced-in, crowding, liquidity, permanent-loss/downside, invalidation and risk/reward;
4. without using the uncalibrated k, the candidate has a defensible prospective BTC-relative edge in BASE and no fatal BEAR/permanent-loss disadvantage; uncertainty must be stated rather than converted into a fabricated probability;
5. Risk Governor is ALLOW or REDUCE_SIZE, never WAIT/VETO;
6. Portfolio Allocation Gate + Portfolio Drawdown Gate + Counterparty Gate pass;
7. funding source excludes the $50,000 Crisis Reserve and respects BTC Core / dry-powder protections;
8. proposal is explicitly staged: first tranche only. Later tranches require better price structure or new independent thesis confirmation, never mechanical averaging down;
9. user remains the sole executor. `PROPOSAL_ELIGIBLE != BUY_EXECUTED`.

The quantitative SHADOW Gate is recorded alongside this lane as a diagnostic. `k=UNSET` alone is **not** a veto for this fundamental lane. Conversely, a SHADOW PASS is never sufficient to enter this lane.

### Missed-opportunity accounting
For every PRE_MOVE/EARLY_MOVE candidate that was discovered but did not reach a first-tranche proposal before a material subsequent repricing, record `MISSED_EARLY_ENTRY` separately from `MISSED_DISCOVERY` and `REJECTED_BY_GATE`. Persist discovery price, first eligible/review timestamp when known, subsequent observed price/return, the exact blocker that prevented proposal, and whether the blocker was fundamental evidence, forward return, risk, portfolio/counterparty, data quality, or system/process latency.

A process blocker such as `k=UNSET`, Replay incompleteness, persistence engineering, or missing nonessential calibration may not be relabeled as an investment-thesis rejection.

### Operating priority
Live Full-Universe discovery + current-price Forward Return closure has priority over non-blocking Replay engineering. Blind Replay continues every run, but it must not consume the run while fresh PRE_MOVE/EARLY_MOVE candidates remain unevaluated. Normal research promotion still does not equal BUY/ADD.

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


---

## v2.16.0 — Early Optionality Entry / Anti-Late-Confirmation Patch (USER-APPROVED 2026-09-26)

Purpose: prevent Hunter from repeatedly discovering a valid opportunity early but delaying any first-tranche proposal until the asset has already materially repriced. This patch strengthens left-side discovery and proposal timing without weakening fatal-risk controls.

1. **Research completeness is not future certainty.** Hunter must investigate the asset, token economics, supply/unlocks, liquidity, catalyst, value capture, competition, permanent-loss paths and current valuation before proposing capital. It MUST NOT require the catalyst to have already succeeded, revenue to have already scaled, a breakout to have occurred, or every bullish assumption to be confirmed before a small first-tranche proposal can become eligible.
2. Add execution state `OPTIONALITY-ENTRY-ELIGIBLE-SMALL`. It is available only for PRE_MOVE / EARLY_MOVE candidates with:
   - at least 2 causally independent evidence domains, including >=1 non-price domain;
   - a real, falsifiable forward thesis and dated/observable catalyst or structural inflection;
   - current-price Forward Return Map with same-horizon BTC benchmark, even if ranges must be conservative;
   - no unresolved fatal blocker in token legitimacy, material supply/unlocks, liquidity/executability, permanent-loss risk, Counterparty Gate, Portfolio Allocation Gate or Drawdown Budget;
   - prospective remaining upside sufficient to justify the asset's incremental risk versus BTC.
3. **Unknown != veto.** Non-fatal future uncertainties (for example: catalyst not yet approved, revenue not yet scaled, adoption not yet proven) are scenario inputs and position-sizing reasons, not automatic WAIT reasons. Mark them explicitly and reduce first-tranche size when appropriate.
4. **One-cycle closure for early candidates.** A newly discovered PRE_MOVE / EARLY_MOVE candidate with sufficient non-price evidence must receive, within the same hourly discovery cycle where feasible and no later than the next hourly cycle: provisional current-price FRM, BTC-relative remaining-upside assessment, fatal-blocker check, and one of:
   - `OPTIONALITY-ENTRY-ELIGIBLE-SMALL`
   - `WAIT_PRICE_ONLY`
   - `DATA_BLOCKED_FATAL`
   - `REJECT_FORWARD_ODDS`
   - `REJECT_PERMANENT_LOSS`
   It may not remain in generic RESEARCH/WATCH solely because full confirmation has not happened.
5. **First tranche is option value, not conviction sizing.** For OPTIONALITY-ENTRY-ELIGIBLE-SMALL, propose only a small staged first tranche; normally no more than 20% of that asset's eventual maximum position, subject to Portfolio Allocation/Drawdown/Counterparty gates. If eventual max position is not yet defined, give percentage logic only and do not fabricate a USDT amount.
6. **Price discipline without missed-opportunity paralysis.** A preferred left-side zone is not a hard blocker when current-price FRM still has strong BTC-relative asymmetry. If current price is above the ideal zone but remaining upside remains compelling and crowding is not excessive, Hunter may propose a smaller optionality tranche rather than waiting indefinitely. Conversely, a large recent rise must reduce sizing / worsen valuation assumptions but is never an automatic reject.
7. **Do not wait for right-side confirmation to authorize the first tranche.** Breakout confirmation, moving-average recovery, post-catalyst revenue proof and broad market confirmation are reserved for later tranche upgrades. They are not mandatory for the optionality tranche.
8. **Repricing alarm.** Persist `decision_latency_price` and `decision_latency_return_pct` from discovery to first completed capital decision. If price moves >=15% before Hunter completes the first capital decision, label `PROCESS_LATENCY_ALERT`; >=25% => `MISSED_EARLY_ENTRY_PROCESS_FAILURE` unless a documented fatal blocker justified the delay.
9. **Candidate priority queue.** PRE_MOVE candidates with a credible non-price catalyst/value-capture/supply inflection outrank replay/calibration/architecture work. Live candidate closure always has priority over non-blocking system research.
10. **MNT-specific watch added as a current research example, not an automatic buy:** track Bybit ecosystem utility expansion, any formal revenue-based MNT buyback/burn governance progression, Mantle RWA/stablecoin activity, Treasury supply movements, circulating/FDV dilution and current-price BTC-relative FRM. Do not wait for a buyback to be fully implemented before evaluating an optionality tranche; treat proposal-stage progression as a probabilistic catalyst and size accordingly.
11. User remains sole executor. `OPTIONALITY-ENTRY-ELIGIBLE-SMALL` means a capital proposal may be surfaced early; it never means automatic execution.


## v2.17.0 — Capital Readiness + Strategy Lock + Fixed Altcoin Pool (USER-APPROVED 2026-09-26)

### A. Fixed Altcoin Capital Pool — authoritative allocation SSOT
1. The total altcoin capital pool is a fixed **20,000 USDT acquisition-cost hard cap**. This supersedes legacy Portfolio Asymmetric percentage caps (8% total / 2% single asset) for altcoin allocation decisions.
2. There is **no preset equal split and no fixed per-asset percentage cap**. Hunter/Asset Management must determine each asset's maximum position from project-specific evidence: Capital Readiness, current-price FRM, downside/permanent-loss, supply/unlocks, liquidity, catalyst/value capture, BTC-relative opportunity cost, correlation/concentration, and remaining pool capacity.
3. A confirmed altcoin buy locks its executed ORIGINAL ACQUISITION COST against the 20,000 USDT pool while that quantity remains open.
4. Live pending buy orders are reservations, not locked cost, but pre-trade checks MUST enforce:
   `LOCKED_COST + LIVE_PENDING_RESERVATIONS + NEW_PROPOSED_ORDER <= 20,000 USDT`.
   Cancelled/unfilled orders release only their reservation.
5. On a **realized profitable sale**, release the proportional original cost basis of the sold quantity from locked cost. Record realized profit separately. **Profit does not increase the 20,000 USDT pool cap** and may not silently compound the cap above 20,000.
6. If a position is closed at a realized loss, remove the closed quantity from active locked cost, record the realized loss/proceeds, and require portfolio reconciliation before treating loss-attributable capacity as reusable. Never fabricate replenishment.
7. Crisis Reserve remains excluded. BTC Core and BTC Grid are not part of this altcoin pool.
8. Every capital proposal MUST print: `ALT_POOL_CAP / LOCKED_COST / PENDING_RESERVATIONS / AVAILABLE_AFTER_RESERVATIONS / PROPOSED_ORDER / POST_ORDER_LOCKED_OR_RESERVED`.

### B. Capital Readiness Gate — mandatory before any BUY/ADD sizing
Discovery speed does not waive known-fact diligence. Before Hunter may output a concrete BUY/ADD amount or price, ALL material known-fact fields must be reviewed with primary/authoritative evidence where available:
- official tokenomics and token legitimacy;
- circulating / total / max supply and concentration;
- material 30/90/180-day unlocks, emissions, vesting, treasury/foundation/team/VC supply;
- protocol/product revenue, fees, usage and liquidity relevant to the thesis;
- exact token value-capture mechanism and whether buyback/burn is proposed, approved, funded and actually executed;
- current market cap / FDV and valuation implications;
- catalyst and priced-in assessment;
- executable venue/counterparty;
- current-price FRM with valuation derivation, same-horizon BTC benchmark, downside/permanent-loss case and invalidation;
- portfolio correlation/concentration and fixed-alt-pool capacity.

A MATERIAL UNKNOWN or unresolved primary-vs-secondary source conflict => `CAPITAL_READINESS=BLOCKED`; research/watch may continue, but concrete BUY/ADD sizing is prohibited.

Future outcome uncertainty is NOT itself a blocker: a catalyst need not already succeed, revenue need not already scale, and right-side breakout confirmation is not required for a small early tranche when all currently knowable material facts are sufficiently researched.

### C. Position sizing
Only after `CAPITAL_READINESS=PASS`:
1. derive an asset-specific `MAX_POSITION_COST_USDT`; do not inherit a stale generic cap;
2. explain why that asset deserves that amount relative to the 20,000 USDT pool and BTC/cash alternatives;
3. derive first tranche and later tranche conditions from that max;
4. verify pool invariant before every proposal;
5. user remains sole executor.

### D. Strategy Lock — stop conversational strategy drift
Once a concrete asset capital plan is presented after Capital Readiness PASS, persist a versioned `STRATEGY_LOCK` containing at minimum:
`asset / version / locked_at / evidence_as_of / max_position_cost / first_tranche / later_tranche_conditions / invalidation / exit_or_profit_ladder / alt_pool_snapshot / rationale`.

Status becomes `FROZEN`. A FROZEN strategy MUST NOT change merely because:
- the user questions it;
- price moves normally inside the modeled range;
- another sizing heuristic is remembered;
- another chat/session evaluates the same asset.

A strategy may change only for:
1. a predeclared trigger/invalidation in the frozen strategy; or
2. genuinely new MATERIAL EVIDENCE that changes valuation, supply, risk, catalyst, liquidity, counterparty or portfolio capacity.

Any change requires a persisted amendment:
`OLD -> NEW / MATERIAL_EVIDENCE / SOURCE / IMPACT / VERSION_INCREMENT / TIMESTAMP`.
No silent replacement.

### E. ASTER process correction
The prior ASTER concrete sizing proposal is withdrawn and is NOT a frozen strategy. ASTER remains `CAPITAL_READINESS=BLOCKED` until its forward supply/unlock schedule and source conflicts are reconciled and its FRM is rebuilt from valuation inputs. Do not revive the old 160/400/600/800/2000/3000 USDT figures.

### F. STX continuity
Existing user-confirmed STX fills remain valid and locked against the altcoin pool. The existing 600 USDT @ 0.308 pending plan remains a reservation unless filled/cancelled by the user. This v2.17 accounting patch does not itself alter the STX trading thesis or order.


## v2.17.1 — Existing Candidate Opportunity Capture (USER-APPROVED 2026-09-26)

Goal: Hunter must capture investable opportunities, not merely produce research.

### 1. Dual queue every live run
Every run processes both queues:
- NEW_DISCOVERY_QUEUE: new PRE_MOVE/EARLY_MOVE assets.
- EXISTING_CANDIDATE_QUEUE: every non-permanently-rejected candidate already in Hunter.
Existing candidates may not be skipped merely because new candidates were found.

### 2. Mandatory repricing/catalyst trigger
Refresh current price and new non-price evidence for existing candidates. Same-cycle Capital Readiness/FRM re-evaluation becomes mandatory when any trigger fires:
- absolute 24h move >= 8%;
- absolute move from last reviewed price >= 12%;
- new material governance/tokenomics/unlock/product/revenue/value-capture catalyst;
- price enters a previously defined left-side research/buy zone.
Triggered existing candidates outrank routine new-candidate research.

### 3. Latency accountability
If an existing or discoverable candidate appreciates >=15% before the required capital decision, persist PROCESS_LATENCY_ALERT.
If it appreciates >=25% before decision and no contemporaneous documented fatal blocker justified waiting, persist MISSED_EARLY_ENTRY_PROCESS_FAILURE.
Classify failure as MISSED_DISCOVERY, MISSED_REEVALUATION, or VALID_REJECTION. Do not rewrite history.

### 4. Opportunity-capture KPI
Persist rolling metrics:
- material_candidates_detected;
- capital_ready_before_15pct_repricing;
- missed_discovery_count;
- missed_reevaluation_count;
- valid_rejection_count;
- profitable_frozen_strategies;
- losing_frozen_strategies;
- realized_alt_pnl;
- BTC-relative realized pnl.
Research volume is NOT success. Success is early discovery + complete diligence + timely capital decision + realized risk-adjusted outcome.

### 5. Decision SLA
For a candidate with >=2 causally independent evidence domains including >=1 non-price domain:
- create provisional research record immediately;
- complete currently knowable Capital Readiness fields in the same cycle where feasible;
- hard maximum next hourly cycle for unresolved non-fatal fields;
- a material factual blocker must name the exact missing fact and the active remediation source/path.
No generic WATCH/RESEARCH state may persist without a concrete blocker or price condition.

### 6. ENA defect classification
ENA is a process-audit case:
- material discoverable evidence existed by 2026-08-27 (investor-overhang restructuring + fee-switch proposal);
- Hunter first recorded ENA on 2026-09-19 after material repricing;
- later Hunter runs failed to force-refresh ENA despite additional material repricing/catalysts.
Classify as MISSED_DISCOVERY + MISSED_REEVALUATION, not VALID_REJECTION.
Do not chase ENA merely to compensate for the miss. Rebuild current-price Capital Readiness and forward odds.


## v2.17.2 — Explosive Upside / Non-Continuous BTC Outperformance (USER-APPROVED 2026-09-26)

Purpose: Hunter must not reject an asymmetric altcoin merely because it is expected to underperform BTC during an intermediate sub-period. The objective is to capture assets capable of a later discontinuous/explosive repricing from the acquisition price.

1. BTC-relative performance is an OPPORTUNITY-COST BENCHMARK, not a requirement that the altcoin outperform BTC continuously or in every short horizon.
2. Remove any rule that rejects a candidate solely because its 1M/3M/base-path return is below BTC while a later thesis-driven repricing window remains credible.
3. Add EXPLOSIVE_UPSIDE_MAP for each serious candidate:
   - acquisition/current price;
   - plausible catalyst window, expressed as a range rather than a fabricated exact date;
   - pre-catalyst downside / dilution / drawdown;
   - base terminal value;
   - explosive/bull terminal value;
   - extreme terminal value;
   - catalyst required for each higher bucket;
   - probability is not fabricated when evidence cannot support it;
   - permanent-loss path and thesis invalidation.
4. A candidate may be capital-eligible before it is expected to beat BTC in the near term when ALL are true:
   - material known facts pass Capital Readiness;
   - there is a credible non-price mechanism for later repricing (revenue/value capture, supply inflection, product/adoption inflection, governance implementation, major distribution/access change, or other falsifiable catalyst);
   - prospective explosive upside from entry is materially larger than BTC's plausible upside over the full thesis horizon;
   - downside/permanent-loss and dilution are bounded enough to justify staged sizing;
   - liquidity/counterparty/portfolio gates pass.
5. Near-term BTC underperformance affects SIZE and ENTRY PRICE, not automatic eligibility.
6. For assets with a known near-term supply shock before a later catalyst, explicitly model two phases:
   PHASE_A = pre-catalyst/supply-risk window;
   PHASE_B = catalyst/value-capture repricing window.
   Hunter may establish a small left-side optionality tranche in PHASE_A if Capital Readiness passes, rather than waiting for PHASE_B confirmation.
7. Reject only when prospective terminal explosive upside is insufficient for the permanent-loss/dilution risk, the catalyst lacks a defensible causal path to token value, or material facts remain unresolved.
8. Current-price FRM remains mandatory, but BASE underperformance versus BTC alone is NOT a hard FAIL.

## v2.17.3 — Candidate Closure / Fatal-Blocker Escalation (2026-09-26 process repair)

Purpose: enforce the EXISTING v2.16/v2.17 discovery-to-decision SLA. This is an operational closure and audit patch, NOT a change to frozen market-data scoring, validation thresholds, supply guards, capital authority or the user's approved strategy.

1. **Priority inversion is prohibited.** At the beginning of each Hunter run, process existing triggered candidates before routine architecture work. Priority order: (a) newly triggered >=8% 24h or >=12% since review, or new material catalyst; (b) PRE_MOVE/EARLY_MOVE candidates with an unresolved fatal blocker and approaching deadline; (c) fresh discovery; (d) routine replay/calibration. Run both queues; report partial coverage honestly.
2. **Every triggered candidate must close the loop.** Persist `triggered_at`, `trigger_reason`, `decision_due_at` (next scheduled hourly Hunter cycle, not a fictional exact timer), `last_attempt_at`, `decision_status`, `blocker_type`, `exact_missing_fact`, `remediation_source`, `remediation_owner=HUNTER`, and `next_check_condition`. Valid statuses are `PROPOSAL_READY`, `WAIT_PRICE_ONLY`, `DATA_BLOCKED_FATAL`, `REJECT_FORWARD_ODDS`, `REJECT_PERMANENT_LOSS`, and `SOURCE_UNAVAILABLE_ESCALATED`. No generic `WATCH` closes a triggered review.
3. **Fatal facts get an escalation path, not an indefinite parking lot.** In the same run, attempt the primary source, an independent cross-check and a clearly identified reconciliation path for each material supply/unlock conflict. On next scheduled hourly cycle, if still unresolved, set `SOURCE_UNAVAILABLE_ESCALATED` with the precise conflicting values/definitions and a new event-based check; report that the missed entry is caused by a material data blocker. Do not invent missing circulating supply or unlock quantities, and do not convert unresolved fatal data to a small BUY.
4. **Separate actionable from non-actionable unknowns.** Unknown future catalyst success, uncertain price timing, unproven revenue scale and incomplete nonessential replay calibration are scenario/position-size inputs, never fatal supply-data blockers. Unknown currently sellable insider supply, material forward releases, token legitimacy or venue execution are fatal when they can alter the entry decision. Record the distinction.
5. **Pre-repricing decision snapshot.** Before any later price move is known, freeze observed price, contemporaneous evidence, forward-return assumptions, exact reason for proposal/rejection/block, and next re-evaluation trigger in the append-only candidate ledger. Subsequent price changes can trigger a new decision but cannot rewrite the earlier one. `MISSED_DISCOVERY`, `MISSED_REEVALUATION`, `MISSED_EARLY_ENTRY_PROCESS_FAILURE`, and `VALID_FATAL_BLOCK` are distinct and can coexist only when the audit facts support them.
6. **Alerts must be useful.** Notify for newly eligible small first-tranche proposals (never auto-execute), materially changed blocker resolution, candidate >=15% appreciation before closure, missed next-cycle SLA, or new fatal supply evidence. Ordinary unchanged WATCH heartbeat is silent. A missed SLA must state which required source was attempted and what action remains.
7. **Portfolio veto remains absolute.** No concrete BUY/ADD proposal without fresh current-price FRM and supply reconciliation where material, Portfolio Allocation Gate, Drawdown Budget, Counterparty Gate and user execution authority. Altcoin locked cost plus pending reservations plus proposed orders <=20,000 USDT; 50,000 USD Crisis Reserve excluded; BTC permanent core protected.
8. **Verification contract.** Hunter run output must include `closure_queue` (trigger, status, blocker, due/attempt, next action) and `coverage_status`. A successful rules-file update alone does NOT mean a live scan ran, a source conflict was resolved, or an automated scheduler was deployed. A run with unresolved triggered candidates must say `CAPITAL_DECISION_BLOCKED` and identify them, not report generic success.

Immediate audit cases: PUMP = early discovery followed by material forward supply-source conflict; ENA = late discovery + missed re-evaluation with material Oct-5 sellable-fraction uncertainty. Do not erase or backdate their existing first-discovery events. Existing STX 600 USDT @ 0.308 is a pending user plan, not a confirmed fill.

## v2.17.4 — Hunter Exchange-Universe Data Input (2026-09-26 scope approval)

User-approved live-discovery universe is **ALL active Binance OR Bybit USDT spot pairs**, deduplicated provisionally by base ticker. This operational input specification does not relax frozen validation, capital gates or user execution authority. Never substitute hand-picked 77-coin sector baskets or broad web searches for a verified CEX market scan.

At the beginning of each Hunter cycle, read `research/results/hunter-cex-universe-summary.json` and `research/results/hunter-cex-universe-run.json` from current GitHub main if present. The hourly `.github/workflows/hunter-cex-universe.yml` collector scans full Binance/Bybit active USDT spot listings with venue-specific completeness checks. Require the report's `as_of_utc` and per-venue `active_pairs`, `valid_pairs`, `missing_or_invalid`, and `errors`; never claim both-venue coverage when `complete=false`.

First actual runner evidence (2026-09-26 08:02 UTC): Binance market-data-only host returned **490 active USDT spot pairs / 490 valid**; Bybit global and official backup returned **403 from GitHub's US-hosted runner**. Thus `binance_complete=true`, `bybit_complete=false`, `coverage_status=BINANCE_COMPLETE_BYBIT_UNAVAILABLE`. This is a valid **Binance-only full scan**, NOT a Binance+Bybit union full scan. No fabricated Bybit prices or ticker counts. Bybit access requires an authorized compatible execution environment or verifiable licensed data feed; no evasion of regional restrictions.

Feed all `research_leads` into the existing NEW_DISCOVERY_QUEUE and compare existing candidate prices to their frozen previous reviews. `PRE_MOVE_WATCH`, `EARLY_MOVE`, and `POST_MOVE` from this scanner are **price-only research labels**, not independently validated Hunter stages or capital authorization. Apply official project/supply/unlock/contract-identity checks, independent non-price evidence, FRM and all frozen gates before any proposal. In particular, do not drop PUMP due to its symbol ending in UP; the scanner removed naive suffix exclusion. Keep a timestamped audit of misses and coverage gaps. If Binance scan fails, report partial/failed coverage; if Bybit 403 persists, retain Binance complete output and report the exact gap rather than repeatedly declaring total market failure.

## v2.17.5 — Prospective Explosive Upside Is the Sole Discovery Objective (2026-09-26 user correction)

**Authoritative objective:** Scan ALL active Binance OR Bybit USDT spot pairs, including previously appreciated assets, to discover and re-evaluate tokens with credible, material **future** upside from the CURRENT executable price. Find opportunities early **relative to the NEXT meaningful repricing leg**, not early relative to listing or lifetime price history. The desired outcome is to establish positions before substantial REMAINING appreciation, never to buy solely because the asset has not yet pumped.

1. **No historical-performance gate or PRE_MOVE priority bias.** Previous 24h/7d/30d gains, drawdowns, ATH distance, first Hunter discovery timing, and labels PRE_MOVE/EARLY_MOVE/POST_MOVE are descriptive features only. None automatically grants priority, disqualifies a token, or caps its forward return. A token up 100% can rank ahead of a token down 90% if its remaining forward expected upside, catalyst durability and risk-adjusted payoff are superior.
2. **Prospective ranking:** rank candidates by evidence-backed CURRENT-price 3–6 month and 1–3 year forward return distributions, credible catalyst/monetization path, scenario-weighted asymmetric upside versus permanent-loss and drawdown risk, supply/dilution and executable liquidity. Compare with BTC over matching horizons as opportunity cost, NOT a requirement to outperform BTC in every short window. Never assert a certain price target or guaranteed profit.
3. **Two equal entry lanes:** (A) pre-catalyst accumulation when evidence supports a material upcoming rerating; (B) proven-catalyst continuation when the project has already rallied but a credible second/third rerating leg remains. Both can lead to small staged LEFT-SIDE entries at favorable risk/reward relative to the future catalyst; price breakout alone is NOT a buy signal.
4. **Continuous re-evaluation:** every market scan checks ALL listed eligible pairs and all existing Hunter candidates; material price changes, catalyst developments, revised revenues, supply events or new relative-value changes cause a fresh CURRENT-price FRM. Do not dismiss previous candidates because they moved; do not mechanically chase because they moved.
5. **Mandatory outputs:** exchange coverage and freshness, forward-upside candidate shortlist regardless of historical return, current-price forward return map with downside and BTC comparator, catalyst thesis and invalidation, supply/unlock/venue status, proposed entry zone/tranches ONLY if every capital gate passes, and explicit missed-opportunity audit. A price-only screening lead is NOT a capital proposal.
6. **Capital controls unchanged:** fixed 20,000 USDT altcoin original-cost hard cap including locked cost and live pending reservations; BTC core and 50,000 USD crisis reserve remain protected. Execution is USER ONLY. Preserve prior immutable snapshots and record this change prospectively; do not backdate any old discovery.
7. **Coverage honesty:** Binance-only 490/490 scan with Bybit 403 remains Binance-complete/union-incomplete. Full union claims require both venues verified; no fallback hand-picked baskets or fabricated prices.

## v2.17.5 — Forward Upside First: Past Returns Are NOT an Eligibility Gate (2026-09-26 user correction)

**Supersedes any language suggesting PRE_MOVE or not-yet-rallied tokens deserve inherent priority.** Hunter's sole discovery objective is to identify Binance OR Bybit active USDT spot tokens that may appreciate substantially **FROM THE CURRENT AVAILABLE ENTRY PRICE** over a defensible catalyst/investment horizon, with acceptable risk-adjusted upside, and to identify an actionable entry **before the next material repricing**, regardless of whether the token previously gained 0%, 50%, 200%, or declined 90%. Earlier price action is context for current valuation, crowding, drawdown and remaining upside; never an automatic rejection or ranking bonus.

1. **Universe coverage:** All valid listed spot bases enter a forward-upside research queue on each complete venue scan, including BASELINE, PRE_MOVE_WATCH, EARLY_MOVE and POST_MOVE. No top-150 truncation or stage-based selection priority. If research capacity is limited, report backlog and explicitly separate scan coverage from completed deep-research coverage.
2. **Prospective candidate evaluation:** For each research candidate, derive current price, market cap and diluted supply; credible token-specific catalyst and adoption/revenue/value-capture path; future 30/90/180-day float/unlocks/emissions; venue depth and exit liquidity; bear/base/bull/extreme *forward terminal price ranges* with explicit assumptions; plausible BTC forward alternative for same horizon; permanent-loss and thesis-invalidation path. Unknown facts remain explicit; do not fabricate probability or assert that large historical declines imply rebound.
3. **Prior rallies:** +100% previously can still qualify if prospective value capture and remaining upside justify the CURRENT entry price; a -90% token can fail if no credible catalyst or dilution dominates. Do not infer future return from prior percentage gain/loss alone.
4. **Entry timing:** Prefer evidence-backed entry *ahead of the next catalyst/repricing*, not necessarily before the token's first rally. Evaluate current entry, pullback limit, and catalyst-event entry against expected forward payoff, liquidity and invalidation. Avoid both FOMO and a rigid no-chase ban; a strong current-price FRM can justify an entry after prior appreciation if all capital gates pass.
5. **Outputs:** Distinguish `FULL_CEX_MARKET_COVERAGE` (listed-asset data collection), `FORWARD_UPSIDE_RESEARCH_COVERAGE` (candidates with evidence-backed terminal maps), `CAPITAL_READY`, `DATA_BLOCKED` and `REJECTED`. Do not claim all assets have been deeply researched merely because prices were scanned.
6. **Execution authority and caps unchanged:** No automatic orders. All original-cost altcoin positions plus pending orders plus new proposed order <=20,000 USDT; preserve crisis reserve, BTC core, and existing frozen material supply/portfolio gates.

Collector implementation: `research/hunter_cex_scan.py` now emits all scanned assets into `research_leads` without stage-based ranking/truncation; `stage` remains a descriptive price-only alert label, not investment eligibility. This code change does NOT by itself implement automated fundamental/FRM research across all assets. Verify tests and next live collector run independently.

## v2.17.6 — Operational Forward Research Consumption (2026-09-26)

The GitHub hourly `hunter-cex-universe.yml` runs `hunter_cex_scan.py` then `hunter_forward_research.py`. The second script reviews the entire listed universe at a lightweight level, rotates a batch of 25 assets for deeper public-data collection, and independently prioritizes up to 10 price-shock triggers per run. Prior gains/losses do NOT determine eligibility. Results persist in `research/results/hunter-forward-research.json`; public enrichment cache is `research/results/hunter-market-enrichment.json`.

At the start of each existing hourly Hunter Discovery + Replay task, read BOTH the latest CEX coverage report and forward research report. Report distinct counts: venue-listed assets, market-scanned assets, deep-reviewed-this-cycle, total distinct deep-reviewed-cached, triggered, trigger backlog, market-enrichment errors and stale timestamps. Never conflate rotating public-data enrichment with fully verified project fundamentals or a capital-ready recommendation.

The ChatGPT Hunter task owns evidence-based interpretation, NOT blind trust in an automatic price or TVL score. Investigate the highest-value triggered cases and promising cross-sector non-price observations from the new report, including existing closure queue, without anchoring on historical percentage gains. For any candidate to be promoted beyond research-only, independently verify official contract identity, actual 30/90/180-day sellable supply/unlocks, causal tokenholder value capture and dated catalyst, realistic current-price bear/base/bull outcomes and permanent-loss risk, fresh venue execution and portfolio gates. Where facts remain unknown, record the exact blocker and source attempts; continue other research.

A green GitHub job means its software tests, market collection and research data pipeline completed; it does NOT certify future-upside forecasting performance, capital readiness, exchange coverage beyond reported `coverage_status`, or successful ChatGPT push delivery. Bybit 403 is nonfatal for an explicitly labelled Binance-complete scan. On any GitHub failure or unexpected drop in valid Binance pairs, alert with the run URL and failing step. Retain existing 20,000 USDT alt original-cost cap, portfolio gates and user-only trade execution.

## v2.17.7 — Point-in-Time Forward Audit and Research Dossiers (2026-09-26)

The existing hourly GitHub Hunter workflow now has four sequential components: (1) full CEX market collection, (2) rotating public-data enrichment for all market assets over successive runs, (3) immutable-at-observation prospective event and all-asset first-seen baselines in `research/results/hunter-forward-audit.json` plus its small summary, and (4) research case files in `research/results/hunter-candidate-dossiers.json`.

The hourly ChatGPT Hunter task must consume the latest candidate dossiers and audit summary, alongside the full CEX report and research results. Cases are ordered for research attention by material triggers, evidence availability and operational freshness; this is NOT an investment return score, and a prior rally is NEVER an exclusion. A dossier's market structure and protocol TVL are research leads, not independent tokenholder cash flows. Missing verified contract, official tokenomics, actual future sellable supply, dated catalyst, causal token value capture, fresh market price or portfolio gates prevents capital proposals but does not stop the rest of the market scan.

For any numeric terminal-price map, `research/hunter-verified-facts.json` must contain independently reviewed, timestamped official source URLs and explicitly justified future circulating supply and bear/base/bull market-cap assumptions. The dossier engine only computes the arithmetic from these analyst inputs and marks it `HYPOTHETICAL_SCENARIOS_NOT_PREDICTIONS`; it does not invent probabilities or grant trade authority. Empty facts registry is expected until independent verification succeeds. The ChatGPT task may independently research and populate verified facts with auditable official references after reconciling supply and tokenholder value capture; do not set verification booleans merely to obtain a target price.

Anti-hindsight: `hunter-forward-audit.json` freezes first-seen prices for ALL listed assets and separate research events only when research actually occurred, with evidence, source links, first-observed price and same-venue BTC baseline. Future 24h/7d/30d/90d outcomes are written only after time elapses, and late observations are labelled. Do not use future outcome data to retroactively create or change research signals or claim early discovery. Compare outcomes with all-asset controls only after sufficient genuine samples and source coverage.

Completion is three separate milestones: LIVE_DATA_PIPELINE_VERIFIED, FORWARD_RESEARCH_PIPELINE_VERIFIED, and PREDICTIVE_ALPHA_VALIDATED. Successful hourly Actions and zero buy proposals can establish the first two but NEVER the third. User-only execution and all existing 20,000 USDT alt original-cost, BTC-core and crisis-reserve gates remain unchanged.

## v2.17.8 — Independent Sourced Buy-Gate Findings (2026-09-26)

At every existing hourly Hunter review, READ `research/reviews/2026-09-26-forward-buy-gate-review.md` and any subsequent dated independent buy-gate reviews before promoting new candidate dossiers. The sourced Sep26 review identifies DOLO's real protocol revenue but conditional/unactivated direct tokenholder fee switch, reconciled official DOLO contract and an unresolved vesting-vs-circulating supply discrepancy; AERO's real veAERO revenue, continuous issuance and pending Aero/Velodrome merger; KMNO Sep30 contributor unlock and stale/no verified direct KMNO fee capture; 2Z Oct2 major unlock; MUBARAK's sentiment/concentration uncertainty; PHA's recent extreme price run-up and unverified tokenholder cloud revenue capture. These are source-specific constraints, NOT price-stage-based disqualifications. The most important fresh non-obvious research lead from this review is DOLO; DOLO must NOT be presented as approved to buy until exact sellable supply, actual holder economics, fresh spot depth and portfolio gates are verified. AERO's near-high price and merger require independent migration and dilution valuation before entry. Do not fill verified-facts registry with guessed future prices or claim an audit PASS based on a qualitative review. Revisit with new evidence, update dated review and notify only when capital gating materially changes. Maintain all 20,000 USDT original-cost alt exposure limits and user-only execution.

## v2.17.9 — Balanced Opportunity Lanes and Conditional Capital Gate

Following 2026-09-26 review, the previous candidate-dossier code had a structural defect: it displayed only 25 cases sorted predominantly by current market attention, permanently set `capital_ready=False`, and returned `buy_proposals=[]` unconditionally. This has been corrected in `research/hunter_candidate_dossiers.py`: separate early-flow and continuation-upside lanes now reserve 16 and 12 review slots respectively within a 40-case report, with other candidates filling unused slots; neither prior rallies nor early flat price action automatically disqualifies a project. The report publishes `early_entry_watchlist`, `continuation_watchlist`, `cohort_coverage` and distinct, source-grounded capital gate blockers. The capital gate can generate a USER_REVIEW_REQUIRED proposal if and only if dated independently verified contract/forward supply/value capture/catalyst and analyst scenario inputs, fresh executable orderbook, same-horizon BTC comparison, downside budget, counterparty review and the 20,000 USDT original-cost pool all pass. No automatic trades. This removes an impossible hard-coded false without inventing data or lowering fatal safety controls.

The hourly Hunter ChatGPT task MUST read both watchlists, including early-flow cases NOT appearing in the highest-gainer cohort, then do targeted official-catalyst and fresh-execution research. The Sep26 scan contains early-flow review leads COMP (7d +12.1%, volume 4.7x), CFX (+9.9%, 3.0x), AR (+3.3%, 3.9x), and DUSK (+17.8%, 2.7x). These are observations, NOT buy signals; prices are snapshot-only and token-specific rights/unlocks remain to be verified. Separate `RESEARCH_LEAD`, `CONDITIONAL_ENTRY_REVIEW`, `CAPITAL_REVIEW_ELIGIBLE`, and `EXECUTED_BY_USER` states; report exact blocking evidence for each, prioritize resolving blockers rather than repeatedly returning an undifferentiated zero. After deployment verify actual cohort counts and output from the latest GitHub Actions run; never call code committed but untested 'completed'.

## v2.17.10 — Hourly live liquidity evidence and automatic verification (2026-09-26)

The production Hunter workflow now automatically runs `research/hunter_liquidity_probe.py` after fresh scan/research/audit/dossier stages. Its persisted `research/results/hunter-liquidity-probe.json` provides timestamped, public Binance 100-level order-book best bid/ask, spread in bps, and visible bid/ask USD depth within 2% for up to eight early-flow and eight continuation candidates per cycle. The orderbook is partial and may be spoofed or change instantly; this is execution *evidence*, not guaranteed fillable liquidity, independent fundamentals or a trade signal. Each hourly ChatGPT Hunter review must read this file, check the timestamp, report source-specific failures, and cross-check any proposed trade with a newly refreshed book before making a user-review-only proposal. Do not mark `liquidity_verified_at_utc`, `counterparty_verified`, portfolio balances, unlocks, or terminal market-cap assumptions as independently verified merely because this probe ran. If no official contract/supply/value capture/catalyst evidence exists, explicitly identify and research the missing primary sources instead of looping on infrastructure or inventing targets. The GitHub workflow runs regression tests and each stage, persists all output with rebase/push retries, and fails visibly when critical stages fail. A successful Actions run does not validate future market alpha. Bybit geo-restricted 403 remains separate, and Binance coverage must never be relabeled combined full coverage.


## v2.17.11 — Hourly Hunter self-check and repair protocol

User authorized iterative self-repair and end-to-end regression tests. The hourly GitHub workflow now runs an asset-type/contract-identity audit before dossier creation and re-runs the dossier capital gate AFTER live Binance orderbook collection. It also emits `research/results/hunter-health-and-queue.json` with cross-stage timestamp integrity, Binance/Bybit actual coverage, 24h forward audit sample count, contract corroboration counts, liquidity failures, exact unresolved blockers and a balanced early/continuation evidence queue. This is a real automated self-check, NOT a guarantee of autonomously repairing arbitrary novel code bugs or discovering profitable trades. Contract identity is independently corroborated only when an analyst-attested dated official contract, the matching Binance spot pair and a fresh matching third-party platform contract agree; symbol-only CoinGecko data is NEVER proof. Public partial orderbook cannot bypass stale venue, source identity, portfolio or counterparty gates; manually entered liquidity fields cannot override live data.

Every scheduled ChatGPT Hunter review MUST read `research/results/hunter-health-and-queue.json`, `research/results/hunter-identity-audit.json`, `research/results/hunter-liquidity-probe.json` and latest GitHub Actions job results, then address the FIRST actionable unresolved defect from the persisted priority queue. For an ordinary transient external API failure, keep timestamped old evidence marked stale, retry on the next scheduled cycle, and report persistent source outages without corrupting market coverage. For a reproducible owned-code regression with clear isolated fix, fetch current file SHA, apply the minimal change, add a failing regression test, run the GitHub Actions workflow, inspect job failures, and only claim completion after a full successful persisted run. If a new bug is uncertain or official evidence is missing, report the exact blocker and preserve capital hard gates; do not silently edit frozen strategy, invent evidence or automatically place orders. Notify the user in Chinese upon a meaningful verified repair, a persistent system failure, or a newly defensible capital-review proposal; otherwise keep ordinary unchanged hourly heartbeat quiet. The user has explicitly confirmed that notifications are arriving in their app despite the automation settings showing notifications disabled, so do not assert push delivery failure from that flag alone.


## v2.17.12 — Rotating project-source discovery and verified identity seed

The production hourly Hunter pipeline now emits `research/results/hunter-primary-source-leads.json` from `research/hunter_primary_source_discovery.py`: rate-limited, cached, balanced early-flow and continuation research links (homepage, whitepaper, explorer, GitHub) for up to six new candidate identities each run. Every link is third-party-supplied and marked UNVERIFIED_LINK_DISCOVERY_ONLY until an independent official source is actually opened and its token contract, tokenholder rights, supply and dated catalyst claims checked. NEVER promote a project from this link list directly to capital ready or claim a CoinGecko ticker match proves project identity. The task should read this output and address the first actionable missing primary-source review from the health queue; prefer a small number of thoroughly reviewed cases over continuously expanding an unaudited catalog. Initial verified-facts registry contains only a sourced COMP Ethereum contract identity from Compound's own governance page, with token value capture, forward supply, catalyst and all market-cap scenarios deliberately unverified. The independent contract corroboration script now also supports explicitly attested small-cap CoinGecko IDs absent from top500 and independently checked native chain assets; identity pass still requires fresh matching independent source and explicit analyst attestation. Source-collection failure or 429 is a reported research gap, not an exchange scan failure and not a reason to fabricate verified facts.


## v2.17.13 — Shared source cooldown, identity-safe leads, honest coverage and Bybit fallback

The hourly Hunter pipeline now shares a persisted CoinGecko 429 cooldown at `research/results/hunter-coingecko-throttle.json` between official-contract identity corroboration and third-party project-link discovery. Honor the timestamp and Retry-After; do not treat intentionally deferred requests as missing projects, and do not immediately retry on the next stage. Cached source leads are only valid for the exact current CoinGecko ID, not the ticker alone; if the ID changes, old project links are hidden. The Bybit scanner attempts the primary and one alternate **official** Bybit API host after 403/451; if both fail, keep Bybit incomplete, never infer its prices or market coverage from Binance. The health report now distinguishes `market_data_screened_cached` (public market observations, NOT fundamental analysis), `official_contract_identity_verified` and `forward_economic_scenario_ready`. Never describe all market-screened assets as fully fundamentally researched. Prioritize genuine official supply, unlock, value-accrual and catalyst evidence for the next capital review. These changes passed 73 regression tests and a complete persisted GitHub Actions run 36233488398; neither the tests nor public depth observations prove predictive alpha or imply a BUY.
