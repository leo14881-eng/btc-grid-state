#!/usr/bin/env python3
"""Independent Stock Shadow V1. Broad paper sampling only; never places orders."""
import json, math, urllib.request, concurrent.futures
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path("research/results/stock-shadow")
STATE=ROOT/"portfolio-v1.json"; EVENTS=ROOT/"trades-v1.json"; SUMMARY=ROOT/"summary-v1.json"
NOTIONAL=1000.0; MAX_TRANCHES=5
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
EXCLUDED_NAME_MARKERS = (" ETF", " ETN", " WARRANT", " WTS", " UNIT", " RIGHTS", " PREFERRED", " PFD", " DEPOSITARY", " DEPOSITORY")
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
            if symbol and row.get("Test Issue")=="N" and row.get("ETF")=="N" and _plain_common_stock(name):
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
            if symbol and row.get("Test Issue")=="N" and row.get("ETF")=="N" and _plain_common_stock(name):
                symbols.add(symbol)
    except Exception as e:
        source_errors.append({"source":"OTHER_LISTED","error":type(e).__name__})
    # Yahoo uses '-' for class shares; Nasdaq directories commonly use '.'.
    return sorted(s.replace(".","-") for s in symbols), source_errors

def _stock_snapshot(symbol):
    url=f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=5d&interval=5m"
    d=get_json(url); r=d["chart"]["result"][0]
    closes=[x for x in r["indicators"]["quote"][0]["close"] if x is not None]
    if not closes: raise ValueError("no close")
    price=float(closes[-1]); prev=float(r.get("meta",{}).get("chartPreviousClose") or (closes[-2] if len(closes)>1 else price))
    if price<=0 or not math.isfinite(price): raise ValueError("bad price")
    return {"base":symbol,"price":price,"change24h":((price/prev)-1)*100 if prev else 0,
            "volume24h":None,"status":"OBSERVED","source":"FREE_PUBLIC_CHART_5M","observed_at":now()}

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
    data_status="OK" if market else "UNKNOWN:ALL_STOCK_SOURCES_FAILED"
    # V1 broad net: every valid discovered stock gets a standardized first paper tranche. No MAX_OPEN.
    for s,m in market.items():
        if s not in state["positions"]:
            tr={"at":now(),"price":m["price"],"notional":NOTIONAL,"reason":"BROAD_OBSERVATION_ENTRY","snapshot":m}
            state["positions"][s]={"symbol":s,"opened_at":tr["at"],"tranches":[tr],"mfe_net_pct":net_pct({"tranches":[tr]},m["price"]),"mae_net_pct":net_pct({"tranches":[tr]},m["price"])}
            events.append({"type":"BUY","symbol":s,**tr})
    for s,p in list(state["positions"].items()):
        m=market.get(s)
        if not m: continue
        price=m["price"]; r=net_pct(p,price)
        p["mfe_net_pct"]=max(p.get("mfe_net_pct",r),r); p["mae_net_pct"]=min(p.get("mae_net_pct",r),r)
        p.update({"last_price":price,"last_at":now(),"avg_price":avg(p),"net_pnl_usdt":round(net_pnl(p,price),6),"net_return_pct":round(r,6)})
        # Broad averaging experiment: fixed observations, not a claim that averaging is optimal.
        n=len(p["tranches"]); first=p["tranches"][0]["price"]
        if n<MAX_TRANCHES and (price/first-1)*100 <= -4*n:
            tr={"at":now(),"price":price,"notional":NOTIONAL,"reason":"DIP_ADD_EXPERIMENT","snapshot":m}
            p["tranches"].append(tr); events.append({"type":"ADD","symbol":s,**tr})
            p["avg_price"]=avg(p)
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
    save(SUMMARY,{"updated_at":now(),"simulation_only":True,"universe_discovered":discovery["discovered"],"universe_seen":len(market),"market_data_status":data_status,"universe_source_errors":discovery["source_errors"],"failed_symbols":failed_symbols,"open_positions":len(state["positions"]),"closed_positions":len(state["closed"]),"wins":len(wins),"losses":len(losses),"realized_net_pnl_usdt":round(realized,6),"events":len(events),"fee_rate_per_side":FEE_RATE,"policy":{"max_open":None,"standard_tranche_usdt":NOTIONAL,"max_tranches":MAX_TRANCHES,"profit_arm_net_pct":ARM_NET_PCT,"profit_giveback_pct_points":GIVEBACK_PCT,"paid_api_required":False,"real_orders":False}})
    print(json.dumps(load(SUMMARY,{}),ensure_ascii=False))

if __name__=="__main__": main()
