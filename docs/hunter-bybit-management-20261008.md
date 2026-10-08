# Bybit existing-position lifecycle integration — October 8

Sentinel full migration was cancelled by the user; preserve its existing ChatGPT analysis and server evidence tasks.

The 5-minute authoritative Monitor now collects public Bybit Spot ticker, matching 1h/4h/micro signals with Bybit BTC benchmark, and a fresh 200-level orderbook only for explicit BYBIT_SPOT V2 holdings. No discovery scan, private API, order endpoint or second writer is introduced. Unknown fees, wrong venue/symbol/category, stale/duplicate/old observations, missing BTC evidence or incomplete held-quantity depth fail closed before marks/health/lifecycle change. Binance processing continues independently. Raw matching Bybit depth, signal identity and generation are retained inside the existing portfolio persistence path.

Existing protection and recovery rules are reused. New high holds, positive net giveback can generate shadow PROFIT_PROTECTION SELL, negative execution gaps remain held with GAPPED_THROUGH_PROTECTION_WINDOW. All Bybit exits require a matching full-quantity estimate; fee-aware accounting does not mechanically force a price/time exit. The 5-minute layer never executes Bybit ADD or creates BUY. Hourly Bybit new-entry/ADD capital admission remains separately unsupported and was not unlocked by this patch. No BUY filters or selection parameters changed.

Bybit holdings and closed positions cannot use Binance historical bars or post-exit marks. Venue-matched historical reconstruction/post-exit monitoring remains explicitly UNKNOWN instead of fabricating a path. Full Bybit new-entry and historical outcome support is NOT claimed complete.

Tests: 609 previous full Hunter tests passed; 12 new integration tests cover matching primary marks, ARM, higher peak/hold, profitable giveback/one SELL, negative gap/no fake positive exit, duplicate/old generations, per-symbol failure, independent Binance management, stale/wrong klines, historical venue isolation, and Monitor persistence/restart. Final full suite621 tests passed in31.003s, with final timestamp quality annotation checked by12 integration tests. No real fills or trading activity are claimed by fixtures.

Server read-only public connectivity was actually verified2026-10-08T01:14:15.979340Z: Bybit BTCUSDT Spot orderbook retCode0,200 bids and200 asks, source_timestamp1791422055753, real_order_count0. This is connectivity evidence, not a true primary Bybit held-position sample or deployed lifecycle test. Current real V2 holdings are Binance. After merge, source/runtime/natural-cycle acceptance and real Bybit-position evidence must be reported separately.

SHADOW_ONLY; NONE_SHADOW_ONLY; real_trading_enabled=false; no real order APIs; capital pool20000/ordinary17000/reserve3000 unchanged. Single Writer, CAS/readback,5-minute Monitor and hourly Research remain intact.
