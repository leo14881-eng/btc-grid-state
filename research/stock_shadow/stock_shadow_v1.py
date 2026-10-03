#!/usr/bin/env python3
"""Independent Stock Shadow V1. Broad paper sampling only; never places orders."""
import json, math, urllib.request, concurrent.futures
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path("research/results/stock-shadow")
STATE=ROOT/"portfolio-v1.json"; EVENTS=ROOT/"trades-v1.json"; SUMMARY=ROOT/"summary-v1.json"
NOTIONAL=1000.0; MAX_TRANCHES=5
MIN_PRICE=2.0; MIN_DOLLAR_VOLUME=10_000_000.0; MIN_SCORE=68.0
MAX_5D_RETURN=18.0; MAX_20D_RETURN=45.0; MAX_SMA20_EXTENSION=18.0; MIN_20D_RETURN=-8.0
FEE_RATE=0.002          # conservative xStock spot-side research assumption; stored explicitly
ARM_NET_PCT=3.0
GIVEBACK_PCT=5.0
PROFIT_FLOOR_NET_PCT=0.5

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
MAX_MARKET_WORKERS = 24

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

def _stock_snapshot(symbol):
    # Daily bars provide liquidity, trend, momentum and relative-strength inputs.
    url=f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=3mo&interval=1d"
    d=get_json(url); r=d["chart"]["result"][0]
    q=r["indicators"]["quote"][0]
    closes=[float(x) if x is not None else None for x in q["close"]]
    volumes=[float(x or 0) for x in q["volume"]]
    valid=[(p,v) for p,v in zip(closes,volumes) if p is not None and p>0]
    if len(valid)<22: raise ValueError("insufficient_history")
    prices=[x[0] for x in valid]; vols=[x[1] for x in valid]
    price=prices[-1]
    ret5=(price/prices[-6]-1)*100
    ret20=(price/prices[-21]-1)*100
    sma20=sum(prices[-20:])/20
    avg_dollar_volume=sum(p*v for p,v in zip(prices[-20:],vols[-20:]))/20
    vol20=(sum(((prices[i]/prices[i-1]-1)*100)**2 for i in range(len(prices)-19,len(prices)))/19)**0.5
    return {"base":symbol,"price":price,"ret5":ret5,"ret20":ret20,"sma20":sma20,
            "avg_dollar_volume20":avg_dollar_volume,"daily_volatility20":vol20,
            "status":"OBSERVED","source":"FREE_PUBLIC_CHART_1D","observed_at":now()}

def score_candidate(m, spy=None, qqq=None):
    reasons=[]; rejects=[]
    price=m["price"]; dv=m["avg_dollar_volume20"]; r5=m["ret5"]; r20=m["ret20"]
    if price < MIN_PRICE: rejects.append("PRICE_TOO_LOW")
    if dv < MIN_DOLLAR_VOLUME: rejects.append("LOW_DOLLAR_VOLUME")
    if r5 > MAX_5D_RETURN: rejects.append("OVEREXTENDED_5D")
    if r20 > MAX_20D_RETURN: rejects.append("OVEREXTENDED_20D")
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
    if dist > MAX_SMA20_EXTENSION: rejects.append("TOO_FAR_ABOVE_SMA20")
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
    score,reasons,rejects,metrics=score_candidate(m,spy,qqq)
    ready=(not rejects and score>=MIN_SCORE and m["ret5"]>-3.0 and m["ret5"]<=MAX_5D_RETURN)
    structure="MOMENTUM_TREND" if ready and m["ret5"]>=0 else ("PULLBACK_IN_TREND" if ready else "NONE")
    return {"ready":ready,"score":score,"reasons":reasons,"rejects":rejects,"metrics":metrics,"entry_structure":structure}

def stock_universe():
    """Discover the full US common-stock universe, then observe symbols concurrently."""
    symbols, discovery_errors=discover_us_common_stocks()
    out={}; failed=[]
    def one(symbol):
        try: return symbol, _stock_snapshot(symbol), None
        except Exception as e: return symbol, None, type(e).__name__
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_MARKET_WORKERS) as ex:
        for symbol,snapshot,error in ex.map(one,symbols):
            if snapshot is not None: out[symbol]=snapshot
            else: failed.append({"symbol":symbol,"error":error})
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
    for s,m,d in candidates:
        if s not in state["positions"]:
            tr={"at":now(),"price":m["price"],"notional":NOTIONAL,"reason":"SELECTIVE_ENTRY_V1","score":d["score"],"entry_structure":d["entry_structure"],"selection_reasons":d["reasons"],"selection_metrics":d["metrics"],"snapshot":m}
            state["positions"][s]={"symbol":s,"opened_at":tr["at"],"tranches":[tr],"entry_score":d["score"],"entry_structure":d["entry_structure"],"mfe_net_pct":net_pct({"tranches":[tr]},m["price"]),"mae_net_pct":net_pct({"tranches":[tr]},m["price"])}
            events.append({"type":"BUY","symbol":s,**tr})
    for s,p in list(state["positions"].items()):
        m=market.get(s)
        if not m: continue
        price=m["price"]; r=net_pct(p,price)
        p["mfe_net_pct"]=max(p.get("mfe_net_pct",r),r); p["mae_net_pct"]=min(p.get("mae_net_pct",r),r)
        p.update({"last_price":price,"last_at":now(),"avg_price":avg(p),"net_pnl_usdt":round(net_pnl(p,price),6),"net_return_pct":round(r,6)})
        # ADD only when the thesis still passes selection and the pullback improves entry; never average mechanically.
        n=len(p["tranches"]); first=p["tranches"][0]["price"]; decision=entry_decision(m,bench.get("SPY"),bench.get("QQQ"))
        pullback=(price/first-1)*100
        if n<MAX_TRANCHES and decision["ready"] and pullback <= -3*n and m["price"]>=m["sma20"]*0.97:
            tr={"at":now(),"price":price,"notional":NOTIONAL,"reason":"THESIS_CONFIRMED_PULLBACK_ADD","score":decision["score"],"snapshot":m}
            p["tranches"].append(tr); events.append({"type":"ADD","symbol":s,**tr}); p["avg_price"]=avg(p)
        # Profit protection: only sells while still net profitable after an armed MFE.
        mfe=p.get("mfe_net_pct",r); giveback=mfe-r
        if mfe>=ARM_NET_PCT and r>0 and (giveback>=GIVEBACK_PCT or r<=PROFIT_FLOOR_NET_PCT):
            closed=dict(p); closed.update({"closed_at":now(),"exit_price":price,"exit_reason":"PROFIT_GIVEBACK","realized_net_pnl_usdt":round(net_pnl(p,price),6),"realized_net_return_pct":round(r,6),"profit_giveback_pct_points":round(giveback,6)})
            state["closed"].append(closed); del state["positions"][s]
            events.append({"type":"SELL","at":closed["closed_at"],"symbol":s,"price":price,"reason":"PROFIT_GIVEBACK","net_pnl_usdt":closed["realized_net_pnl_usdt"],"net_return_pct":closed["realized_net_return_pct"],"mfe_net_pct":mfe,"giveback_pct_points":giveback,"USER_ALERT_REQUIRED":True})
    wins=[x for x in state["closed"] if x.get("realized_net_pnl_usdt",0)>0]; losses=[x for x in state["closed"] if x.get("realized_net_pnl_usdt",0)<=0]
    realized=sum(x.get("realized_net_pnl_usdt",0) for x in state["closed"])
    state["updated_at"]=now(); state["simulation_only"]=True
    save(STATE,state); save(EVENTS,events)
    save(SUMMARY,{"updated_at":now(),"simulation_only":True,"universe_discovered":discovery["discovered"],"universe_seen":len(market),"market_data_status":data_status,"universe_source_errors":discovery["source_errors"],"failed_symbols":failed_symbols,"candidates_ready":len(candidates),"rejection_counts":rejection_counts,"selection_version":"SELECTIVE_ENTRY_V1","open_positions":len(state["positions"]),"closed_positions":len(state["closed"]),"wins":len(wins),"losses":len(losses),"realized_net_pnl_usdt":round(realized,6),"events":len(events),"fee_rate_per_side":FEE_RATE,"policy":{"max_open":None,"standard_tranche_usdt":NOTIONAL,"max_tranches":MAX_TRANCHES,"profit_arm_net_pct":ARM_NET_PCT,"profit_giveback_pct_points":GIVEBACK_PCT,"paid_api_required":False,"real_orders":False}})
    print(json.dumps(load(SUMMARY,{}),ensure_ascii=False))

if __name__=="__main__": main()
