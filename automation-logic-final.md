# Investment Automations — Canonical Logic FINAL

Version: 2026-09-15 v1.2
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

More aggressive BTC accumulation requires joint improvement across:
1. Valuation — reliable long-term valuation evidence becomes attractive.
2. Liquidity — macro and/or crypto liquidity improves or at least stops deteriorating.
3. Forced selling — leverage washout/liquidations/OI reset/exchange sell pressure/panic selling recedes.
4. Stabilization — bad news stops producing new lows, spot/ETF demand improves, higher lows form or seller exhaustion appears.

Cheap does not equal buy. Preserve deeper-drawdown capital and execute in tranches.

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

When safe deterministic state-governance defects are discovered (for example duplicate portfolio fields in a strategy-owned state file), recommend/perform repair only when the data owner is unambiguous and no investment fact must be guessed; reread and verify. Never rewrite investment history.

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

# TASK 5 — Asymmetric Opportunity Hunter — v4.2 FINAL

Schedule: daily 09:00 Asia/Ho_Chi_Minh.

Read `portfolio-state.json`, `decision-journal.json`, `opportunity-hunter-state.json`, then this file.

A ledger with `status=ACTIVE_INITIALIZED`, a valid timestamp and `assets={}` is healthy when Portfolio SSOT and Journal confirm zero Asymmetric positions. Do not block a first trade merely because assets is empty. If a position/execution exists elsewhere but is absent from Hunter ledger, output `STATE SYNC VIOLATION` and block new actions until repaired.

Capital: total Asymmetric hard cap 10,000 USDT; C-Class single asset <=1,000; all C-Class <=3,000. When portfolio-level percentage caps are stricter than these nominal Hunter caps, the stricter applicable cap governs until the architecture is formally reconciled. Never use BTC core/dip-buy cash, Grid, Structural, $200K term deposit or $50K Crisis Reserve without explicit reallocation.

Goal: identify genuine early asymmetric repricing opportunities (including ZEC/PEPE/TIA-type patterns) before/early in a move, while avoiding high-FDV/unlock/value-capture traps. Typical target window weeks to ~3-18 months for 3x-20x; 3-5y only assesses upside ceiling/durability. Short-term BTC outperformance is not a hard gate, but BTC/Cash opportunity cost must be explicit.

Search broadly without overfiltering. Prefer early structural change, narrative/catalyst formation, improving demand/value capture, favorable supply asymmetry and underpricing before consensus. Keep the displayed candidate set compact, but `NO QUALIFIED BUY` must never suppress the research watchlist. A run with zero executable BUYs must still surface the strongest early-stage opportunities found.

## Mandatory scored candidate framework

Every daily run must output a ranked Top Watchlist whenever enough market data exists to evaluate candidates. Normally show the best 5-10 assets; fewer is allowed only when evidence is genuinely insufficient. Do not invent filler candidates merely to reach a count.

Each displayed asset receives a transparent 0-100 Fundamental Opportunity Score composed of eight 0-10 subscores:

1. Repricing / Upside Potential — weight 20%: plausible remaining upside over the target window and 3-5y ceiling, not past performance.
2. Catalyst / Narrative Formation — weight 15%: identifiable upcoming or emerging catalysts, preferably before consensus saturation.
3. Adoption / Demand — weight 15%: verified usage, users, volume, TVL, revenue or other economically relevant demand appropriate to the asset.
4. Token Value Capture — weight 15%: credible mechanism connecting ecosystem success to token demand/value; weak or absent capture must score poorly.
5. Valuation / Underpricing — weight 15%: market cap/FDV/revenue or protocol-appropriate valuation and how much optimism is already priced.
6. Supply / Unlock Quality — weight 10%: circulating/FDV structure, unlocks, emissions, insider concentration and sell-pressure risk. Better supply quality = higher score.
7. Liquidity / Market Structure — weight 5%: tradability, depth, venue quality and whether the setup is already excessively crowded/reflexive.
8. Risk / Thesis Durability — weight 5%: smart-contract, regulatory, competitive, governance, permanent-loss and thesis fragility. More durable/lower permanent-loss risk = higher score.

Convert each 0-10 subscore to the weighted total: `Total Score = Σ(subscore / 10 × weight)`, reported as 0-100. Missing critical evidence must be marked `DATA INSUFFICIENT`; do not silently award a neutral score. If non-critical evidence is missing, score conservatively and identify the missing evidence. Scores are decision aids, not automatic BUY triggers.

Tier guidance based on the Total Score, subject to Risk Governor and critical-data overrides:
- <60 = MONITOR / normally omit from compact Top Watchlist unless strategically important.
- 60-69 = WATCH.
- 70-79 = RESEARCH.
- 80-87 = CANDIDATE.
- >=88 = HIGH CONVICTION RESEARCH, but still NOT automatically executable.

An executable B-Class `BUY SMALL` requires more than score: complete downside/EV due diligence, Risk Governor PASS, acceptable supply/unlock/liquidity, explicit BTC/Cash opportunity cost, authorized funding source and Allocation Gate PASS. A HIGH score with a critical red flag may remain WAIT or be vetoed.

## Early-discovery and change tracking

For each Top Watchlist asset output: Rank / Asset / Total Score / Tier / Stage / Action, followed by the eight subscores, strongest evidence, key catalyst, supply/unlock issue, thesis kill, concrete upgrade trigger and concrete downgrade trigger.

Always identify `NEW TO RADAR`, `UPGRADED`, `DOWNGRADED`, and `NEAREST TO BUY` when applicable. Preserve the distinction between research status and execution status: WATCH/RESEARCH/CANDIDATE can be valuable outputs even when today's verdict is `NO QUALIFIED BUY`.

Reflexivity ranking is separate from the Fundamental Opportunity Score. Output it only when price/volume/positioning/social/flow evidence is sufficiently verified; otherwise write `REFLEXIVITY: INCOMPLETE`. Do not boost the Fundamental score merely because price has already pumped.

Before B-Class executable `BUY SMALL`, evaluate -30/-50/-70%, near-zero/permanent-loss, liquidity/exit failure, Token Demand failure, worst supply/unlock/emission and quantitative EV/asymmetry. Estimated probabilities must be labeled estimates. Critical missing data => `DATA INSUFFICIENT / B DUE DILIGENCE PENDING`.

PENDLE current default = WAIT. Working research trigger around $1.50-$1.65 retrace plus normalized momentum/valuation and Risk Governor PASS, OR materially stronger verified executed buybacks + adoption/TVL + Token Demand evidence. Price trigger is not an automatic order; rerun full Gate.

After confirmed buy: daily price/volume/material catalyst/unlock monitoring; weekly revenue/TVL/adoption/executed buybacks/supply/valuation; event-triggered tokenomics/governance/contract/regulatory/thesis-break monitoring. Record confirmed entry price/quantity/time/capital/thesis/invalidation/catalyst/track. Never invent execution values.

Sell/de-risk for thesis break, permanent-loss/value-capture/supply/liquidity deterioration or deteriorating forward asymmetry. At original Bull Case first recommend mandatory 25%-50% profit-taking, then reassess remainder; higher target needs genuinely new evidence.

Learning-loop framework changes only after at least one CLOSED real trade with confirmed execution history and explicit user approval.

Daily output is mandatory even when there is no BUY: TODAY'S VERDICT / TOP WATCHLIST WITH SCORES / NEW TO RADAR / UPGRADES-DOWNGRADES / NEAREST TO BUY / MATERIAL CHANGES / Fundamental ranking + eight subscores + tier / Reflexivity ranking if sufficiently verified else INCOMPLETE / Risk Governor / confirmed positions-actions / risks-invalidations-data gaps. If no executable asset qualifies, output `NO QUALIFIED BUY` but still show the scored watchlist and the closest candidate with concrete trigger. Only a genuine market-data failure may produce no scored candidates, and that must be labeled `DATA INSUFFICIENT` rather than `NO CANDIDATES`.
