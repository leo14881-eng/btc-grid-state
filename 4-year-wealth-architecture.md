# 4-Year Wealth Architecture — FINAL v1.2

Status: ACTIVE / FROZEN
Effective date: 2026-11-08
Horizon: 4 years
Role: Highest-level capital architecture above BTC Sentinel, Grid, Hunter and Structural modules.
Execution authority: USER ONLY.
Automation budget: ZERO new scheduled tasks.


## 0. CURRENT CAPITAL STRUCTURE — USER-APPROVED 2026-10-11

This versioned amendment supersedes conflicting historical L1/L2/L3, A/B/C/D/E, 10K daily swing, 20K larger swing, fixed 120K core + 40K swing, historical 45/35/15 allocation, and old grid range references. Historical figures below are not executable allocation instructions.

- Main investment budget: **200,000 USDT = 160,000 USDT BTC unified pool (core BTC + swing BTC + standby USDT; dynamic proportions) + 20,000 USDT Hunter altcoin pool + 20,000 USDT extreme-crash reserve**. Budgets are not verified current balances.
- BTC Grid: **14,000 USDT separately**, outside the main 200K, hard cap 20,000 USDT. Only state.json is the current configuration source; 72K–86K below is a historical reference, not current settings.
- Crisis/living reserve: **50,000 USD strictly isolated**, excluded from main-investment NAV and investable capital.
- Core BTC: governed by valuation and historical drawdowns, with trend controlling pacing; do not casually sell core on ordinary short-term bearish predictions. Historical 0.5 BTC floor is not an automatic veto on independently justified swing actions.
- Swing BTC: primary KPI is **realized net USDT P&L after fees and reasonable slippage**. BTC quantity, BTC Buy&Hold comparison and missed upside are diagnostics only, never a minimum-profit veto.
- Extreme-crash reserve: only flash crashes, systemic liquidity shocks or severe mispricing, not ordinary 5–10% volatility.
- Hunter: active acquisition cost plus actual pending-order reservations <= 20,000 USDT. Existing approximately 6,000U AXS belongs to Hunter; until it exits net profitable and Hunter demonstrates sufficient verifiable profit evidence, no new live altcoin buy/add and no mechanical averaging down. Missing position reconciliation blocks claims of available capital.
- Management objective: from a 200,000 USDT starting budget toward approximately 600,000 USDT by 2028-03, not guaranteed. Report current verified main-investment NAV, realized net profit, module contribution, gap/progress and UNKNOWN for missing evidence.
- Only the user executes real trades. No automated orders or unilateral cap changes. Capital proposals require allocation, drawdown, counterparty and freshness gates.
- 33K BTC bottom is a scenario hypothesis, never a factual anchor.

## 1. Objective hierarchy

1. Avoid permanent capital impairment and ruin.
2. Preserve liquidity and dry powder so the portfolio is never forced to sell core BTC for ordinary cash needs.
3. Maximize four-year net worth on a risk-controlled basis.
4. BTC is the primary wealth engine. Strategic BTC quantity target is >= 4 BTC, but this is NOT a calendar deadline and must never force buying at unattractive conditions.
5. Active strategies are return enhancers, not substitutes for the core architecture.

## 2. Capital structure and ownership

`portfolio-state.json` is the balance/allocation SSOT. Module state files own only their module runtime state. Budgets are caps/mandates, not proof of current cash or NAV.

- **Main investment budget: 200,000 USDT** = **160,000 USDT BTC unified pool** (only three components: **核心BTC / 波段BTC / 待命USDT**, dynamic proportions) + **20,000 USDT Hunter altcoin pool** + **20,000 USDT extreme-crash reserve**.
- **BTC Grid: 14,000 USDT**, independent of the 200,000 USDT main investment budget. Grid's current configuration is read only from `state.json`; exchange-live balances/status must be separately verified. The 20,000 USDT Grid hard cap remains a ceiling, not an allocation target.
- **Crisis/living reserve: 50,000 USD**, strictly isolated, non-investable by default and never included in main-investment NAV. It is not the 20,000 USDT extreme-crash reserve.
- There is no separate 200,000 USD strategic term-deposit layer, no additional 18,425 USDT ordinary dry-powder layer, and no separate Structural/Asymmetric capital pool in the current architecture.
- Core BTC is managed by valuation and historical drawdown; trend controls pacing. Ordinary short-term bearishness is not a reason to casually sell core BTC.
- Swing BTC is managed by realized net USDT profit after fees and reasonable slippage. BTC quantity change, BTC Buy&Hold-relative return and missed-upside cost are diagnostics only; none is a minimum-profit veto.
- Hunter's total active acquisition cost plus live pending buy reservations must not exceed 20,000 USDT. The existing approximately 6,000U AXS position is part of Hunter; no new live altcoin BUY/ADD or mechanical averaging down until AXS exits net profitable after fees and Hunter has sufficient independently reviewable profit evidence. Unknown balances/orders mean available capacity is UNKNOWN.
- The 20,000 USDT extreme-crash reserve is only for flash crashes, systemic liquidity shocks or severe mispricing; ordinary 5%-10% volatility does not qualify.
- Only the user executes real trades. No automation may auto-trade or change any capital cap.

## 3. BTC Accumulation Regime

The architecture uses conditions, not a fixed price prediction.

A0 EXPENSIVE / NO ACCUMULATION
- Preserve dry powder.
- No strategic-pool deployment for BTC accumulation.

A1 EARLY ACCUMULATION
- Bear-market/valuation conditions have improved but no extreme dislocation.
- Only limited staged deployment is allowed.

A2 DEEP ACCUMULATION
- Material valuation compression plus meaningful deleveraging/liquidity stress or equivalent independent confirmation.
- Larger staged deployment may be proposed.

A3 CAPITULATION
- Broad forced selling / liquidation / panic / liquidity dislocation with BTC long-term thesis intact.
- High deployment intensity may be proposed, still staged.

A4 GENERATIONAL OPPORTUNITY
- Exceptional multi-domain undervaluation/dislocation with BTC thesis intact and no fundamental impairment.
- Maximum strategic risk budget may be proposed, but never by exhausting all liquidity or L0.

A1-A4 are not price-only levels. BTC price, valuation, liquidity, forced selling, ETF/capital flow, macro regime and market structure must be evaluated together under the existing Sentinel evidence rules.

## 4. Anti-calendar rule

The >=4 BTC objective is STRATEGIC_TARGET, not MANDATORY_DEADLINE.
Forbidden behavior:
- buying solely to reach a BTC quantity by a date;
- spending all dry powder because a predicted bottom was reached;
- assuming any fixed bottom such as 33K must occur.
If the market does not offer acceptable risk/reward, holding cash is valid.

## 5. BTC Grid mandate

BTC Grid is a separate module outside the 200,000 USDT main investment budget:
- configured capital: 14,000 USDT;
- configured range and grid count: read only from `state.json` (latest repository configuration shows 75,000–89,000 USDT and 28 grids, last confirmed 2026-09-25; exchange-live status is not verified);
- hard capital ceiling: 20,000 USDT, not an allocation target.
Grid capital, orders and P&L must be audited separately and must never be merged into the main-investment NAV until a user-confirmed close/settlement and explicit synchronization. The 50,000 USD crisis/living reserve is never Grid funding. Counterparty Risk Gate overrides Grid profitability.

## 6. Hunter altcoin-pool mandate

Hunter is the only current altcoin capital pool and has a 20,000 USDT hard cap based on active acquisition cost plus live pending buy reservations. Hunter has no independent execution authority.
Required chain before any future capital proposal: research gate -> BTC-relative forward opportunity gate -> supply/value-capture/liquidity/risk gates -> pool-capacity reconciliation -> Portfolio Allocation Gate -> Portfolio Drawdown Gate -> user approval.
PRE_MOVE or candidate status alone is never a BUY signal. Until the existing approximately 6,000U AXS position exits net profitable after fees and Hunter has sufficient independently reviewable profit evidence, new live altcoin BUY/ADD and mechanical averaging down remain blocked. Research and shadow validation may continue.

## 7. Retired Structural capital lane

The former separate Structural 15% allocation and 5% single-asset limit are retired and non-executable. There is no separate Structural capital pool in the current 200,000 USDT architecture. Research may continue as general diligence, but any altcoin capital exposure must be accounted for inside the Hunter 20,000 USDT pool and remains subject to the AXS/profit-evidence lock.

## 8. Counterparty Risk Veto

Counterparty/custody safety has veto priority over return.
For Bybit:
- GREEN: normal operation allowed, residual CEX risk remains.
- YELLOW: stop adding funds; prioritize principal safety; close/stop nonessential exposure and promptly withdraw removable principal/long-term assets.
- ORANGE/RED: emergency exit of all removable nonessential custody funds.
A profitable strategy is never a reason to ignore YELLOW+ counterparty risk.

## 9. Portfolio Drawdown Gate

Every new capital proposal must include a portfolio stress test before approval.
Minimum stress scenario:
- BTC: -50%
- Grid risk asset exposure: -40%
- Hunter altcoin pool: -70%
- 待命USDT / 50,000 USD crisis reserve: nominally stable, but the crisis reserve remains isolated and excluded from investable NAV.

Required outputs:
- stressed portfolio loss in USD;
- stressed portfolio loss as % of investable net worth;
- remaining dry powder;
- crisis/living-reserve isolation status;
- core BTC status;
- counterparty concentration.

Until a separate empirically justified numeric maximum-drawdown budget is explicitly approved by the user, the Drawdown Gate is CONSERVATIVE / NO-FABRICATED-THRESHOLD: it must calculate and disclose stress loss but may not invent a hard percentage limit.

## 10. No leverage

Default architecture: NO LEVERAGE.
Prohibited as core wealth-building methods:
- BTC perpetual/futures leverage;
- leveraged altcoin positions;
- borrowing against BTC to recursively buy BTC;
- recursive lending/borrowing loops.
Any future exception requires an explicit architecture amendment, not a module-level decision.

## 11. Benchmark and scorecard

Monthly architecture scorecard:
1. Net Worth
2. BTC Quantity
3. BTC Average Cost (only from verified cost data; UNKNOWN if not reconstructable)
4. Dry Powder
5. 50,000 USD crisis/living reserve isolation (excluded from investment NAV)
6. Active risk exposure (BTC Grid + Hunter + extreme-crash reserve)
7. Maximum Drawdown from verified portfolio history
8. BTC-relative Performance

Benchmarking must not use return alone. Compare return and drawdown/risk. No module may claim value-add solely because it made a positive nominal return.

## 12. Decision authority

LEVEL 1 — RESEARCH
Automations/modules may collect data, calculate, classify regimes and generate alerts.

LEVEL 2 — CAPITAL PROPOSAL
Modules may propose an allocation only after all required gates pass. Proposal is not execution.

LEVEL 3 — EXECUTION
Only explicit user authorization can create a real capital action.
After execution, state synchronization order is mandatory:
1. append decision journal;
2. update portfolio-state.json;
3. update relevant module ledger/state;
4. reread and verify.
Partial failure must be surfaced as STATE SYNC DEGRADED/FAILURE.

## 13. Highest-level hard rules

- No leverage by default.
- 50,000 USD Crisis Reserve is non-investable by default.
- BTC is the primary four-year wealth engine.
- >=4 BTC is a strategic target, never a forced calendar target.
- Always preserve dry powder.
- BTC Grid <= 20,000 USDT.
- Hunter altcoin risk may never exceed its 20,000 USDT cost-plus-pending-reservation cap or threaten core BTC and portfolio survival.
- No separate Structural/Asymmetric capital pool exists.
- Counterparty Risk Gate can veto every return strategy.
- Never force trades to hit annual return targets.
- Every capital action must pass the Portfolio Allocation Gate.
- User retains execution authority.
- No new scheduled task is created for this architecture.

## 14. Hierarchy

4-Year Wealth Architecture
  -> Portfolio Allocation / Drawdown / Counterparty Gates
    -> BTC Sentinel / BTC Grid / Hunter
      -> Capital Proposal
        -> User Approval
          -> Execution
            -> Journal + Portfolio SSOT + Module State + Reread Verification

## 15. Governance

This file is the highest-level wealth-management policy SSOT.
It does NOT replace portfolio-state.json as the balance SSOT or automation-logic-final.md as module execution logic.
If lower-level logic conflicts with this architecture on capital safety, capital isolation, leverage, counterparty veto or execution authority, this architecture prevails.
Material changes require explicit user approval and a versioned GitHub update.
