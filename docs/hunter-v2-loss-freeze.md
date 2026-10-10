# V2 loss plus market freeze (draft, not deployed)

V2 loss-only freezing requires both L and B. L is two consecutive losing exits or at least 1000 USDT realized losses during the current six hours. B is one complete observation, at most 900 seconds old with no future timestamps: BTC 5m <= -2.5%, 15m <= -4%, or 1h <= -6%, plus Binance altcoin 24h negative breadth >= 85% or breadth down at least 5% >= 50%. The existing collector excludes BTC and its stablecoin base set. A systemic HIGH label alone is not evidence of B.

Normal-market losses still update actual PnL, manual request receipts and per-asset reentry locks. They do not quarantine principal or create a loss freeze. The shared systemic gate and its recovery remain unchanged. V1 uses the original shared loss policy.

The V2 ledger and qualified loss_freeze_episode are distinct. Every BUY, direct ADD and allocator admission uses the same lane-aware gate. Rolling loss is recalculated at evaluation time. A qualified episode quarantines unreleased loss principal and uses the existing durable observation IDs, >=300-second independent observations, and gradual 25% release after the second NORMAL observation. Repeated observations and restarts cannot release twice. Consumed loss sequences cannot recreate a completed episode without a new losing exit. A profitable exit breaks the consecutive sequence.

Legacy WATCH/TRIPPED/RECOVERING state is not recategorized or cleared. Its original natural recovery completes first; activation records an unchanged copy of that completion state. Historical loss events, SELL events, closed positions and manual request receipts are not rewritten. If deployment occurs before legacy recovery completes, the original policy continues until it completes. No historic B is invented.

Audit baseline: main 3c934e6bdcf20a82bd24ece86f710025f2c420d7, portfolio blob ad6701802076ebfe172cd5ab533da6827531d91d; observed 2026-10-10T23:01:43.932119Z: legacy RECOVERING, 500 USDT remaining, four recovery observations. PR98 remains excluded. PR99/100 remain in ancestry. Review current main recovery before deployment.

No state artifacts, real orders, scheduler triggers, credentials or trading workflow changes. Capital authority remains NONE_SHADOW_ONLY; the 20000/17000/3000 bounds are unchanged. This draft needs exact-head review and separate merge/deployment approval. CI is offline Python testing only.
