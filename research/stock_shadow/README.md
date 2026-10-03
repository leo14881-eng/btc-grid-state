# Stock Shadow — V3 SSOT

> **Status:** active paper-trading research system  
> **Strategy version:** `HYBRID_ENTRY_V1_POSITION_STATE_V3`  
> **Scope:** US-listed common stocks only  
> **Execution:** simulation only; never places real orders  
> **Source of truth:** this document describes the current Stock Shadow strategy and lifecycle implemented on `main`.

## 1. Purpose and isolation

Stock Shadow is an independent US-stock shadow trading system designed to create a large, auditable BUY / ADD / SELL sample set for forward validation.

It is isolated from all non-stock systems. Stock Shadow code, tests, workflows, and persistent state are limited to:

- `research/stock_shadow/**`
- `research/results/stock-shadow/**`
- `tests/test_stock_shadow.py`
- `.github/workflows/stock-shadow*.yml`

It does not place real orders. There is no portfolio-wide cap on the number of open shadow positions.

## 2. Persistent records

All persistent outputs live under `research/results/stock-shadow/`.

- `portfolio-v1.json` — current open positions, tranches, weighted average price, latest price, net PnL / return, MFE / MAE, position state, recovery-add evidence, profit-protection state, and closed-position records.
- `trades-v1.json` — append-only logical trade-event ledger for formal BUY / ADD / SELL events.
- `summary-v1.json` — latest market scan and aggregate system statistics.
- `position-monitor-v1.json` — latest five-minute position-monitor health/result state.

Profitability is evaluated on a **net-after-simulated-fees** basis. The primary return fields are `net_return_pct` and `realized_net_return_pct`, not gross price return.

## 3. Universe and market data

The hourly manager discovers US-listed common stocks from Nasdaq Trader symbol directories:

- Nasdaq-listed securities
- Other US exchange listings

It excludes ETFs, test issues, warrants, units, rights, preferred shares, and obvious depositary-like securities.

Daily market snapshots currently come from free public chart data and derive:

- current daily close
- 5-day return
- 20-day return
- SMA20
- 20-day average dollar volume
- 20-day daily volatility
- latest-volume / average-volume ratio
- 20-day high / low
- 20-day range position

SPY and QQQ are used as broad-market relative-strength benchmarks.

### Known market-data limitation

Exact bid/ask spread and execution slippage are not currently available in the full-market scan path. Sector-relative ETF mapping is also not implemented yet.

## 4. Tradeability gates

Current constants:

| Rule | Value |
|---|---:|
| Standard shadow tranche | 1,000 |
| Maximum tranches per position | 5 |
| Low-price reference | $2 |
| Minimum average dollar volume | $10M |
| Minimum avg dollar volume for price < $2 | $25M |
| Normal minimum score | 68 |
| Early-entry minimum score | 62 |
| Maximum normal 5D return | 18% |
| Maximum normal 20D return | 45% |
| Maximum normal SMA20 extension | 18% |
| Minimum 20D return | -8% |
| Parabolic 5D reject | >35% |
| Parabolic 20D reject | >80% |
| Parabolic SMA20 extension reject | >30% |

A stock is **not rejected solely because its price is below $2**. Low-priced names must instead satisfy the stronger liquidity requirement.

## 5. Candidate scoring

Candidate score combines a small number of interpretable dimensions:

- liquidity: up to 15
- 20-day trend: up to 20
- 5-day momentum: up to 20
- relative strength versus SPY / QQQ: up to 25
- price structure versus SMA20: up to 10
- volatility quality: up to 10

The system can reject candidates for insufficient liquidity, weak trend, excessive extension, parabolic moves, or being materially below trend.

## 6. BUY — three entry structures

A formal BUY is created only when one of the following entry structures is ready.

### EARLY_ACCUMULATION

Designed to detect an earlier accumulation / participation anomaly without requiring a breakout.

Requires, among other gates:

- score >= 62
- 5D return between -4% and +8%
- 20D return between -3% and +18%
- SMA20 distance between -3% and +8%
- volume ratio >= 1.35
- non-negative relative strength versus SPY / QQQ
- 20D range position >= 0.45

### PULLBACK_IN_TREND

Designed for a controlled pullback inside an established trend.

Requires, among other gates:

- score >= 68
- 5D return between -3% and 0%
- positive 20D return
- SMA20 distance between -3% and +12%
- positive relative strength versus SPY / QQQ

### MOMENTUM_TREND

Designed for confirmed right-side strength without accepting a parabolic chase.

Requires, among other gates:

- score >= 68
- 5D return between 0% and +18%
- 20D return <= 45%
- SMA20 extension <= 18%

Entry precedence is:

`EARLY_ACCUMULATION -> PULLBACK_IN_TREND -> MOMENTUM_TREND`

A new BUY cannot also ADD during the same manager run.

## 7. Position State

The position-state engine deliberately uses a small number of decision states to reduce overfitting:

- `STRONG`
- `HEALTHY_PULLBACK`
- `UNCERTAIN`
- `BROKEN`

It evaluates:

1. broad-market relative performance versus SPY / QQQ
2. trend structure versus SMA20
3. 20-day relative strength
4. short-term selling pressure as recorded evidence

Current structural definitions include:

- `trend_broken`: SMA20 distance < -6% **and** 20D return < 0
- `relative_weak`: 20D relative performance versus SPY / QQQ < -5%
- `BROKEN`: requires **both** trend structure break and relative weakness

Therefore, a broad-market decline by itself does not automatically cause a structural SELL. A stock falling with the market can remain a healthy pullback or uncertain state.

Sector-relative status is currently recorded as `UNAVAILABLE_V2_BASELINE`; sector mapping is not yet active.

## 8. ADD — V3 recovery logic

V3 explicitly rejects the old idea of mechanically adding simply because price fell.

The system first tracks the position's swing high. A drawdown greater than 1% from that swing high marks that a real pullback has occurred and begins tracking a pullback low.

An ADD becomes eligible only when all of the following are true:

- a real pullback was previously observed
- current Position State is not `BROKEN`
- price has recovered above the tracked pullback low
- market-relative strength is improving versus the previous position-state observation
- the stock still passes a valid entry decision
- fewer than 5 tranches are already open
- the position was not newly opened in the same run

Formal ADD reason:

`PULLBACK_RECOVERY_ADD_V3`

After an ADD, the pullback marker is reset. Continued decline alone never creates another ADD.

## 9. SELL — V3 exit engine

V3 has two independent full-position exit paths.

### A. Net-profit giveback protection

Simulated fee rate is currently `0.2%` per side.

Profit protection arms after the position reaches at least **+1.0% MFE net of simulated fees**.

The current positive-net-profit floor is based on maximum net profit achieved:

| MFE net profit | Allowed giveback | Protected floor |
|---|---:|---:|
| 1% to <8% | 50% of MFE | retain about 50% |
| 8% to <15% | 35% of MFE | retain about 65% |
| 15% to <30% | 25% of MFE | retain about 75% |
| >=30% | 20% of MFE | retain about 80% |

Minimum protected positive net floor: **+0.10%**.

When current net return is still positive but falls to or below the active floor, the hourly manager performs a full shadow SELL with reason:

`NET_PROFIT_GIVEBACK_V3`

This implements the rule that Stock Shadow is not a long-term holding system: meaningful profit should not be allowed to round-trip back to zero before exit.

### B. Structural break

If profit protection has not already caused an exit and Position State becomes `BROKEN`, the hourly manager performs a full shadow SELL with reason:

`STRUCTURE_BROKEN_V3`

Profit-protection SELL has precedence over structural-break SELL when both conditions apply.

## 10. Fees and realized performance

Every lifecycle is evaluated after simulated transaction costs.

Current fee assumption:

`FEE_RATE = 0.002` per side.

For positions containing multiple ADD tranches, final performance uses the entire position:

- total invested notional
- all tranche quantities and prices
- buy-side simulated fees
- final sale proceeds
- sell-side simulated fees
- final net PnL
- final net return percentage

Closed samples record:

- `realized_net_pnl_usdt`
- `realized_net_return_pct`
- `gross_price_return_pct`
- `estimated_total_fees_usdt`
- `profit_giveback_pct_points`
- `exit_reason`
- post-exit review due days: 1 / 3 / 5 / 10

A positive gross stock-price move is **not** classified as a win if simulated fees make realized net PnL non-positive.

## 11. Hourly manager

Workflow:

`.github/workflows/stock-shadow.yml`

Scheduled at minute 23 of every hour.

The workflow:

1. runs isolated Stock Shadow tests
2. refreshes benchmark data
3. checks SEC public-data health
4. runs the full Stock Shadow manager
5. runs winner / loser review
6. enforces the Stock Shadow result-path isolation guard
7. persists only Stock Shadow result state

The workflow uses `stock-shadow-main` concurrency with `cancel-in-progress: false`.

## 12. Five-minute position monitor

Workflow:

`.github/workflows/stock-shadow-position-monitor.yml`

Scheduled every five minutes on weekdays. The monitor:

- reads only already-open positions
- does not discover new stocks
- cannot BUY
- cannot ADD
- refreshes held-symbol prices
- updates weighted average, current net PnL / return, MFE, and MAE
- writes monitor health under the Stock Shadow result directory

Its primary quote path is the Nasdaq public bulk stock snapshot, with a tightly bounded fallback for rare missing held symbols.

### Important V3 consistency gap

The five-minute monitor is **not yet fully aligned with the hourly V3 exit engine**.

At present it can calculate/update a profit-protection signal, but it does **not** execute a full shadow SELL from profit giveback alone. Structural exits also remain the responsibility of the full hourly position-state manager.

Therefore:

- hourly manager: V3 BUY / ADD / actual profit-giveback SELL / structural SELL
- five-minute monitor: position-only quote/MFE/MAE/profit-signal monitoring; no BUY, ADD, or SELL

This limitation must not be described as if intraday five-minute V3 exits are already active.

## 13. Validation policy

Core BUY / ADD / SELL rules should be frozen during forward validation unless a clear implementation or structural defect is found.

The purpose of freezing is not to stop trading. It is to prevent repeatedly tuning the same parameters against the same sample set and creating overfit results.

The system should accumulate real forward shadow samples and compare winner / loser cohorts before evidence-based parameter changes.

Key evaluation fields include:

- formal BUY / ADD / SELL counts
- realized net PnL after simulated fees
- realized net return percentage
- MFE / MAE
- entry structure
- exit reason
- post-exit forward behavior

## 14. Known limitations / next validation items

1. Sector-relative mapping is not implemented.
2. Exact bid/ask spread and realistic slippage are not implemented in the full-market path.
3. Five-minute monitor is not yet aligned with V3 actual SELL semantics.
4. Post-exit +1D / +3D / +5D / +10D due-day metadata exists, but complete forward tracking must be verified before treating it as operational.
5. There is no partial scale-out; current exits close the full shadow position.
6. Full-market discovery makes many individual free daily-chart requests and can return partial market data for some symbols.
7. Extended-hours monitoring is not active.
8. Strategy profitability is not considered proven until a meaningful number of closed forward samples exists.
9. Current state and giveback thresholds are intentionally simple heuristics and must be validated rather than repeatedly optimized in-sample.

## 15. Design principle

**Data may be complex; trading decisions should remain simple and explainable.**

A decline is not automatically a stop. A rally is not automatically a chase. A healthy pullback can become an ADD opportunity only after observable recovery, while a true structural break requires independent confirmation. SELL logic protects already-earned net profit and exits genuinely broken structures.
