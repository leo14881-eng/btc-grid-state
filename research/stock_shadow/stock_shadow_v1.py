#!/usr/bin/env python3
"""Independent Stock Shadow V2 clean-sample final run. Broad paper sampling only; never places orders."""
import json, math, os, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT=Path("research/results/stock-shadow")
STATE=ROOT/"portfolio-v1.json"; EVENTS=ROOT/"trades-v1.json"; SUMMARY=ROOT/"summary-v1.json"
NOTIONAL=1000.0; MAX_TRANCHES=5
LOW_PRICE_REFERENCE=2.0; MIN_DOLLAR_VOLUME=10_000_000.0; LOW_PRICE_MIN_DOLLAR_VOLUME=25_000_000.0; MIN_SCORE=68.0
MAX_5D_RETURN=18.0; MAX_20D_RETURN=45.0; MAX_SMA20_EXTENSION=18.0; MIN_20D_RETURN=-8.0
PARABOLIC_5D_RETURN=35.0; PARABOLIC_20D_RETURN=80.0; PARABOLIC_SMA20_EXTENSION=30.0
EARLY_MIN_SCORE=62.0; EARLY_MAX_20D_RETURN=18.0; EARLY_MAX_SMA20_EXTENSION=8.0
FEE_RATE=0.002          # conservative xStock spot-side research assumption; stored explicitly
ARM_NET_PCT=1.0
PROFIT_FLOOR_NET_PCT=0.10
# Swing book: once a trade has meaningful net profit, protect a positive net exit.
# Allowed giveback shrinks as MFE grows; all values are AFTER buy/sell fees.
PROFIT_GIVEBACK_BANDS=((30.0,0.20),(15.0,0.25),(8.0,0.35),(1.0,0.50))

def now(): return datetime.now(timezone.utc).isoformat()
def get_json(url):
    req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0 stock-shadow-research","Accept":"application/json"})
    with urllib.request.urlopen(req,timeout=25) as r: return json.load(r)

def first_json(urls):
    last=None
    for url in urls:
        try: return get_json(url), url
        except Exception as e: last=e
    raise last
def load(p,d):
    try: return json.loads(p.read_text()) if p.exists() else d
    except Exception: return d
def save(p,o):
    p.parent.mkdir(parents=True,exist_ok=True); p.write_text(json.dumps(o,ensure_ascii=False,indent=2,sort_keys=True)+"\n")

NASDAQ_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
EXCLUDED_NAME_MARKERS = (" ETF", " ETN", " WARRANT", " WTS", " UNIT", " RIGHT", " PREFERRED", " PFD", " DEPOSITARY", " DEPOSITORY")
ALPACA_BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
ALPACA_BATCH_SIZE = 200  # batch daily bars; SIP end is delayed outside real-time entitlement window

def get_text(url):
    req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0 stock-shadow-research","Accept":"text/plain,*/*"})
    with urllib.request.urlopen(req,timeout=25) as r: return r.read().decode("utf-8","replace")

def _plain_common_stock(name):
    u=(" "+name.upper()+" ")
    return not any(marker in u for marker in EXCLUDED_NAME_MARKERS)

def discover_us_common_stocks():
    """Discover US-listed common stocks from Nasdaq Trader symbol directories.

    Includes Nasdaq plus NYSE/NYSE American and other US exchange listings.
    Excludes ETFs/test issues and obvious non-common-stock security types.
    """
    symbols=set(); source_errors=[]
    try:
        lines=get_text(NASDAQ_LISTED).splitlines()
        header=lines[0].split("|")
        for line in lines[1:]:
            if not line or line.startswith("File Creation Time"): continue
            row=dict(zip(header,line.split("|")))
            symbol=row.get("Symbol","").strip()
            name=row.get("Security Name","").strip()
            if symbol and "$" not in symbol and row.get("Test Issue")=="N" and row.get("ETF")=="N" and _plain_common_stock(name):
                symbols.add(symbol)
    except Exception as e:
        source_errors.append({"source":"NASDAQ_LISTED","error":type(e).__name__})
    try:
        lines=get_text(OTHER_LISTED).splitlines()
        header=lines[0].split("|")
        for line in lines[1:]:
            if not line or line.startswith("File Creation Time"): continue
            row=dict(zip(header,line.split("|")))
            symbol=(row.get("ACT Symbol") or row.get("NASDAQ Symbol") or "").strip()
            name=row.get("Security Name","").strip()
            if symbol and "$" not in symbol and row.get("Test Issue")=="N" and row.get("ETF")=="N" and _plain_common_stock(name):
                symbols.add(symbol)
    except Exception as e:
        source_errors.append({"source":"OTHER_LISTED","error":type(e).__name__})
    # Yahoo uses '-' for class shares; Nasdaq directories commonly use '.'.
    return sorted(s.replace(".","-") for s in symbols), source_errors

def _snapshot_from_bars(symbol, bars, source):
    rows=[b for b in bars if b.get("c") is not None and float(b["c"])>0]
    if len(rows)<22: raise ValueError("insufficient_history")
    prices=[float(b["c"]) for b in rows]; vols=[float(b.get("v") or 0) for b in rows]
    highs=[float(b["h"]) for b in rows if b.get("h") is not None]
    lows=[float(b["l"]) for b in rows if b.get("l") is not None]
    price=prices[-1]; ret5=(price/prices[-6]-1)*100; ret20=(price/prices[-21]-1)*100
    sma20=sum(prices[-20:])/20
    avg_dollar_volume=sum(p*v for p,v in zip(prices[-20:],vols[-20:]))/20
    vol20=(sum(((prices[i]/prices[i-1]-1)*100)**2 for i in range(len(prices)-19,len(prices)))/19)**0.5
    avg_volume20=sum(vols[-20:])/20
    volume_ratio=(vols[-1]/avg_volume20) if avg_volume20 else 0.0
    high20=max(highs[-20:]) if highs else max(prices[-20:])
    low20=min(lows[-20:]) if lows else min(prices[-20:])
    range_pos20=((price-low20)/(high20-low20)) if high20>low20 else 0.5
    return {"base":symbol,"price":price,"ret5":ret5,"ret20":ret20,"sma20":sma20,
            "avg_dollar_volume20":avg_dollar_volume,"daily_volatility20":vol20,
            "volume_ratio20":volume_ratio,"high20":high20,"low20":low20,"range_position20":range_pos20,
            "status":"OBSERVED","source":source,"observed_at":now()}

def _stock_snapshot(symbol):
    # Compatibility/fallback helper for benchmarks only. Full universe uses Alpaca batch bars.
    url=f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=3mo&interval=1d"
    d=get_json(url); r=d["chart"]["result"][0]; q=r["indicators"]["quote"][0]
    bars=[{"c":p,"v":v,"h":h,"l":l} for p,v,h,l in zip(q["close"],q["volume"],q.get("high",[]),q.get("low",[])) if p is not None]
    return _snapshot_from_bars(symbol,bars,"FREE_PUBLIC_CHART_1D")

def _alpaca_batch_bars(symbols):
    key=os.getenv("APCA_API_KEY_ID"); secret=os.getenv("APCA_API_SECRET_KEY")
    if not key or not secret: raise RuntimeError("missing_alpaca_secrets")
    api_symbols=[s.replace("-",".") for s in symbols]
    end=datetime.now(timezone.utc)-timedelta(minutes=20); start=end-timedelta(days=110)
    params={"symbols":",".join(api_symbols),"timeframe":"1Day","start":start.isoformat().replace("+00:00","Z"),
            "end":end.isoformat().replace("+00:00","Z"),"limit":10000,"feed":"sip","adjustment":"all"}
    headers={"APCA-API-KEY-ID":key,"APCA-API-SECRET-KEY":secret,"User-Agent":"stock-shadow/3.0"}
    merged={}; token=None
    while True:
        if token: params["page_token"]=token
        url=ALPACA_BARS_URL+"?"+urllib.parse.urlencode(params)
        req=urllib.request.Request(url,headers=headers)
        with urllib.request.urlopen(req,timeout=45) as resp: body=json.load(resp)
        for sym, rows in (body.get("bars") or {}).items(): merged.setdefault(sym.replace(".","-"),[]).extend(rows)
        token=body.get("next_page_token")
        if not token: break
    return merged

def score_candidate(m, spy=None, qqq=None):
    reasons=[]; rejects=[]
    price=m["price"]; dv=m["avg_dollar_volume20"]; r5=m["ret5"]; r20=m["ret20"]
    # V2 tradeability: price alone is not a rejection. Low-priced names must prove stronger liquidity.
    if price < LOW_PRICE_REFERENCE and dv < LOW_PRICE_MIN_DOLLAR_VOLUME: rejects.append("LOW_PRICE_INSUFFICIENT_LIQUIDITY")
    if dv < MIN_DOLLAR_VOLUME: rejects.append("LOW_DOLLAR_VOLUME")
    if r5 > PARABOLIC_5D_RETURN: rejects.append("PARABOLIC_5D")
    if r20 > PARABOLIC_20D_RETURN: rejects.append("PARABOLIC_20D")
    if r20 < MIN_20D_RETURN: rejects.append("WEAK_20D_TREND")
    if price < m["sma20"]*0.94: rejects.append("BELOW_TREND")
    score=0.0
    # Liquidity 0..15
    score += min(15.0, max(0.0, 5.0 + math.log10(max(dv,1)/MIN_DOLLAR_VOLUME)*5.0))
    # Trend 0..20 and momentum 0..20; reward strength without rewarding extreme chase.
    trend=max(0.0,min(20.0,10.0+r20*0.45)); momentum=max(0.0,min(20.0,10.0+r5*0.8))
    score += trend + momentum
    # Relative strength to SPY/QQQ, 0..25.
    spy20=spy["ret20"] if spy else 0.0; qqq20=qqq["ret20"] if qqq else 0.0
    rel=((r20-spy20)+(r20-qqq20))/2
    score += max(0.0,min(25.0,12.5+rel*0.8))
    # Price vs SMA20 structure, 0..10.
    dist=(price/m["sma20"]-1)*100
    if dist > PARABOLIC_SMA20_EXTENSION: rejects.append("PARABOLIC_SMA20_EXTENSION")
    score += max(0.0,min(10.0,7.0+dist*0.35))
    # Volatility quality, 0..10: enough movement, but penalize extreme noise.
    v=m["daily_volatility20"]
    score += 10.0 if 1.0<=v<=4.5 else (6.0 if v<=7.0 else 1.0)
    if r5>=0 and r20>0: reasons.append("POSITIVE_MOMENTUM")
    if rel>0: reasons.append("OUTPERFORMS_SPY_QQQ")
    if price>=m["sma20"]: reasons.append("ABOVE_SMA20")
    if dv>=MIN_DOLLAR_VOLUME: reasons.append("LIQUID")
    return round(score,2), reasons, rejects, {"relative20":round(rel,4),"distance_sma20_pct":round(dist,4)}

def entry_decision(m, spy=None, qqq=None):
    """One engine, multiple entry structures: early anomaly + right-side trend + healthy pullback."""
    score,reasons,rejects,metrics=score_candidate(m,spy,qqq)
    r5=m["ret5"]; r20=m["ret20"]; price=m["price"]; sma=m["sma20"]
    dist=(price/sma-1)*100
    vr=m.get("volume_ratio20",1.0); rp=m.get("range_position20",0.5)
    spy20=spy["ret20"] if spy else 0.0; qqq20=qqq["ret20"] if qqq else 0.0
    rel=((r20-spy20)+(r20-qqq20))/2
    metrics.update({"volume_ratio20":round(vr,4),"range_position20":round(rp,4)})
    structure="NONE"
    # Left-side / early anomaly: not extended, improving relative strength, abnormal participation,
    # and already stabilised around/above trend. It does NOT require a breakout.
    early=(not rejects and score>=EARLY_MIN_SCORE and -4.0<=r5<=8.0 and
           -3.0<=r20<=EARLY_MAX_20D_RETURN and -3.0<=dist<=EARLY_MAX_SMA20_EXTENSION and
           vr>=1.35 and rel>=0 and rp>=0.45)
    # Right-side trend: confirmed strength, but still within a non-parabolic chase envelope.
    momentum=(not rejects and score>=MIN_SCORE and 0<=r5<=MAX_5D_RETURN and
              r20<=MAX_20D_RETURN and dist<=MAX_SMA20_EXTENSION)
    # Healthy pullback inside an established trend.
    pullback=(not rejects and score>=MIN_SCORE and -3.0<r5<0 and r20>0 and
              -3.0<=dist<=12.0 and rel>0)
    if early: structure="EARLY_ACCUMULATION"
    elif pullback: structure="PULLBACK_IN_TREND"
    elif momentum: structure="MOMENTUM_TREND"
    ready=structure!="NONE"
    if early: reasons.append("EARLY_VOLUME_ANOMALY")
    if ready and structure!="EARLY_ACCUMULATION" and r5>MAX_5D_RETURN*0.75: reasons.append("LATE_STAGE_CAUTION")
    return {"ready":ready,"score":score,"reasons":reasons,"rejects":rejects,"metrics":metrics,"entry_structure":structure}


def profit_floor_net_pct(mfe):
    """Dynamic positive net-profit floor. Returns None until protection is armed."""
    if mfe < ARM_NET_PCT: return None
    for threshold, giveback_fraction in PROFIT_GIVEBACK_BANDS:
        if mfe >= threshold:
            return max(PROFIT_FLOOR_NET_PCT, mfe*(1.0-giveback_fraction))
    return None

def recovery_add_signal(p, m, ps):
    """ADD only after a real pullback has happened and the position is improving again."""
    price=m["price"]
    high=max(float(p.get("swing_high_price",price)),price)
    p["swing_high_price"]=high
    dd=(price/high-1.0)*100 if high else 0.0
    if dd < -1.0:
        low=min(float(p.get("pullback_low_price",price)),price)
        p["pullback_low_price"]=low
        p["pullback_seen"]=True
    low=p.get("pullback_low_price")
    recovery=((price/low-1.0)*100) if low else 0.0
    prev_rel=(p.get("position_state_v2") or {}).get("market_relative20")
    rel_improving=prev_rel is not None and ps["market_relative20"] > prev_rel
    price_improving=bool(low and price > low)
    eligible=bool(p.get("pullback_seen") and ps["state"]!="BROKEN" and price_improving and rel_improving)
    return {"eligible":eligible,"drawdown_from_swing_high_pct":round(dd,4),
            "recovery_from_pullback_low_pct":round(recovery,4),"relative_improving":rel_improving,
            "price_improving":price_improving}

def position_state_v2(m, spy=None, qqq=None):
    """Small, explainable state engine. No single indicator can mark a position BROKEN."""
    price=m["price"]; sma=m["sma20"]; r5=m["ret5"]; r20=m["ret20"]
    spy20=spy["ret20"] if spy else 0.0; qqq20=qqq["ret20"] if qqq else 0.0
    relative20=((r20-spy20)+(r20-qqq20))/2
    dist=(price/sma-1)*100
    vr=m.get("volume_ratio20",1.0)
    trend_broken=(dist < -6.0 and r20 < 0)
    relative_weak=(relative20 < -5.0)
    selling_pressure=(r5 < -5.0 and vr >= 1.5)
    trend_strong=(dist >= 0 and r20 > 0)
    relative_strong=(relative20 >= 0)
    # BROKEN requires independent confirmation: structure + relative weakness.
    if trend_broken and relative_weak:
        state="BROKEN"
    elif r5 < 0 and not trend_broken and not relative_weak:
        state="HEALTHY_PULLBACK"
    elif trend_strong and relative_strong:
        state="STRONG"
    else:
        state="UNCERTAIN"
    return {"state":state,"market_relative20":round(relative20,4),"distance_sma20_pct":round(dist,4),
            "selling_pressure":selling_pressure,"trend_broken":trend_broken,"relative_weak":relative_weak,
            "sector_relative_status":"UNAVAILABLE_V2_BASELINE"}

def position_state_v3(p, market_state, net_return_pct):
    """Position-relative state; market strength and our trade outcome are separate dimensions."""
    mae=float(p.get("mae_net_pct",net_return_pct))
    if market_state.get("state")=="BROKEN":
        state="BROKEN"
    elif net_return_pct <= -8.0 or mae <= -10.0:
        state="DETERIORATING"
    elif net_return_pct <= -3.0:
        state="UNDERWATER"
    elif net_return_pct > 0:
        state="PROFITABLE"
    else:
        state="HEALTHY_PULLBACK"
    return {"state":state,"net_return_pct":round(net_return_pct,6),"mae_net_pct":round(mae,6),
            "market_state":market_state.get("state")}

def stock_universe():
    """Discover full US common-stock universe and fetch daily bars in Alpaca batches."""
    symbols, discovery_errors=discover_us_common_stocks()
    out={}; failed=[]
    for i in range(0,len(symbols),ALPACA_BATCH_SIZE):
        batch=symbols[i:i+ALPACA_BATCH_SIZE]
        try:
            bars_by_symbol=_alpaca_batch_bars(batch)
        except urllib.error.HTTPError as e:
            err={"type":"HTTPError","status":e.code,"reason":str(e.reason),"source":"ALPACA_BATCH"}
            failed.extend({"symbol":s,"error":err} for s in batch); continue
        except Exception as e:
            err={"type":type(e).__name__,"message":str(e)[:160],"source":"ALPACA_BATCH"}
            failed.extend({"symbol":s,"error":err} for s in batch); continue
        for s in batch:
            rows=bars_by_symbol.get(s,[])
            try: out[s]=_snapshot_from_bars(s,rows,"ALPACA_SIP_BATCH_1D")
            except Exception as e: failed.append({"symbol":s,"error":{"type":type(e).__name__,"message":str(e)[:160],"source":"ALPACA_SIP_BATCH"}})
    return out, failed, {"discovered":len(symbols),"source_errors":discovery_errors}


def avg(p):
    q=sum(t["notional"]/t["price"] for t in p["tranches"]); c=sum(t["notional"] for t in p["tranches"])
    return c/q if q else 0
def qty(p): return sum(t["notional"]/t["price"] for t in p["tranches"])
def net_pnl(p,price):
    c=sum(t["notional"] for t in p["tranches"]); proceeds=qty(p)*price
    fees=c*FEE_RATE+proceeds*FEE_RATE
    return proceeds-c-fees
def net_pct(p,price):
    c=sum(t["notional"] for t in p["tranches"])
    return net_pnl(p,price)/c*100 if c else 0

def main():
    state=load(STATE,{"version":2,"simulation_only":True,"positions":{},"closed":[]})
    events=load(EVENTS,[])
    market, failed_symbols, discovery=stock_universe()
    bench={}
    for idx in ("SPY","QQQ"):
        try: bench[idx]=_stock_snapshot(idx)
        except Exception: pass
    candidates=[]; rejection_counts={}
    for s,m in market.items():
        d=entry_decision(m,bench.get("SPY"),bench.get("QQQ"))
        m["selection"]=d
        if d["ready"]: candidates.append((s,m,d))
        else:
            for reason in d["rejects"] or ["SCORE_OR_ENTRY_NOT_READY"]:
                rejection_counts[reason]=rejection_counts.get(reason,0)+1
    candidates.sort(key=lambda x:x[2]["score"],reverse=True)
    data_status=("OK" if market and not failed_symbols else ("PARTIAL" if market else "UNKNOWN:ALL_STOCK_SOURCES_FAILED"))
    # Selective V1: scan the whole market, but BUY only candidates that pass every gate.
    newly_opened=set()
    for s,m,d in candidates:
        if s not in state["positions"]:
            tr={"at":now(),"price":m["price"],"notional":NOTIONAL,"reason":"SELECTIVE_ENTRY_V1","score":d["score"],"entry_structure":d["entry_structure"],"selection_reasons":d["reasons"],"selection_metrics":d["metrics"],"snapshot":m}
            state["positions"][s]={"symbol":s,"opened_at":tr["at"],"tranches":[tr],"entry_score":d["score"],"entry_structure":d["entry_structure"],"mfe_net_pct":net_pct({"tranches":[tr]},m["price"]),"mae_net_pct":net_pct({"tranches":[tr]},m["price"])}
            events.append({"type":"BUY","symbol":s,**tr}); newly_opened.add(s)
    for s,p in list(state["positions"].items()):
        m=market.get(s)
        if not m: continue
        price=m["price"]; r=net_pct(p,price)
        p["mfe_net_pct"]=max(p.get("mfe_net_pct",r),r); p["mae_net_pct"]=min(p.get("mae_net_pct",r),r)
        p.update({"last_price":price,"last_at":now(),"avg_price":avg(p),"net_pnl_usdt":round(net_pnl(p,price),6),"net_return_pct":round(r,6)})
        # V3 lifecycle: worsening never ADDs; ADD waits for pullback + observable recovery.
        n=len(p["tranches"]); decision=entry_decision(m,bench.get("SPY"),bench.get("QQQ"))
        ps=position_state_v2(m,bench.get("SPY"),bench.get("QQQ"))
        recovery=recovery_add_signal(p,m,ps)
        p["recovery_add_signal"]=recovery
        p["market_state_v3"]=ps
        p["position_state_v3"]=position_state_v3(p,ps,r)
        p["position_state_v2"]=ps  # compatibility for existing forward-sample records
        mfe=p.get("mfe_net_pct",r); giveback=mfe-r
        floor=profit_floor_net_pct(mfe)
        p["profit_protection_floor_net_pct"]=round(floor,6) if floor is not None else None
        p["profit_protection_signal"]=bool(floor is not None and r<=floor)
        if s not in newly_opened and n<MAX_TRANCHES and recovery["eligible"] and decision["ready"]:
            tr={"at":now(),"price":price,"notional":NOTIONAL,"reason":"PULLBACK_RECOVERY_ADD_V3","score":decision["score"],"position_state":ps,"recovery_signal":recovery,"snapshot":m}
            p["tranches"].append(tr); events.append({"type":"ADD","symbol":s,**tr}); p["avg_price"]=avg(p)
            p["pullback_seen"]=False; p["pullback_low_price"]=None
        # Intraday profit-protection SELL belongs exclusively to the 5m monitor.
        # This full scan uses daily bars that may be stale intraday/weekends.
        exit_reason=None
        if ps["state"]=="BROKEN":
            if r > 0:
                exit_reason="STRUCTURE_BROKEN_PROFIT_EXIT_V3"
                p.pop("rebound_exit_pending_v3",None)
            else:
                # Hard rule: never realize a loss into a breakdown. Wait for a rebound.
                p["rebound_exit_pending_v3"]={"armed_at":p.get("rebound_exit_pending_v3",{}).get("armed_at",now()),
                    "reason":"FUNDAMENTAL_OR_STRUCTURE_DETERIORATION","lowest_net_return_pct":round(min(r,(p.get("rebound_exit_pending_v3") or {}).get("lowest_net_return_pct",r)),6)}
        elif p.get("rebound_exit_pending_v3"):
            # Rebound exits are profit-only too. A losing rebound remains pending.
            if r > 0:
                exit_reason="REBOUND_PROFIT_EXIT_AFTER_DETERIORATION_V3"
        if exit_reason:
            final_pnl=net_pnl(p,price); final_r=net_pct(p,price)
            closed=dict(p); closed.update({"closed_at":now(),"exit_price":price,"exit_reason":exit_reason,
                "realized_net_pnl_usdt":round(final_pnl,6),"realized_net_return_pct":round(final_r,6),
                "gross_price_return_pct":round((price/avg(p)-1)*100,6),"estimated_total_fees_usdt":round((sum(t["notional"] for t in p["tranches"])*FEE_RATE)+(qty(p)*price*FEE_RATE),6),
                "profit_giveback_pct_points":round(giveback,6),"post_exit_tracking_due_days":[1,3,5,10]})
            state["closed"].append(closed); del state["positions"][s]
            events.append({"type":"SELL","at":closed["closed_at"],"symbol":s,"price":price,"reason":exit_reason,
                "net_pnl_usdt":closed["realized_net_pnl_usdt"],"net_return_pct":closed["realized_net_return_pct"],
                "gross_price_return_pct":closed["gross_price_return_pct"],"estimated_total_fees_usdt":closed["estimated_total_fees_usdt"],
                "position_state":ps,"USER_ALERT_REQUIRED":True})
    wins=[x for x in state["closed"] if x.get("realized_net_pnl_usdt",0)>0]; losses=[x for x in state["closed"] if x.get("realized_net_pnl_usdt",0)<=0]
    realized=sum(x.get("realized_net_pnl_usdt",0) for x in state["closed"])
    state["updated_at"]=now(); state["simulation_only"]=True
    save(STATE,state); save(EVENTS,events)
    save(SUMMARY,{"updated_at":now(),"simulation_only":True,"universe_discovered":discovery["discovered"],"universe_seen":len(market),"market_data_status":data_status,"universe_source_errors":discovery["source_errors"],"failed_symbols":failed_symbols,"candidates_ready":len(candidates),"rejection_counts":rejection_counts,"selection_version":"HYBRID_ENTRY_V1_POSITION_STATE_V3","open_positions":len(state["positions"]),"closed_positions":len(state["closed"]),"wins":len(wins),"losses":len(losses),"realized_net_pnl_usdt":round(realized,6),"events":len(events),"fee_rate_per_side":FEE_RATE,"policy":{"max_open":None,"standard_tranche_usdt":NOTIONAL,"max_tranches":MAX_TRANCHES,"profit_arm_net_pct":ARM_NET_PCT,"profit_floor_min_net_pct":PROFIT_FLOOR_NET_PCT,"profit_giveback_bands":PROFIT_GIVEBACK_BANDS,"paid_api_required":False,"real_orders":False}})
    print(json.dumps(load(SUMMARY,{}),ensure_ascii=False))

if __name__=="__main__": main()
