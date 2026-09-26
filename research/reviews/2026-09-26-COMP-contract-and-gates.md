# COMP first primary-source verification — 2026-09-26 UTC

Status: **CONTRACT_IDENTITY_THIRD_PARTY_CORROBORATED; RESEARCH_ONLY; NO BUY**.

## Direct official verification
- Compound's official governance page explicitly publishes Ethereum mainnet COMP contract `0xc00e94Cb662C3520282E6f5717214004A7f26888`: https://compound.finance/governance/comp
- Compound's official v2 deployment docs separately list mainnet COMP token (not the cCOMP lending receipt token): https://docs.compound.finance/v2/
- Official governance documentation describes COMP as an ERC-20 governance/voting token. It does **not** by itself establish that all Compound protocol fees are directly distributed to liquid COMP holders: https://docs.compound.finance/v2/governance/
- Binance active spot pair: `COMPUSDT` from Hunter scan.
- Independent CoinGecko contract corroboration: https://www.coingecko.com/en/coins/compound-governance-token (verified by the hourly Hunter identity audit against exact Ethereum platform+contract, not just ticker).
- These facts are persisted in `research/hunter-verified-facts.json`; exact independent match is published in `research/results/hunter-identity-audit.json`.

## Market observations are NOT valuations
- Hunter Binance snapshot 2026-09-26 ~09:24 UTC: COMP ~$23.9; CoinGecko market cap ~$238.9m and circulating/total supply 10m in third-party snapshot.
- The current CoinGecko 10m/10m snapshot is not a primary-source dated 30/90/180-day future saleable-supply schedule. Do not treat circulating=total as proof of no treasury sales or exchange inventory.
- Public Binance orderbook spread/depth is sampled per scan; refresh immediately before any user-review-only proposal. Do not reuse this review's market price as an executable quote.

## Outstanding independent buy gates
1. Verify actual COMP governance value accrual vs protocol business fees and relevant governance changes from primary sources; governance rights alone are not equivalent to a cash dividend.
2. Verify current and future treasury, foundation and emissions/saleable supply for 30/90/180d from current official or on-chain sources.
3. Identify a dated catalyst that has a defensible causal path to COMP token demand/value capture, not merely Compound lending TVL.
4. Model source-grounded bear/base/bull market-cap assumptions with same-horizon BTC opportunity cost; reject fabricated point targets and guaranteed returns.
5. Refresh orderbook and check user-confirmed live alt portfolio open cost + pending reservations + proposal <=20,000 USDT, drawdown and counterparty gates.
6. Prospective Hunter audit still lacks matured 24h/7d/30d/90d evidence of actual discovery alpha.

**Capital status:** zero COMP purchase recommendation at this stage. Identity is now independently corroborated, but remaining economic and supply gates are open research tasks.
