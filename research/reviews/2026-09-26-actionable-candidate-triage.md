# Hunter decision-oriented candidate review — 2026-09-26, Binance-only

**Evidence timestamp:** Binance scanner 2026-09-26T10:18:12Z; prior market-structure observations may be from earlier research cycles, not necessarily simultaneous. **No BUY approval or execution.** Reviewed candidates are preserved in `research/hunter-reviewed-watchlist.json` and are first in live-orderbook collection; unrelated rotating signals cannot silently drop their research. Prices below are historical scanner reference prices, NOT live executable quotes.

| Asset | Binance snapshot | Actual finding | Action now | Independent evidence still required |
|---|---:|---|---|---|
| DOLO | $0.03216 | Official 1bn initial supply; CoinGecko DOLO ID `dolomite` and Ethereum contract match official docs; CoinGecko roughly $15.9m circulating MC / $30.9m FDV as-of review; oDOLO 1:1 DOLO pairing creates demand, but liquid DOLO does **not** directly earn protocol revenue and veDOLO fee sharing is conditional on DAO activation. Prior 7d quote volume ~6.81x previous 7d, Binance 24h spot volume only ~$400k, so liquidity requires live depth. | **PRESERVE_FOR_DEEP_REVIEW, NO BUY YET.** Verify actual DAO fee activation, holder value capture, 30/90/180d unlock wallet flows, fresh depth and BTC-relative upside. | Official fee switch/on-chain DAO evidence, actual forward saleable supply, documented market-cap scenario assumptions. |
| 2Z | $0.07524 | Independent third-party schedule indicates major October 2, 2026 insider-heavy unlock. Upbit-published projected circulation rises from 3,469,417,500 at September month-end to 5,113,013,125 at October month-end: **+47.38% projected supply**. At constant market cap and no demand response, the mechanical price ratio would be 3.4694/5.1130 = ~0.6785 (hypothetical **-32.15%**); NOT a price forecast. The third-party Oct 2 cliff amount differs from the monthly schedule. Issuer's initial disclosure describes multi-year insider vesting and no contractual dividend/profit-sharing rights. | **MATERIAL_SUPPLY_EVENT_PENDING: no new capital proposal until actual on-chain release and saleable float reconciled.** Not a claim price must fall. | Confirm 2026-10-02 actual on-chain event, amount/beneficiary wallets, actual demand and pricing. |
| AERO | $0.9000 | Prior 7d gain ~36.5%, volume ~1.68x previous 7d. Liquid AERO does not automatically receive locked veAERO fees; migration/issuance economics require reconciliation. | **KEEP_CONTINUATION_REVIEW, NO BUY YET.** Prior rally alone does not disqualify it. | Confirm official migration terms, net value capture after issuance and BTC-relative prospective scenario. |
| ENA | $0.2788 | Prior 7d gain ~36.4%, volume ~2.68x previous 7d, Binance 24h volume ~$140.7m. Price momentum is not verified forward upside. | **KEEP_CONTINUATION_REVIEW, NO BUY YET.** | Independent exact token identity, current official forward unlocks and tokenholder value capture. |
| COMP | $23.91 | Official contract independently corroborated, prior 7d volume ~4.7x previous 7d; no independently verified forward economics or causal tokenholder cash flow. | **PRESERVE_RESEARCH, NO BUY YET.** | Forward supply, actual tokenholder value capture, defensible forward scenarios, fresh depth. |

## Method and non-fabrication constraints

1. These are research and risk actions, not expected-return ranks. A large rally does not auto-exclude a coin, and small market cap alone is not a reason to buy.
2. DOLO's ~$360m protocol TVL is customer deposits, not the DOLO token's value or holder cash flow. Fee switch remains unverified as active. Do not populate `forward_supply_verified` from current market-data circulation alone.
3. 2Z's October forecast is a **published schedule**, not proof of realized circulation. Third-party cliff estimates and the month-end schedule use different dates/methods. The constant-cap dilution calculation is a counterfactual illustration, not a trading prediction.
4. Current pipeline has no trustworthy, sourced bear/base/bull terminal market caps or 30/90/180d forward saleable supply for these candidates. Do not invent them to turn a research candidate into a BUY. Existing 20,000 USDT altcoin original-cost cap and independent portfolio, liquidity, counterparty and BTC-relative gates remain mandatory.
5. Fix effectiveness must be measured by **DOLO retained in live dossiers, 2Z unresolved supply risk surfaced in health, reviewed candidates probed for fresh Binance orderbooks, and newly corroborated DOLO contract identity** — not by test count alone.

## Sources

- DOLO official mechanics/contract/conditional veDOLO fees: https://docs.dolomite.io/dolo/token-mechanics
- DOLO official initial allocations: https://docs.dolomite.io/dolo/distribution
- DOLO independent ID, contract, current supply and market metrics: https://www.coingecko.com/en/coins/dolomite
- DOLO rights caveat (third-party April 2026, potentially stale): https://defillama.com/token/DOLO
- 2Z issuer original disclosure: https://doublezero.xyz/2z-tokenomics-disclosure.pdf
- 2Z Upbit month-end forecast (may be revised): https://static.upbit.com/guide/circulating_supply/2Z_20251002.pdf
- 2Z third-party Oct 2 unlock estimate, NOT primary proof of actual release: https://app.tokenomics.com/tokenomics/doublezero/unlocks
- Aerodrome official docs: https://aerodrome.finance/docs
- COMP official governance and token: https://compound.finance/governance/comp

**Next substantive investigation:** first reconcile DOLO's real future supply and active veDOLO fee switch; separately check 2Z on-chain October 2 release and post-event depth. AERO and ENA require source-grounded forward tokenholder economics before any proposed entry.
