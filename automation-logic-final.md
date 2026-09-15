# Investment Automations — Canonical Logic FINAL

Version: 2026-09-15 v1.3
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

# TASK 5 — Asymmetric Opportunity Hunter — v5.0 FINAL

Schedule: daily 09:00 Asia/Ho_Chi_Minh.

Read `portfolio-state.json`, `decision-journal.json`, `opportunity-hunter-state.json`, then this file.

A ledger with `status=ACTIVE_INITIALIZED`, a valid timestamp and `assets={}` is healthy when Portfolio SSOT and Journal confirm zero Asymmetric positions. Do not block a first trade merely because assets is empty. If a position/execution exists elsewhere but is absent from Hunter ledger, output `STATE SYNC VIOLATION` and block new actions until repaired.

Capital: total Asymmetric nominal hard cap 10,000 USDT; legacy C-Class single asset <=1,000 and all C-Class <=3,000 remain conservative references until class migration is fully reconciled. When portfolio-level percentage caps are stricter than Hunter nominal caps, the stricter applicable cap governs. Never use BTC core/dip-buy cash, Grid, Structural, $200K term deposit or $50K Crisis Reserve without explicit reallocation.

Goal: identify genuine early asymmetric repricing opportunities, including fundamental-backed and reflexive/narrative opportunities, before/early in a move while avoiding high-FDV, unlock, value-capture and permanent-loss traps. Typical target window is weeks to ~3-18 months for 3x-20x; 3-5y assesses upside ceiling/durability only. `NO QUALIFIED BUY` must never suppress the research watchlist.

## Core architecture — classification + split axes + gates

Never compress quality, entry and safety into one score.

`QUALITY ≠ ENTRY`
`ENTRY ≠ SAFETY`
`SAFETY ≠ UPSIDE`

### Layer 1 — Classification

- `A-CLASS`: fundamental-backed asymmetry; current, verified economic quality/value capture is material.
- `B-CLASS`: reflexive/narrative asymmetry; opportunity may be real despite low FQ, but requires smaller risk budget and stricter exit discipline.
- `B-DEVELOPMENT`: current thesis is still primarily EA/catalyst driven while A-Class characteristics remain conditional or pending. Future expected improvement is not enough for A-Class labeling.
- `HYBRID`: both currently verified fundamental and reflexive/entry characteristics are material.

Class migration must be evidence-driven. Example: conditional future value capture does not justify `A-DEVELOPMENT`; migration from B-DEVELOPMENT -> HYBRID/A-CLASS requires actual verified economic activation/adoption/value capture.

### Layer 2 — Two independent scores

`FQ = Fundamental Quality Score (0-100)` answers: how strong is the token's current economic quality?

FQ must evaluate, with asset-appropriate evidence: adoption/product demand; revenue/economic activity; token value capture; competitive durability; supply quality; fundamental sustainability. Protocol/company success without token-level value capture must not be treated as token quality. Store `FQ_AS_OF`. FQ is not permanent: material exploit, revenue collapse, tokenomics/value-capture change, regulatory shock or major competitive loss forces immediate reassessment. Otherwise refresh on a reasonable weekly-to-monthly cadence appropriate to data availability.

`EA = Entry Asymmetry Score (0-100)` answers: at today's price and timing, how attractive is the forward payoff asymmetry?

EA must evaluate: remaining repricing potential; valuation/mispricing; catalyst timing; positioning/crowding; supply-demand asymmetry; reflexivity potential; entry/invalidation geometry. Past drawdown or distance from ATH is not by itself underpricing.

FQ and EA MUST NOT be merged, averaged, multiplied or converted into a Total Opportunity Score. Maintain separate `FUNDAMENTAL QUALITY RANKING` and `ENTRY ASYMMETRY RANKING`.

Do not freeze example scores into the template. All candidate scores must be recalculated under the same current rubric and same stated `as_of` evidence. If evidence is insufficient, output `DATA INSUFFICIENT`; never fill scores by analogy or memory.

### Layer 3 — Evidence Maturity (EM)

EM is a gate/context field, not a ranking bonus. Use explicit maturity states; numeric mapping is optional and must not be used as a hidden score multiplier.

Default states:
- `SPECULATIVE`
- `PROPOSED`
- `APPROVED / CONDITIONAL`
- `ACTIVE / EARLY`
- `VERIFIED / ECONOMICALLY MATERIAL`

EM affects capital eligibility, required confirmation and position size. A proposed/conditional mechanism cannot be described as active economic value capture.

### Layer 4 — Fundamental Floor (FF)

FF = `HIGH / MEDIUM / LOW / NONE`. It answers: if the main catalyst fails, how much real token-level economic support remains?

FF is a risk-budget gate only; it never adds ranking points. Evaluate the token, not merely the protocol/company. Revenue without token value capture does not automatically create a high floor.

### Layer 5 — Risk Governor

Risk Governor may `ALLOW`, `REDUCE SIZE`, `WAIT`, or `VETO`. Research status and execution status are separate.

A `CONDITIONAL` state is never allowed to be vague. It must include:
- a falsifiable `condition`;
- `action_if_met`;
- `action_if_not_met`;
- `review_deadline`;
- `evidence_required`.

If any of those are missing or thresholds cannot be justified from evidence/history/market structure, `CONDITIONAL` maps to `WAIT`. Never invent X/Y/Z thresholds merely to make a condition executable.

Risk Governor checks permanent-loss risk, smart-contract/technical risk, regulation, liquidity/exit risk, concentration, unlock/emissions, token-demand failure, single-catalyst dependency, correlation, portfolio allocation and BTC/Cash opportunity cost.

### Layer 6 — Position sizing / execution discipline

A-Class may receive a larger Asymmetric risk budget only when FQ and EA are both sufficiently strong, evidence is mature, FF is acceptable, Risk Governor allows it and Allocation Gate passes.

B-Class/B-Development may remain on Radar with low FQ or low FF. Low FQ/FF does not automatically delete an early opportunity. However capital eligibility requires smaller sizing, higher liquidity standards, faster review and explicit exit discipline.

For every B-Class/B-Development asset, distinguish `RADAR ELIGIBILITY` from `CAPITAL ELIGIBILITY`. A candidate may remain WATCH/RESEARCH without complete execution parameters. Before `BUY SMALL` becomes eligible, all three must be predefined and evidence-based:
1. `INVALIDATION`: explicit thesis/price invalidation; not vague language such as momentum weakened.
2. `TIME STOP`: asset/event-specific N-day deadline for thesis/catalyst realization or mandatory exit/review.
3. `PROFIT LADDER`: predefined X% gain -> Y% trim steps plus runner rule, calibrated to volatility/liquidity/market cap/reflexivity/catalyst window rather than a universal template.

If any of these three cannot be responsibly quantified: `RADAR = ALLOWED`, `BUY SMALL = PROHIBITED`.

## EA freshness and event override

EA is perishable. Every EA must store:
- `EA_SCORE`
- `EA_AS_OF`
- `EA_EXPIRES_AT`
- `EA_STATUS = FRESH / STALE / INVALIDATED_BY_EVENT`

Default maximum TTL:
- A-Class: <=7 days.
- B-Class/B-Development/reflexive: <=3 days.

Use a shorter TTL when catalyst/event timing requires it. `STALE` or `INVALIDATED_BY_EVENT` EA may remain on Radar but cannot authorize `BUY SMALL`, `BUY` or `ADD`.

The following events force immediate EA invalidation/recalculation regardless of TTL: major unlock; governance result; tokenomics change; exploit; material listing/delisting; regulatory action; major protocol launch/failure; material revenue/TVL discontinuity; buyback activation/deactivation; major supply change; catalyst completion/failure.

FQ also carries `FQ_AS_OF`; material fundamental events force immediate reassessment even if normal review cadence has not elapsed.

## Opportunity signals — labels, not a third score

Use simple boolean/category signals to expose rare setups. Initial thresholds are `PROVISIONAL` until calibrated on real outcomes; never claim historical validation without evidence.

- `DUAL-STRONG` provisional: FQ >=70 AND EA >=75. High-quality + high-entry-asymmetry A/Hybrid setup; highest research priority, not automatic BUY.
- `EA-ONLY` provisional: FQ <55 AND EA >=80. Typical B-Class/reflexive setup; small-risk channel only.
- `QUALITY-TRAP` provisional: FQ >=80 AND EA <50. Excellent quality but poor entry; do not chase.
- `DEVELOPING` may be used sparingly for FQ 55-69 AND EA >=70 when current fundamentals are partial but important conditions remain unverified.

Avoid threshold cliffs: scores near cutoffs require judgment/confidence labels. Recalibrate thresholds only from recorded real signals/outcomes and with explicit user approval.

## Research tiers vs execution states

Research tiers describe research priority only and must never imply permission to buy. Use compact tiers such as `RADAR / WATCH / RESEARCH / PRIORITY RESEARCH` based on evidence and the two rankings.

Execution state must be one of:
- `RADAR ONLY`
- `RESEARCH`
- `WAIT`
- `CONDITIONAL — WAIT`
- `CAPITAL ELIGIBLE — SMALL`
- `CAPITAL ELIGIBLE`
- `VETO`

No `BUY SMALL / BUY / ADD` recommendation is allowed unless current data is fresh, explicit conditions are met, risk budget and funding source are stated, invalidation/exit plan is explicit, Risk Governor allows it and Allocation Gate passes.

## Candidate discovery and mandatory output

Every daily run must output a compact Top Watchlist whenever enough market data exists. Normally show the strongest 5-10 assets; fewer only when evidence is genuinely insufficient. Never invent filler. `NO QUALIFIED BUY` still requires the strongest research candidates.

For each asset output at minimum:
- Asset / Class
- FQ / FQ_AS_OF
- EA / EA_AS_OF / EA_EXPIRES_AT / EA_STATUS
- EM / FF
- Opportunity Signal
- Research Tier
- Risk Governor
- Execution State
- strongest evidence / catalyst
- thesis kill
- upgrade trigger / downgrade trigger
- supply/unlock issue
- data gaps/conflicts

For B-Class/B-Development assets that are Capital Eligible, additionally output `Invalidation / Time Stop / Profit Ladder / Max Position / Review Deadline`.

Always output both `FUNDAMENTAL QUALITY RANKING` and `ENTRY ASYMMETRY RANKING`, plus `DUAL-STRONG`, `EA-ONLY`, `QUALITY-TRAP` candidates when applicable. Output `NEAREST TO A-CLASS BUY` and `NEAREST TO B-CLASS BUY` separately. Do not collapse them into one `NEAREST TO BUY`.

Reflexivity analysis remains separate. Use verified price/volume/positioning/social/flow evidence; otherwise output `REFLEXIVITY: INCOMPLETE`. Reflexivity can support EA/B-Class analysis but cannot masquerade as FQ.

## Current-case discipline

Do not freeze HYPE/ENA/PENDLE/ZEC/ZRO/ONDO example scores into canonical logic. Re-score them using the same v5.0 rubric and current evidence on each relevant run.

ENA must not be labeled A-development merely because future value capture is possible. If value capture remains approved/conditional, FF low and thesis mainly depends on future USDe growth, classify `B-DEVELOPMENT` unless current verified evidence justifies migration. Fee-switch approval is not equivalent to economically material active buybacks.

For event-driven assets such as ZRO, an EA score must not survive across a material unlock event. If a 2026-09-20 unlock remains material to the thesis, set `EA_EXPIRES_AT` no later than that event. After the event, reacquire wallet flows, CEX inflows, actual selling, spot absorption and price reaction before recalculating EA. Repeated monthly unlocks should be judged by actual absorption, not mechanically treated as one-time permanent vetoes.

PENDLE legacy $1.50-$1.65 research zone remains a research reference only, not an automatic BUY trigger. Full v5.0 gates and fresh EA are required.

## Due diligence, monitoring and exits

Before any executable B-Class `BUY SMALL`, evaluate -30/-50/-70%, near-zero/permanent-loss, liquidity/exit failure, token-demand failure, worst supply/unlock/emission and quantitative EV/asymmetry. Estimated probabilities must be labeled estimates. Critical missing data => `DATA INSUFFICIENT / B DUE DILIGENCE PENDING`.

After confirmed buy: daily price/volume/material catalyst/unlock monitoring; weekly revenue/TVL/adoption/executed buybacks/supply/valuation; event-triggered tokenomics/governance/contract/regulatory/thesis-break monitoring. Record confirmed entry price/quantity/time/capital/thesis/invalidation/catalyst/track. Never invent execution values.

Sell/de-risk for predefined invalidation, time stop, thesis break, permanent-loss/value-capture/supply/liquidity deterioration or deteriorating forward asymmetry. Profit-taking follows the precommitted asset-specific ladder for B-Class positions; do not improvise after entry.

Learning-loop framework changes only after at least one CLOSED real trade with confirmed execution history and explicit user approval. Threshold/TTL calibration may be proposed from recorded signals but is not silently rewritten.

## Daily mandatory output

`TODAY'S VERDICT / TOP WATCHLIST / FQ RANKING / EA RANKING / OPPORTUNITY SIGNALS / NEW TO RADAR / UPGRADES-DOWNGRADES / NEAREST TO A-CLASS BUY / NEAREST TO B-CLASS BUY / MATERIAL CHANGES / EM-FF / EA FRESHNESS / Risk Governor / EXECUTION STATE / confirmed positions-actions / risks-invalidations-data gaps / Allocation Gate if capital is proposed`.

If no executable asset qualifies, output `NO QUALIFIED BUY` while preserving the watchlist. Only genuine market-data failure may produce no scored candidates, labeled `DATA INSUFFICIENT`, never `NO CANDIDATES`.