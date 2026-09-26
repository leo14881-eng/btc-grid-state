# Hunter independent buy-gate review — 2026-09-26

**Research-only; no executed trade, no approved new BUY.** Data snapshot: Binance 2026-09-26T08:28:32Z; 490 Binance spot pairs; Bybit unavailable (HTTP 403). Snapshot prices are NOT executable live quotes. Hunter 25 dossier records are all RESEARCH_INCOMPLETE. This review adds source-specific external checks without retroactively modifying frozen discovery records. Past appreciation does not disqualify future research.

| Asset | Binance snapshot USD | Observed 24h | Independently checked facts | Remaining critical gate | Current capital action |
|---|---:|---:|---|---|---|
| DOLO | 0.03186 | +2.84% | Official DOLO contract 0x0F81001eF0A83ecCE5ccebf63EB302c70a39a654, oDOLO pairs with DOLO to purchase discounted veDOLO; protocol fee distribution to veDOLO remains **conditional on DAO activation**. DefiLlama protocol TVL ~$350-360m, trailing 30d protocol revenue ~$235k (NOT DOLO holder cash flow). CoinGecko circulating ~$512m/1bn, MC ~$15.9m, FDV ~$31m. Third-party tokenomics Oct24 2026 unlock ~9.44m DOLO; its 28% unlocked vesting metric conflicts with 51% market circulation, so reconcile definition and independently verify 30/90/180d saleable float. | Official actual fee-switch activation, on-chain unlock wallet and saleable supply, venue executable spread/depth, 3-scenario forward tokenholder valuation | **CONDITIONAL_RESEARCH_ONLY; NO ORDER** |
| AERO | 0.8854 | +18.43% | Aerodrome official docs: veAERO voters receive exchange fees and incentives, *unlocked AERO does not directly receive these fees*; weekly emissions approximately 10.9% annualized as of Apr26; official plan to merge with Velodrome to become Aero in 2026, terms/timing not independently verified. 7d +34.05%, near 30d highs. | Migration/conversion and issuance terms, current price/depth, net tokenholder return after inflation and opportunity cost | **NO NEW BUY AT SNAPSHOT; MIGRATION GATE** |
| KMNO | 0.04366 | +16.46% | Official Kamino Sep17 Galaxy curator and Sep24 USDai launch show product activity, NOT guaranteed KMNO fee capture. Tokenomist lists Sep30 core-contributor unlock, only ~56% unlocked; DefiLlama token-rights page (updated May15) says fee switch off, no verified buybacks. | Exact Sep30 sellable release and on-chain wallet, up-to-date official KMNO holder fee capture, fresh market depth | **WAIT_UNLOCK_RECONCILIATION** |
| 2Z | 0.07387 | +33.48% | Tokenomics.com lists Oct2 2026 ~1.625bn unlock (~46.4% current MCAP); separate article claims 1.655bn (~47.7% circulating); sources differ. | Verify official contract-specific cliff and exact supply before reconsideration | **BLOCKED_MAJOR_NEAR_UNLOCK** |
| MUBARAK | 0.05333 | +22.65% | Tokenomics.com claims 100% vested; CoinGecko Hunter snapshot MC/FDV both ~$52.5m. 7d volume ~13.8x prior 7d; 24h ~37m USDT. | Official concentration, holders and real catalyst, sustainable demand and risk/reward; vesting complete is not proof of no whales | **NO_NEW_BUY_SENTIMENT_ONLY** |
| PHA | 0.076 | +34.04% | Official Phala blog shows 2026 confidential-AI product announcements; Hunter 7d return ~112%, volume ~10.2x prior 7d. | Verified PHA holder value capture from cloud business, precise circulating/emission supply, depth after surge; review official June 2026 API vulnerability disclosure | **NO_NEW_BUY_AFTER_SURGE_WITHOUT_CAPTURE** |
| TNSR | 0.0449 | +17.23% | Tensor marketplace official docs document fees but not direct TNSR holder distribution. Tokenomist claims ~743m of 1bn unlocked while its summary also says 'fully unlocked'; inconsistent, investigate. | Token-holder fee claim and reconciled unlock data | **RESEARCH_ONLY** |

## Sources and source quality
- Official DOLO mechanics: https://docs.dolomite.io/dolo/token-mechanics
- Official DOLO distribution (initial schedule, not live unlocked float): https://docs.dolomite.io/dolo/distribution
- DOLO token market data: https://www.coingecko.com/en/coins/dolomite
- DOLO protocol fees, not tokenholder dividends: https://defillama.com/protocol/dolomite
- DOLO third-party vesting (reconcile conflicting unlocked/circulating definitions): https://app.tokenomics.com/tokenomics/dolomite/unlocks
- Aerodrome official token economics and pending merger: https://aerodrome.finance/docs
- KMNO product official: https://kamino.com/blog/galaxy-vault-curation-kamino and https://kamino.com/blog/compute-backed-credit-comes-to-kamino
- KMNO vesting third-party: https://tokenomist.ai/kamino/tokenomics
- KMNO token rights third-party (May 2026 snapshot, may be stale): https://defillama.com/token/KMNO
- 2Z vesting third-party: https://app.tokenomics.com/tokenomics/doublezero/unlocks
- MUBARAK vesting third-party: https://app.tokenomics.com/tokenomics/mubarak/unlocks
- PHA official product/security blog: https://phala.com/blog
- Tensor third-party vesting with inconsistent summary: https://tokenomist.ai/tensor/unlock-events

## Immediate follow-up and gate
1. Research DOLO actual net holder economics and exact near-term insider unlock wallets; distinguish low token MC from protocol TVL (TVL is customer deposits, not enterprise equity). Reconcile 28% vested vs 51% circulating rather than selecting whichever seems favorable. Do not turn a price-only breakout into a buy recommendation.
2. Research AERO/Aero merger and effective per-token revenue under ongoing issuance; do not assume current AERO directly receives veAERO revenue.
3. On KMNO and 2Z reconcile next cliff and confirm live saleable supply before any capital proposal.
4. All BUY proposals must pass fresh orderbook, full portfolio cost+reservation <=20,000 USDT, drawdown budget, counterparty and BTC-core/reserve segregation. If any fatal blocker persists, **zero new order**; continue researching other candidates. No invented target prices, probabilities, return guarantees or portfolio allocation.
