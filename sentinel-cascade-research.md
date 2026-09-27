# Sentinel Cascade Research v0.2

Status: approved SHADOW RESEARCH; not production deployment. No trading or frozen-rule changes.

Goal: detect accumulating fragility and correlated contagion regardless of initiating catalyst. Not every flash crash has detectable advance warning.

Independent domains:
- Executable liquidity: BTC/SOL bid depth, spreads, slippage, venue dispersion.
- Leverage: OI in consistent units, funding, basis and collateral stress.
- Spot demand: verified spot flow, price response and completed ETF sessions.
- Macro: yield, USD, equity and credit stress, verified policy shocks.
- Counterparty: withdrawals, venue failures, reserve-data freshness and collateral pricing anomalies.
- Chain/protocol: stablecoin deviations across venues, bridges, oracles and settlement issues.

Method: record source, timestamp, unit, baseline and freshness for every observation. Track deterioration velocity and co-occurrence; group dependent observations to avoid counting one liquidation chain as several independent signals. Unknown or conflicting inputs are not healthy signals.

Shadow labels: FRAGILITY_WATCH, EARLY_CASCADE_WARNING, CONTAGION_ESCALATING, SYSTEMIC_RISK_CANDIDATE, RECOVERY_WATCH, INSUFFICIENT_DATA. Early warning requires at least two causally independent domains including non-price evidence. Local exchange anomalies remain local until propagation is corroborated. These labels do not replace frozen Sentinel states or Bybit risk grades.

Replay: point-in-time stress episodes and matched non-crash volatility. Measure warning lead time, missed events, false alerts/month, data latency and availability. Avoid retrospective leakage. No numeric trigger is validated until replay passes and user approves promotion.

Integration: research-only; existing BTC floor, capital allocation, staged Module B, isolated crisis reserve, Grid and user-only execution remain unchanged. Hourly scans cannot guarantee detection of minute-scale events. Production integration requires verified live feeds, replay results and separate approval.
