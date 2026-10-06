# Sentinel migration evidence boundary

2026-10-06: the repository had no Sentinel execution engine or scheduled server job. Its original Leading Warning analysis runs in the enabled ChatGPT automation. Public data collection is now available as a read-only server evidence scan; this does **not** mean the investment analysis has been ported.

`sentinel-runtime.json` retains `CHATGPT_AUTOMATION` as the sole formal state writer. The supplied service always invokes `--preview`. No production `sentinel-state.json`, portfolio, decision journal, Hunter ledger or order is changed by it. Only GitHub main is persistent authority; journal logs are operational evidence, never portfolio authority.

The collector independently timestamps BTC market and derivatives plus Upbit AXS/KRW, Binance AXS spot/OI/funding. Untimestamped depth is explicitly receipt-only and cannot pass source freshness. Missing ETF/macro/liquidation/holder evidence is explicitly listed. Collecting valid market evidence without a faithful Leading Warning analysis returns `ANALYSIS_FAILED` / `ANALYSIS_NOT_PORTED`; it never advances `last_successful_scan_at`, fabricates a neutral investment decision or replaces previous leading evidence.

The CAS transport fetches exact current blob, merges only Sentinel-owned fields, retries non-force main conflicts at most three times, rejects an older run, preserves non-owned changes and verifies exact bytes plus commit ancestry on main. This transport is tested against real bare Git races but remains denied for server publication while the registry does not admit it. GitHub production write credentials and cross-platform Single Writer admission need separate empirical acceptance.

AXS lower-cost left-side research is separate from 1.40–1.42 second-wave and 1.454–1.46 strong-breakout price observations. A price zone alone is not a confirmed signal or an instruction to trade. No automatic real trading exists.

Remaining full-migration dependencies: a faithful callable version of the existing ChatGPT reasoning engine; timestamped ETF/macro alternative sources; cross-platform writer lease with acknowledged old-task behavior; verified notification relay; actual state persistence, failure injection and two complete natural scheduled cycles. Until then keep the original ChatGPT scheduler enabled and do not claim a complete migration.
