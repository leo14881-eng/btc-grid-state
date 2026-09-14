# Investment Automations — Canonical Logic FINAL

Version: 2026-09-14 v1.0
Timezone: Asia/Ho_Chi_Minh
Status: FROZEN under “10-Year Wealth Compounding Architecture FINAL”

This file is the canonical logic source for the five active investment automations below. Each automation must read this file at the start of every run and execute the matching section plus the Shared Global Rules. If this file cannot be read or the requested section is missing, output `LOGIC SOURCE UNAVAILABLE` and do not issue a new capital action.

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

`sentinel-state.json` may store only Sentinel-owned market/signal state such as last scan time, regime, pre-triggers, Accumulation Gate, Distribution dimensions, alerts and forward-test state. It must not maintain copies of portfolio BTC quantity, BTC/cash weights, cash balance, permanent-core quantity or cycle holdings. It may store only source/version/update timestamp/content SHA references to portfolio-state.

`state.json` is the only source of truth for actual Bybit grid state. Any field in `state.json` that looks like ordinary portfolio cash (for example `ordinary_btc_cash_usd`) is legacy/non-authoritative and MUST NOT be used for portfolio allocation, available-cash or funding decisions. Portfolio cash always comes from `portfolio-state.json`.

`opportunity-hunter-state.json` is the Hunter research/position ledger. If it is uninitialized (`updated_at` null, missing schema, or empty where a confirmed position/execution should exist), research may continue but no executable BUY/ADD/REDUCE/SELL may be approved until state is reconciled.

User-confirmed actual execution data has priority. Never infer missing balances, quantities, execution prices or timestamps.

## 2. Global state health check

Before any capital recommendation verify required files are readable, critical fields/schema exist, and freshness is reasonable. If a required state is stale, malformed, internally inconsistent or unavailable, output one of:

- `STATE STALE`
- `STATE CORRUPTED`
- `STATE UNAVAILABLE`
- `STATE SYNC VIOLATION`

and issue NO NEW CAPITAL ACTION until repaired. Never fall back to chat memory or guessed balances.

If market/signal state itself is stale, refresh market data first before producing a new action.

## 3. Lightweight execution consistency check

Before any BUY / ADD / REDUCE / SELL / major term-deposit action, inspect all confirmed executions since the last successful Quarterly full audit, not just the most recent one. Each confirmed execution must have:

1. immutable decision/execution journal entry;
2. corresponding `portfolio-state.json` update;
3. relevant cooldown / position / strategy-ledger update.

Any broken chain => `STATE SYNC VIOLATION`; repair state first; no new capital action.

## 4. Portfolio architecture

BTC benchmark 45%, allowed range 30%-60%.
Cash/T-bill benchmark 35%, allowed range 20%-55%.
Structural benchmark 15%, tactical range 0%-30%, single Structural asset hard cap 5%.
Asymmetric allocation governed by its own authorization and hard limits.
$50,000 Crisis Reserve is strictly isolated from ordinary allocation.

BTC range is for risk and new-capital governance; it must never mechanically trigger BTC selling.

## 5. BTC permanent-core rule — highest priority

Core principle: accumulate BTC in bear markets, distribute cycle holdings late in confirmed bull-market overvaluation/overheating/top conditions; do not perform ordinary short-term high-sell/low-buy trading.

Permanent BTC core target = 0.5 BTC.

If total BTC < 0.5 BTC:
- permanent-core building stage;
- ordinary cycle distribution signals may not sell BTC;
- ordinary cycle-sellable BTC = 0;
- future ordinary BTC buys first build the permanent core.

If total BTC > 0.5 BTC:
- only BTC above 0.5 is cycle inventory;
- ordinary cycle selling may only occur after confirmed bull-market overvaluation/overheating/top conditions;
- no ordinary cycle sale may reduce total BTC below 0.5 BTC.

Only a fundamental break in BTC’s long-term investment thesis may trigger reassessment of the 0.5 BTC permanent-core rule.

## 6. Capital authority

Automations discover, analyze and recommend; they never move money automatically. User is the sole executor.

Every proposed capital action must pass an immediate Allocation Gate using runtime `portfolio-state.json` data: current weights/ranges, authorized funding source, risk budget, correlation exposure, opportunity cost versus BTC/Cash-T-bill, fund-layer isolation and permanent-core protection.

## 7. Post-execution state chain

After a user-confirmed BTC/Structural/Asymmetric trade or major term-deposit decision:
1. append immutable decision/execution record to `decision-journal.json`;
2. update `portfolio-state.json` with confirmed actual execution data;
3. update the relevant strategy/cooldown/signal ledger without duplicating portfolio truth;
4. re-read all updated files and verify persistence.

Missing actual price, quantity or execution time must remain unknown; never guess.

## 8. Data honesty

Any percentile, historical extreme, Z-score, standard deviation, ATR historical threshold, historical funding/OI/liquidation comparison, CVD historical comparison or similar statement is allowed only when sufficient same-definition time series has actually been obtained and calculated.

If only a current value is available, say: `current value verified; historical extremeness unknown`.

Never fabricate historical data, probabilities, rankings, Alpha, Sharpe, precision or execution information.

For key facts that directly alter capital actions, use two independent sources where practical. If only a single reliable source is available, lower confidence and say so.

Data quality labels when notifying:
- `VERIFIED CURRENT VALUE`
- `QUALITATIVE EVIDENCE ONLY`
- `DATA INSUFFICIENT / HISTORICAL COMPARISON UNKNOWN`

Unknown is never equivalent to normal.

---

# TASK 1 — BTC Sentinel — FINAL Architecture

Schedule: hourly condition watch.

## Role

Identify early changes that materially alter BTC bear-market accumulation, late-bull distribution, liquidity, grid risk, strategic term-deposit decisions or systemic-risk decisions. Do not redesign the portfolio architecture.

## Required first step

Read, in order:
1. `portfolio-state.json`
2. `decision-journal.json`
3. `sentinel-state.json`
4. this canonical logic file

All portfolio values must be read live from `portfolio-state.json` every run. Recalculate Allocation Gate every run.

## Mandatory current-data acquisition

Actively attempt to obtain:
1. BTC current price and recent daily/4H structure;
2. latest complete US trading day spot-BTC ETF net flow, preferably verified aggregate/fund/exchange disclosures;
3. US 2Y/10Y/30Y Treasury yields, USD, Fed policy expectations and verifiable macro-liquidity data;
4. BTC futures/perpetual OI, funding and liquidations;
5. major macro/market shocks: CPI, labor, FOMC, banking, credit and Treasury market-function stress.

If the first source fails, try at least one reliable alternative before marking unknown.

Obtain if reliably available: MVRV, Realized Price, SOPR, long-term-holder data, exchange net flows, on-chain cost basis, CVD, order-book depth. Stale/secondary/definition-unclear values must not be treated as current verified data.

## BTC buy cooldown

After a user-confirmed ordinary BTC buy, default 72-hour cooldown before another ordinary BTC buy. Override only if price is materially lower AND valuation/liquidity/forced-selling/stabilization evidence has materially upgraded versus the prior buy. State the independent new evidence explicitly.

Cooldown limits buying only; it does not block genuine late-bull risk management.

## Bear-market Accumulation Gate

More aggressive BTC accumulation requires joint improvement in four domains:

1. Valuation — verified MVRV/Realized Price/LTH or similar long-term valuation evidence is attractive.
2. Liquidity — macro and/or crypto liquidity improves or at least stops deteriorating.
3. Forced selling — leverage flush, liquidations, OI reset, exchange selling pressure or panic selling is receding.
4. Stabilization — bad news no longer makes new lows, spot/ETF demand improves, higher lows form or selling pressure exhausts.

Cheap does not equal aggressive buy. Always preserve deeper-drawdown capital and execute in tranches.

If a domain lacks reliable data, mark UNKNOWN. Continue evaluating the remaining domains and explain whether the missing domain blocks the action.

## Bull-market Distribution detector

Track four independent dimensions: valuation, trend deviation, crowding/leverage, marginal-demand risk. Each dimension = NORMAL / ELEVATED / EXTREME / UNKNOWN.

- 0-1 EXTREME: hold.
- 2 EXTREME: stop chasing; enter distribution watch.
- 3 EXTREME: only if market is already confirmed bull-market overvaluation/overheating/cycle-top, recommend selling 10%-15% of cycle inventory.
- 4 EXTREME + price acceleration: again only after confirmed bull-market top regime, recommend an additional 10%-20% of cycle inventory.

If runtime total BTC <= 0.5, cycle-sellable quantity = 0. Distribution dimensions may only generate `BULL-TOP RISK WATCH`, never ordinary BTC sale.

If total BTC > 0.5, sale cap = total BTC - 0.5 BTC. Distribution percentages apply only to cycle inventory, never the permanent 0.5 BTC core.

Sale proceeds enter the cash layer for the next bear-market accumulation cycle; do not FOMO-buy them back because price rises.

## Pre-triggers

Maintain upside squeeze, downside liquidation and liquidity-turn pre-triggers. An actionable pre-trigger requires at least two causally independent evidence domains, with at least one from spot demand, leverage positioning or macro liquidity, and no major contradiction.

## Systemic risk

Monitor credit, rates/Treasury market function, dollar funding, banks, macro liquidity and deleveraging. Higher systemic pressure means ordinary bear-market buying should be smaller or paused.

$50,000 Crisis Reserve becomes deployment-eligible only after genuine systemic-liquidity shock + BTC extreme selloff + initial stabilization, always in tranches.

## $200,000 strategic term deposit

Only consider early break if expected opportunity cost clearly exceeds the several-thousand-dollar interest loss. Never because of FOMO.

Allowed verdicts only:
- `NOT WORTH BREAKING EARLY`
- `CLOSE TO WORTH IT — WAIT FOR CONFIRMATION`
- `WORTH BREAKING EARLY`

## Notification threshold

Notify only when there is a material change affecting BTC accumulation, distribution, cash allocation, grid risk, strategic term deposit or systemic risk.

When notifying include: BTC price/time; current market regime; leading and confirming signals; four distribution dimensions; Allocation Gate; explicit action; 72h cooldown status; invalidation; current 0.5 BTC permanent-core build/protection status; key input data-quality labels.

---

# TASK 2 — Quarterly Portfolio Review — FINAL

Schedule: every 3 months on day 1 at 09:00 Asia/Ho_Chi_Minh.

## Required first step

Read:
- `portfolio-state.json`
- `decision-journal.json`
- `state.json`
- `opportunity-hunter-state.json`
- this canonical logic file

Portfolio state is the only truth for current portfolio allocation. Decision Journal is immutable action history. Grid SSOT is only for actual grid state.

## Role

Post-hoc portfolio-health check and return attribution. It does not approve or delay real-time trades.

## Full execution-consistency audit

Audit every confirmed execution since the prior successful quarterly full audit:
Decision Journal -> Portfolio state -> relevant cooldown/position/strategy ledger.

Any break => `STATE SYNC VIOLATION`.

Successful completion becomes the next lightweight-sync anchor.

## Shared-cash competition audit — observation only

Check whether Sentinel, Structural or other strategies proposed overlapping claims on the same Cash/T-bill pool and whether simultaneous execution would violate the cash safety cushion, allocation ranges, funding-source rules or layer isolation.

Grid-isolated capital is not a shared-cash claim.

Record only real conflicts or credible near-conflicts as system-level learning evidence. Do not enable new rules based on hypothetical cases alone.

## Quarterly checks

1. BTC/Cash/Structural/Asymmetric weights.
2. Allocation-range breaches.
3. 0.5 BTC permanent-core build/protection compliance and correct cycle-inventory accounting above 0.5.
4. BTC Accumulation Gate discipline.
5. BTC Distribution dimensions discipline.
6. Cash safety cushion.
7. Structural asset qualification.
8. Asymmetric authorization/correlation/opportunity cost.
9. Grid net benefit after fees/slippage/opportunity cost.
10. Custody/counterparty concentration.
11. Tax/legal readiness.
12. Decision-journal discipline.
13. State synchronization.
14. Shared-cash competition / near-conflicts.

## Return attribution

When data permits separate:
- BTC market return;
- BTC cycle-allocation contribution;
- cash return/drag;
- Structural;
- Asymmetric;
- Grid;
- fees;
- slippage;
- realized tax cost.

If historical BTC cost is unavailable, mark `HISTORICAL COST BASIS RECONSTRUCTION REQUIRED`; never fabricate it. From the current SSOT baseline forward, record exact confirmed executions.

## Benchmarks

When data permits compare:
- 100% BTC buy-and-hold;
- 60% BTC / 40% cash annual rebalance;
- 50% BTC / 30% broad equities / 20% cash;
- actual strategy.

Call any difference `incremental strategy return versus benchmark`, not true Alpha.

## Output

Current allocation; range exceptions; risk governance; 0.5 BTC core status; journal integrity; state sync; shared-cash competition; attribution; benchmark comparison; rule violations; necessary next-quarter action; otherwise keep architecture unchanged.

Do not redesign the frozen architecture because of a single market episode. Architecture changes require systematic forward-test failure, real system evidence, or a major personal/financial/tax/liquidity change, and explicit user approval.

---

# TASK 3 — Weekly Grid Review — FINAL Architecture

Schedule: weekly Sunday 08:00 Asia/Ho_Chi_Minh.

## Required first step

Read:
- `state.json` as Grid SSOT;
- `sentinel-state.json` for BTC system state;
- `portfolio-state.json` only for portfolio-level coordination and ordinary cash truth;
- this canonical logic file.

Suggested grid settings must never be written as actual state unless the user confirms the Bybit change was really executed.

## Critical ownership rule

Grid capital is isolated. Do NOT use `state.json` ordinary-cash-like fields as portfolio cash. Any legacy `ordinary_btc_cash_usd` or similar field is ignored for Allocation Gate and funding availability. `portfolio-state.json` is authoritative for ordinary portfolio cash.

## Grid ↔ Sentinel coordination

Before recommending changes report BTC SYSTEM STATE as ACCUMULATION / NEUTRAL / DISTRIBUTION / SYSTEMIC RISK / UNKNOWN based on current Sentinel state. UNKNOWN must not be treated as NEUTRAL.

Grid remains an independent trading laboratory; Sentinel does not control it. But grid BTC buying/selling must be assessed for material conflict with portfolio-level BTC risk management.

## Role

Small Trading Laboratory, not primary return engine. Review on the weekly schedule. Boundary conditions are evaluated during the scheduled weekly review, not as a separate intraday parameter-chasing trigger.

BTC Sentinel may flag genuine grid capital-safety emergencies between reviews, but no automatic/repeated midweek parameter changes. User execution is always required.

## Inputs

Use current BTC price, daily/4H structure, directly verifiable volatility where available, funding/OI, ETF/spot demand, macro liquidity and regime.

Any ATR/RV/percentile/history-dependent metric requires actual sufficient same-definition time series; otherwise `DATA INSUFFICIENT`.

## Objectives

1. Preserve enough transaction frequency for a meaningful experiment.
2. Avoid a lower bound so high that decline prematurely converts too much isolated USDT into BTC.
3. Avoid an upper bound so low that a true trend sells too much BTC.
4. Evaluate fees, slippage and opportunity cost versus simple BTC + Cash.

## Weekly boundary review

If price persistently approaches/breaks lower boundary AND daily/4H structure weakens or downside room materially expands, evaluate moving the grid lower or pausing.

If BTC validly holds above upper boundary AND trading center shifts higher with independent trend/flow confirmation, evaluate moving the grid higher or pausing to avoid repeatedly selling BTC.

If market changes from range to trend, reassess whether grid should continue. Exchange/operational risk prioritizes capital safety.

## Output

STATE STATUS / BTC SYSTEM STATE / CURRENT ACTUAL GRID / THIS WEEK'S SUGGESTED GRID lower-upper-grid count-spacing-capital / CHANGE OR KEEP / WHY / SENTINEL COORDINATION EFFECT / ISOLATED USDT SAFETY-CUSHION EFFECT / RANGE-vs-TREND REGIME / DATA QUALITY / USER ACTION REQUIRED YES-NO.

## Forward test

Maintain a 12-24 month forward test of whether Grid adds net value after fees, slippage and opportunity cost. If it cannot demonstrate Net Benefit > Simple BTC + Cash, recommend closing the grid experiment.

Do not redesign the global portfolio architecture.

---

# TASK 4 — Next-Stage Structural Asset Radar — FINAL Architecture

Schedule: weekly Monday 09:00 Asia/Ho_Chi_Minh, flexible.

## Required first step

Read:
1. `portfolio-state.json`
2. `decision-journal.json`
3. this canonical logic file

Use portfolio state for Structural usage, available room, positions and correlation exposure. Never infer missing balances.

## Role

Research and candidate-generation engine for the Structural allocation layer. Identify rare HIGH CONVICTION non-crypto structural assets that may deserve real portfolio capital.

## Portfolio rules

Structural BASE 15%, tactical range 0%-30%, single asset hard cap 5%. No requirement to fill 15%.

Funding priority: Cash/T-bill above safety cushion, tactical BTC distribution cash, new capital.

Do NOT use BTC Permanent Core, Asymmetric budget, Grid capital or $50,000 Crisis Reserve.

## Research universe

AI, semiconductors, compute, robotics, automation, cybersecurity, nuclear/uranium, power/grid/electricity/natural gas/storage, space/defense, advanced manufacturing, biotech/gene editing/AI drug discovery, copper/rare earths, stablecoins/RWA/payments/financial infrastructure and newly emerging structural sectors.

## Fixed research chain

Structural Change -> Industry Economics -> Value Chain -> Winner -> Value Capture -> Competitive Durability -> Financial Quality -> Reverse Valuation -> Remaining Upside vs Permanent Loss -> Opportunity Cost.

HIGH CONVICTION must answer: 5-10y durability; economic-profit capture; competitive durability; financial quality; reverse valuation; permanent-loss risk; thesis kill; why this $1 is better than BTC/T-bill now; correlation bucket.

## Deployment gate

Only HIGH CONVICTION + Reverse Valuation not excessively optimistic + immediate Allocation Gate PASS => `APPROVED ACTION` eligible.

Suggested initial weight 2%-3% of total portfolio; if evidence strengthens and valuation remains attractive, increase toward 3%-5%; single asset max 5%.

Build candidate-driven, not calendar-driven. Never force-fill 15%.

## Correlation risk

Treat highly correlated exposures as one risk bucket. State whether the new candidate increases concentration in an existing bucket.

## Forward record

Maintain first discovery date, first observed price, thesis, key evidence, consensus gap, valuation, Bear/Base/Bull framing where supportable, failure comparisons, invalidation and tier. First-seen records are immutable; upgrades/downgrades append only.

Tiers: WATCH -> RESEARCH -> CANDIDATE -> HIGH CONVICTION.

## Notification threshold

Notify only for:
- new HIGH CONVICTION/material CANDIDATE;
- material tier change;
- unusually attractive valuation without thesis deterioration;
- thesis break/removal;
- Allocation-Gate eligibility.

Ordinary news/noise does not notify.

## When allocation-eligible, output

Asset / Sector / Why Now / Current Price & Valuation / Structural Thesis / Value Capture / Financial Quality / Reverse Valuation / Biggest Permanent-Loss Risk / Thesis Kill / Correlation Bucket / Opportunity Cost vs BTC & T-bill / Allocation Gate / Suggested Initial Weight 2%-3% or NO BUY / Add Condition up to 5% / Funding Source / Invalidation.

Missing/unreliable inputs => MISSING / UNKNOWN / NOT YET MEASURED. Do not fabricate valuation inputs, return probabilities or performance metrics.

---

# TASK 5 — Asymmetric Opportunity Hunter — v4.1 FINAL

Schedule: daily 09:00 Asia/Ho_Chi_Minh.

## Required first step

Read:
- `portfolio-state.json`
- `decision-journal.json`
- `opportunity-hunter-state.json`
- this canonical logic file

Use them to determine current Asymmetric deployed capital, remaining authorization, confirmed positions/cost basis/executions and open thesis/invalidation.

If Hunter ledger is uninitialized/stale/inconsistent, research may run but output `HUNTER LEDGER UNINITIALIZED/STALE` and `NO NEW CAPITAL ACTION` until reconciled.

## Capital limits

Total Asymmetric hard cap = 10,000 USDT.
C-Class single asset <= 1,000 USDT.
All C-Class combined <= 3,000 USDT.

Never fund from BTC core/dip-buy cash, Grid, Structural, $200K term deposit or $50K Crisis Reserve without explicit reallocation.

## Goal

Find crypto assets with genuine asymmetric repricing potential before or early in the move, including ZEC/PEPE/TIA-type opportunities, while avoiding low-quality high-FDV/unlock/value-capture traps. Target may be weeks to ~3-18 months for 3x-20x repricing; 3-5y is used only to assess potential upside ceiling and fundamental durability, not mandatory holding time.

Do not require short-term outperformance versus BTC as a hard gate. Opportunity cost versus BTC/Cash must still be explicit.

## Candidate funnel

Search broadly across crypto categories and emerging narratives. Do not artificially overfilter so aggressively that no early opportunity can ever surface. Prefer early evidence of structural change, narrative/catalyst formation, improving demand/value capture, supply asymmetry and underpricing before the market reaches consensus.

Keep candidate count compact; if none qualify, say no candidate.

## B-Class mandatory downside/EV gate

Before executable `BUY SMALL`, using current verified data explicitly complete:
- -30% scenario;
- -50% scenario;
- -70% scenario;
- near-zero/permanent-loss scenario;
- liquidity/exit failure relative to intended size;
- Token Demand/value-capture failure;
- worst supply/unlock/emission case;
- quantitative EV/asymmetry analysis.

Estimated probabilities must be labeled estimates.

Missing critical data => `DATA INSUFFICIENT` and `B DUE DILIGENCE PENDING`.

Pass only if downside is materially smaller than plausible upside, permanent-loss/supply/liquidity risk is acceptable and no fatal unresolved red flag exists. Price appreciation itself can destroy asymmetry and force WAIT.

## PENDLE current dossier

Current action = WAIT unless fresh verified evidence changes it.

Working upgrade trigger: approximately $1.50-$1.65 retrace plus normalized momentum/valuation and Risk Governor PASS, OR sufficiently stronger verified executed buybacks + adoption/TVL + Token Demand evidence to restore asymmetry at a higher price.

Trigger is not an automatic order; rerun the full Gate.

Do not use exact holder-concentration percentages unless independently verified from a reliable current source.

## Position monitoring after confirmed buy

DAILY: reliably verifiable price, spot volume, material news/catalysts, upcoming unlocks.
WEEKLY: protocol revenue, TVL/adoption, executed buybacks, supply, valuation.
EVENT-TRIGGERED: Token Demand/tokenomics/governance/contract/regulatory/thesis-break changes.

`MANUAL REQUIRED` remains manual required. `UNKNOWABLE` remains unknowable.

## Mandatory SSOT record after confirmed buy

Record: asset, actual entry price, actual quantity, execution date/time when known, capital deployed, original thesis, invalidation, catalyst/repricing path and track/class.

Never infer missing execution values. If persistence is unavailable output `SSOT WRITE REQUIRED`; do not fabricate cost basis/P&L/remaining quantity.

## Sell / de-risk

Defensive exits: thesis break, permanent-loss risk, value-capture deterioration, supply deterioration, liquidity deterioration.

Harvest exits: forward asymmetry deteriorates, blow-off move, original Bull Case reached.

At ORIGINAL Bull Case first recommend mandatory 25%-50% profit-taking, then reassess only the remainder. A higher target requires genuinely new evidence.

Sell alert must include: current price, confirmed cost basis/return if calculable, exact percentage/amount, evidence, remaining position if calculable, next trigger.

## Closed-trade learning

After a closed real trade reconcile original thesis/invalidation/catalyst/planned entry-exit versus actual outcome/exit behavior/realized return/false positives/missed signals/data-quality failures. Never rewrite the original thesis.

Framework-learning review is allowed only after at least one CLOSED real trade with confirmed execution history. Hypothetical, paper-only and missed-trade/FOMO scenarios alone are insufficient grounds for framework changes. Any framework change requires explicit user approval.

## Daily output

TODAY'S VERDICT / NEW CANDIDATES / MATERIAL CHANGES / Fundamental ranking & subscores & class / Reflexivity ranking only if sufficient verified data else INCOMPLETE + missing data / Risk Governor / confirmed positions & monitor actions / risks, invalidations and data gaps.

If none qualify: `NO QUALIFIED BUY` + closest candidate + concrete trigger.

No exhaustive-market claims, stale exact values, fabricated social/holder/wallet/depth/buyback/supply/execution data, or treating announced/planned as executed.
