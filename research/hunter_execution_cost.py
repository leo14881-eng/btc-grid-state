#!/usr/bin/env python3
"""Research-only executable cost simulation from a fresh Binance spot book."""
import math

def estimate(book, quote_usdt, fee_bps=10.0, target_net_usdt=150.0, stop_pct=0.025):
    """Simulate market buy and immediate mark-to-bid exit; no execution authority."""
    if not 0 < quote_usdt <= 20000 or not 0 <= fee_bps <= 100 or not 0 < stop_pct < 1:
        raise ValueError("INVALID_PARAMETERS")
    bids = sorted(((float(p), float(q)) for p,q in book["bids"]), reverse=True)
    asks = sorted(((float(p), float(q)) for p,q in book["asks"]))
    if not bids or not asks or bids[0][0] <= 0 or asks[0][0] <= bids[0][0]:
        raise ValueError("INVALID_BOOK")
    if any(not math.isfinite(p*q) or p <= 0 or q < 0 for p,q in bids+asks):
        raise ValueError("INVALID_LEVEL")
    mid = (bids[0][0]+asks[0][0])/2
    remaining = quote_usdt; coins = 0.
    for p,q in asks:
        spent = min(remaining,p*q)
        coins += spent/p
        remaining -= spent
        if remaining < 1e-7: break
    if remaining >= 1e-7: raise ValueError("INSUFFICIENT_ASK_DEPTH")
    remaining_coins = coins; exit_quote = 0.
    for p,q in bids:
        sold = min(remaining_coins,q)
        exit_quote += sold*p
        remaining_coins -= sold
        if remaining_coins < 1e-10: break
    if remaining_coins >= 1e-10: raise ValueError("INSUFFICIENT_BID_DEPTH")
    buy_avg=quote_usdt/coins
    exit_avg=exit_quote/coins
    fees=(quote_usdt+exit_quote)*fee_bps/10000
    roundtrip_cost=quote_usdt-exit_quote+fees
    # Conservative target: future sell execution retains current exit-side impact in bps.
    exit_impact=(mid-exit_avg)/mid
    target_gross_sell=quote_usdt+target_net_usdt+quote_usdt*fee_bps/10000
    required_mid=target_gross_sell/(coins*(1-exit_impact)*(1-fee_bps/10000))
    stop_mid=buy_avg*(1-stop_pct)
    stop_sell=coins*stop_mid*(1-exit_impact)
    stop_loss=quote_usdt-stop_sell+(quote_usdt+stop_sell)*fee_bps/10000
    return dict(quote_usdt=quote_usdt, fee_bps=fee_bps, mid=round(mid,9),
        buy_avg=round(buy_avg,9), exit_avg=round(exit_avg,9),
        buy_slippage_bps=round((buy_avg/mid-1)*10000,3),
        exit_slippage_bps=round(exit_impact*10000,3),
        spread_bps=round((asks[0][0]-bids[0][0])/mid*10000,3),
        roundtrip_cost_usdt=round(roundtrip_cost,2),
        target_net_usdt=target_net_usdt, required_future_mid=round(required_mid,9),
        required_mid_gain_pct=round((required_mid/mid-1)*100,3),
        stop_mid=round(stop_mid,9), estimated_stop_loss_usdt=round(stop_loss,2),
        estimated_rr=round(target_net_usdt/stop_loss,3) if stop_loss>0 else None,
        caveat="Static snapshot; orderbook, fees and fills can change; research only.")
