# Shared V1/V2 loss plus market freeze — draft, not deployed

Each lane evaluates its own portfolio loss ledger. The only new loss-freeze trigger is L AND BTC_BAD AND ALT_BAD.

* L: two consecutive losing exits OR at least 1000 USDT realized losses during the current six hours.
* BTC_BAD: BTC 5m <= -2.5%, 15m <= -4%, OR 1h <= -6%.
* ALT_BAD: existing Binance altcoin 24h negative breadth >= 85% OR breadth down at least 5% >= 50%. The existing collector excludes BTC and its stablecoin base set.
* Evidence: one complete observation, <=900 seconds old, no future timestamps. Missing evidence is UNKNOWN; a systemic HIGH label alone is not bad-market confirmation.

Normal-market losses still update actual PnL and per-asset reentry locks. They do not quarantine principal or create a loss episode. The independent systemic gate and its recovery are unchanged. V1 remains broad discovery entry with no total capital cap. V2 retains executable entry and 20000/17000/3000 bounds.

Loss accounting and loss_freeze_episode are distinct. Both BUY loops, direct ADD and allocator admission share one gate. Rolling losses and eligible principal are recalculated at evaluation time. Expired losses from a broken streak are excluded from a later episode's principal; an unbroken consecutive-loss sequence can still qualify regardless of the rolling window. Previously consumed losses cannot recreate a completed episode without a new losing exit.

Qualified episodes retain durable observation IDs, >=300-second independent observations, and 25% principal release after the second NORMAL observation. A duplicate, restart, future timestamp or single NORMAL cannot clear a qualified episode.

Existing legacy WATCH/TRIPPED/RECOVERING state completes its original natural recovery before activation. No historical B is fabricated and no production state is cleared. Activation saves the legacy completion record. Historical SELLs, closed positions, PnL, manual-stop receipts and other ledgers are never rewritten by this PR.

## Integration contract

This PR owns the shared loss gate, risk update and quarantine adapters in hunter_shadow_trader_v2.py and the monitor's call to that adapter. The other reentry task owns reentry lifecycle rules and the exact new-BUY note "2026.0.10.11 新策略". Preserve loss_freeze.circuit_for_admission(state) as the reentry circuit data source when merging both changes. Its inclusion changes the source of active freeze state, not the existing reentry conditions. Keep ADD distinct from a new BUY after liquidation.

This PR alone does not enable or write the complete new-strategy label. The parent must review the combined diff and run both sets of lifecycle, BUY/ADD, risk and label tests before approving the combined release. No partial release should label itself as the complete new strategy.

Baseline for resumed implementation: d5f7937ecda1188f2ef367fc5ad1a0e07b9698cf. PR98/35 remain excluded. No real orders, state artifact edits, scheduler triggers, credentials or uploads. Code and offline CI only; separate approval is required before merge/deployment.
