#!/usr/bin/env python3
"""Stock Shadow V1: broad-sampling xStock paper-trading ledger. No real orders."""
import json, os, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path("research/results/stock-shadow")
STATE=ROOT/"portfolio-v1.json"
EVENTS=ROOT/"trades-v1.json"
SUMMARY=ROOT/"summary-v1.json"
NOTIONAL=1000.0
MAX_TRANCHES=5

def now(): return datetime.now(timezone.utc).isoformat()

def get_json(url):
    req=urllib.request.Request(url,headers={"User-Agent":"stock-shadow-v1/1.0"})
    with urllib.request.urlopen(req,timeout=20) as r: return json.load(r)

def bybit_tickers():
    d=get_json("https://api.bybit.com/v5/market/tickers?category=spot")
    out={}
    for x in d.get("result",{}).get("list",[]):
        s=x.get("symbol","")
        if s.endswith("USDT") and ("X" in s[:-4] or s[:-4] in {"MU","SNDK"}):
            try: out[s]={"price":float(x["lastPrice"]),"change24h":float(x.get("price24hPcnt") or 0)*100}
            except: pass
    return out

def load(path,default):
    if path.exists():
        try: return json.loads(path.read_text())
        except: pass
    return default

def save(path,obj):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(obj,ensure_ascii=False,indent=2,sort_keys=True)+"\n")

def avg(p):
    ts=p["tranches"]; q=sum(t["notional"]/t["price"] for t in ts)
    return sum(t["notional"] for t in ts)/q if q else 0

def pnl(p,price):
    q=sum(t["notional"]/t["price"] for t in p["tranches"])
    cost=sum(t["notional"] for t in p["tranches"])
    return q*price-cost

def main():
    ROOT.mkdir(parents=True,exist_ok=True)
    state=load(STATE,{"version":1,"simulation_only":True,"positions":{},"closed":[]})
    events=load(EVENTS,[])
    tick=bybit_tickers()
    # Broad V1: first observation becomes a paper BUY. No MAX_OPEN.
    for s,m in tick.items():
        if s not in state["positions"]:
            p={"symbol":s,"opened_at":now(),"tranches":[{"at":now(),"price":m["price"],"notional":NOTIONAL,"reason":"BROAD_OBSERVATION_ENTRY","snapshot":m}],"mfe_pct":0.0,"mae_pct":0.0}
            state["positions"][s]=p
            events.append({"type":"BUY","at":now(),"symbol":s,"price":m["price"],"notional":NOTIONAL,"reason":"BROAD_OBSERVATION_ENTRY","snapshot":m})
    # Track every open position; add experiments at materially cheaper prices while capped per name.
    for s,p in list(state["positions"].items()):
        if s not in tick: continue
        price=tick[s]["price"]; a=avg(p); ret=(price/a-1)*100 if a else 0
        p["mfe_pct"]=max(p.get("mfe_pct",ret),ret); p["mae_pct"]=min(p.get("mae_pct",ret),ret)
        p["last_price"]=price; p["last_at"]=now(); p["net_pnl_usdt_before_fees"]=round(pnl(p,price),6)
        first=p["tranches"][0]["price"]
        n=len(p["tranches"])
        threshold=-4*n
        if n<MAX_TRANCHES and (price/first-1)*100 <= threshold:
            tr={"at":now(),"price":price,"notional":NOTIONAL,"reason":"DIP_ADD_EXPERIMENT","snapshot":tick[s]}
            p["tranches"].append(tr); events.append({"type":"ADD","symbol":s,**tr})
    state["updated_at"]=now()
    save(STATE,state); save(EVENTS,events)
    save(SUMMARY,{"updated_at":now(),"simulation_only":True,"open_positions":len(state["positions"]),"closed_positions":len(state["closed"]),"events":len(events),"policy":{"max_open":None,"standard_tranche_usdt":NOTIONAL,"max_tranches_per_symbol":MAX_TRANCHES,"purpose":"broad forward sampling; learn winners vs losers before tightening V2"}})
    print(json.dumps({"tickers_seen":len(tick),"open":len(state["positions"]),"events":len(events)}))

if __name__=="__main__": main()
