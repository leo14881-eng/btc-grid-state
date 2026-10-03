#!/usr/bin/env python3
"""Position-only Stock Shadow monitor. Never discovers or opens positions."""
import json, os, time, urllib.parse, urllib.request
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT=Path("research/results/stock-shadow")
STATE=ROOT/"portfolio-v1.json"; EVENTS=ROOT/"trades-v1.json"; HEALTH=ROOT/"position-monitor-v1.json"
FEE_RATE=0.002; BATCH_SIZE=40; MAX_BATCHES=12
ARM_NET_PCT=3.0; GIVEBACK_PCT=5.0; PROFIT_FLOOR_NET_PCT=0.5
NY=ZoneInfo("America/New_York")

def now(): return datetime.now(timezone.utc).isoformat()
def load(p,d):
    try: return json.loads(p.read_text()) if p.exists() else d
    except Exception: return d
def save(p,o):
    p.parent.mkdir(parents=True,exist_ok=True); p.write_text(json.dumps(o,ensure_ascii=False,indent=2,sort_keys=True)+"\n")
def market_open(ts=None):
    t=(ts or datetime.now(timezone.utc)).astimezone(NY)
    return t.weekday()<5 and dtime(9,30)<=t.time()<dtime(16,0)
def avg(p):
    q=sum(t["notional"]/t["price"] for t in p["tranches"]); c=sum(t["notional"] for t in p["tranches"])
    return c/q if q else 0
def qty(p): return sum(t["notional"]/t["price"] for t in p["tranches"])
def net_pnl(p,price):
    c=sum(t["notional"] for t in p["tranches"]); proceeds=qty(p)*price
    return proceeds-c-(c*FEE_RATE+proceeds*FEE_RATE)
def net_pct(p,price):
    c=sum(t["notional"] for t in p["tranches"]); return net_pnl(p,price)/c*100 if c else 0
def _get_json(url):
    req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0 stock-shadow-position-monitor","Accept":"application/json"})
    with urllib.request.urlopen(req,timeout=20) as r: return json.load(r)
def parse_spark(payload):
    out={}
    spark=payload.get("spark",{}).get("result") or []
    for row in spark:
        symbol=row.get("symbol"); responses=row.get("response") or []
        if not symbol or not responses: continue
        meta=responses[0].get("meta") or {}
        price=meta.get("regularMarketPrice")
        if price is None:
            closes=(responses[0].get("indicators",{}).get("quote") or [{}])[0].get("close") or []
            vals=[x for x in closes if x is not None]; price=vals[-1] if vals else None
        if price is not None and float(price)>0: out[symbol]=float(price)
    return out
def batch_quotes(symbols):
    prices={}; errors=[]; requests=0
    for i in range(0,len(symbols),BATCH_SIZE):
        if requests>=MAX_BATCHES: break
        batch=symbols[i:i+BATCH_SIZE]
        url="https://query1.finance.yahoo.com/v7/finance/spark?"+urllib.parse.urlencode({"symbols":",".join(batch),"range":"1d","interval":"5m"})
        try:
            prices.update(parse_spark(_get_json(url))); requests+=1
        except urllib.error.HTTPError as e:
            requests+=1; errors.append({"batch":i//BATCH_SIZE,"reason":f"HTTP_{e.code}"})
            if e.code in (403,429): break
        except Exception as e:
            requests+=1; errors.append({"batch":i//BATCH_SIZE,"reason":type(e).__name__})
        if i+BATCH_SIZE<len(symbols): time.sleep(0.35)
    return prices,errors,requests
def main(force=False):
    state=load(STATE,{"positions":{},"closed":[]}); events=load(EVENTS,[])
    positions=state.get("positions",{}); symbols=sorted(positions)
    if not force and not market_open():
        save(HEALTH,{"updated_at":now(),"status":"SKIPPED_MARKET_CLOSED","positions":len(symbols),"requests":0,"provider":"YAHOO_SPARK_BATCH_5M","buy_capability":False})
        print(json.dumps(load(HEALTH,{}))); return
    prices,errors,requests=batch_quotes(symbols)
    sells=0; updated=0
    for s in list(symbols):
        price=prices.get(s)
        if price is None: continue
        p=positions.get(s)
        if not p: continue
        r=net_pct(p,price); mfe=max(p.get("mfe_net_pct",r),r); mae=min(p.get("mae_net_pct",r),r)
        p.update({"last_price":price,"last_at":now(),"avg_price":avg(p),"net_pnl_usdt":round(net_pnl(p,price),6),"net_return_pct":round(r,6),"mfe_net_pct":mfe,"mae_net_pct":mae})
        updated+=1
        giveback=mfe-r
        if mfe>=ARM_NET_PCT and r>0 and (giveback>=GIVEBACK_PCT or r<=PROFIT_FLOOR_NET_PCT):
            closed=dict(p); closed.update({"closed_at":now(),"exit_price":price,"exit_reason":"PROFIT_GIVEBACK_POSITION_MONITOR","realized_net_pnl_usdt":round(net_pnl(p,price),6),"realized_net_return_pct":round(r,6),"profit_giveback_pct_points":round(giveback,6)})
            state.setdefault("closed",[]).append(closed); del positions[s]; sells+=1
            events.append({"type":"SELL","at":closed["closed_at"],"symbol":s,"price":price,"reason":closed["exit_reason"],"net_pnl_usdt":closed["realized_net_pnl_usdt"],"net_return_pct":closed["realized_net_return_pct"],"mfe_net_pct":mfe,"giveback_pct_points":giveback,"USER_ALERT_REQUIRED":True})
    state["updated_at"]=now(); state["simulation_only"]=True
    save(STATE,state); save(EVENTS,events)
    status="OK" if updated==len(symbols) else ("PARTIAL" if updated else ("OK_EMPTY" if not symbols else "UNKNOWN"))
    save(HEALTH,{"updated_at":now(),"status":status,"provider":"YAHOO_SPARK_BATCH_5M","positions_before":len(symbols),"quotes_received":len(prices),"positions_updated":updated,"shadow_sells":sells,"requests":requests,"batch_size":BATCH_SIZE,"errors":errors,"buy_capability":False,"real_orders":False})
    print(json.dumps(load(HEALTH,{}),ensure_ascii=False))
if __name__=="__main__": main(force="--force" in __import__("sys").argv or os.getenv("STOCK_SHADOW_FORCE_MONITOR")=="1")
