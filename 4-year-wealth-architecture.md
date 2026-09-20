# 4-Year Wealth Architecture — FINAL v1.0

Status: ACTIVE / FROZEN
Effective date: 2026-09-20
Horizon: 4 years
Role: Highest-level capital architecture above BTC Sentinel, Grid, Hunter and Structural modules.
Execution authority: USER ONLY.
Automation budget: ZERO new scheduled tasks.

## 1. Objective hierarchy

1. Avoid permanent capital impairment and ruin.
2. Preserve liquidity and dry powder so the portfolio is never forced to sell core BTC for ordinary cash needs.
3. Maximize four-year net worth on a risk-controlled basis.
4. BTC is the primary wealth engine. Strategic BTC quantity target is >= 4 BTC, but this is NOT a calendar deadline and must never force buying at unattractive conditions.
5. Active strategies are return enhancers, not substitutes for the core architecture.

## 2. Capital layers and ownership

Portfolio-state.json remains the balance/allocation SSOT. Module state files own only their module runtime state.

L0 CRISIS RESERVE
- Current reference: USD 50,000.
- Strictly isolated.
- Default investable amount: USD 0.
- Must not be counted as BTC dry powder or Hunter/Structural capital.
- Use requires an explicit user decision that the capital is no longer crisis reserve.

L1 STRATEGIC POOL
- Current reference: approximately USD 200,000.
- Primary future deployment pool.
- Not automatically deployable merely because it exists.
- Deployment requires regime + portfolio Allocation Gate.

L2 ORDINARY DRY POWDER
- Current user-confirmed reference: 18,425 USDT.
- Available for approved ordinary portfolio deployment.
- Must remain subject to dry-powder preservation rules.

L3 BTC CORE
- Current reference: 0.27803703 BTC.
- First permanent-floor milestone: 0.5 BTC.
- Long-term strategic target: >= 4 BTC.
- While below 0.5 BTC, ordinary cycle distribution must not reduce BTC holdings.
- After exceeding 0.5 BTC, only BTC above the permanent floor may become cycle-sellable under the existing BTC cycle policy.

L4 ACTIVE STRATEGIES
- BTC Spot Grid current capital: 14,000 USDT.
- Grid hard cap: 20,000 USDT.
- Structural and Asymmetric/Hunter positions require their own gates plus the portfolio-level gate.
- Active strategies may never raid L0.

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

## 5. Grid mandate

Grid is an ACTIVE RETURN ENHANCER:
- range/reference currently 72,000–86,000 USDT;
- 28 grids;
- current capital 14,000 USDT;
- hard capital cap 20,000 USDT.
Grid goals:
1. monetize volatility;
2. accumulate BTC during declines.
Grid capital is isolated from ordinary allocation accounting.
Grid BTC is not added to permanent BTC Core until the strategy is closed/settled and the resulting BTC is user-confirmed and synchronized into portfolio-state.json.
Counterparty Risk Gate overrides grid profitability.

## 6. Hunter / Asymmetric mandate

Hunter discovers asymmetric opportunities; it has no independent capital authority.
Required chain before any capital proposal:
Hunter research gate -> BTC-relative forward opportunity gate -> module capital gate -> Portfolio Allocation Gate -> Portfolio Drawdown Gate -> user approval.
PRE_MOVE or candidate status alone is never a BUY signal.
A single altcoin must never be allowed to threaten BTC Core, L0, or portfolio survival.

## 7. Structural mandate

Structural positions are multi-year thesis positions, separate from Hunter.
A Hunter candidate may be promoted to Structural only after durable fundamental/value-capture evidence and explicit portfolio reclassification.
Buying an asset does not automatically make it Structural.

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
- Hunter/Asymmetric: -70%
- Structural: -50%
- Cash/L0: nominally stable unless the proposal itself introduces custody/stablecoin risk.

Required outputs:
- stressed portfolio loss in USD;
- stressed portfolio loss as % of investable net worth;
- remaining dry powder;
- L0 status;
- BTC Core status;
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
5. Permanent/Crisis Reserve
6. Active Risk Exposure (Grid + Hunter + Structural)
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
- Hunter/altcoin risk may never threaten BTC Core or portfolio survival.
- Counterparty Risk Gate can veto every return strategy.
- Never force trades to hit annual return targets.
- Every capital action must pass the Portfolio Allocation Gate.
- User retains execution authority.
- No new scheduled task is created for this architecture.

## 14. Hierarchy

4-Year Wealth Architecture
  -> Portfolio Allocation / Drawdown / Counterparty Gates
    -> BTC Sentinel / Grid / Hunter / Structural
      -> Capital Proposal
        -> User Approval
          -> Execution
            -> Journal + Portfolio SSOT + Module State + Reread Verification

## 15. Governance

This file is the highest-level wealth-management policy SSOT.
It does NOT replace portfolio-state.json as the balance SSOT or automation-logic-final.md as module execution logic.
If lower-level logic conflicts with this architecture on capital safety, capital isolation, leverage, counterparty veto or execution authority, this architecture prevails.
Material changes require explicit user approval and a versioned GitHub update.
