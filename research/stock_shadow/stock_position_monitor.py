#!/usr/bin/env python3
"""Position-only Stock Shadow monitor. Never discovers or opens positions."""
import json, os, urllib.parse, urllib.request, urllib.error
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT=Path("research/results/stock-shadow")
STATE=ROOT/"portfolio-v1.json"; EVENTS=ROOT/"trades-v1.json"; HEALTH=ROOT/"position-monitor-v1.json"
FEE_RATE=0.002; BATCH_SIZE=40; MAX_BATCHES=12
ARM_NET_PCT=1.0
PROFIT_FLOOR_NET_PCT=0.10
PROFIT_GIVEBACK_BANDS=((30.0,0.20),(15.0,0.25),(8.0,0.35),(1.0,0.50))
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
def profit_floor_net_pct(mfe):
    """Same V3 dynamic after-fee profit floor as the full Stock Shadow lifecycle."""
    if mfe < ARM_NET_PCT: return None
    for threshold, giveback_fraction in PROFIT_GIVEBACK_BANDS:
        if mfe >= threshold:
            return max(PROFIT_FLOOR_NET_PCT, mfe*(1.0-giveback_fraction))
    return None

def _get_json(url):
    req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0 stock-shadow-position-monitor","Accept":"application/json"})
    with urllib.request.urlopen(req,timeout=20) as r: return json.load(r)
def _parse_price(v):
    try:
        x=float(str(v).replace("$","").replace(",","").strip()); return x if x>0 else None
    except Exception: return None

def alpaca_snapshot_quotes(symbols):
    """Batch latest SIP trades for held symbols; no Yahoo/Nasdaq fallback."""
    if not symbols: return {},[],0
    key=os.getenv("APCA_API_KEY_ID"); secret=os.getenv("APCA_API_SECRET_KEY")
    if not key or not secret: return {},[{"reason":"MISSING_ALPACA_SECRETS"}],0
    headers={"APCA-API-KEY-ID":key,"APCA-API-SECRET-KEY":secret,"User-Agent":"stock-shadow-position-monitor/3.0"}
    prices={}; errors=[]; requests=0
    # Historical SIP latest-trades is entitlement-safe outside the 15-minute real-time window.
    # Use latest bars with an end delayed 20m; during market hours this remains near-real-time enough
    # for the 5m shadow monitor without requiring a paid real-time subscription.
    end=datetime.now(timezone.utc)-__import__("datetime").timedelta(minutes=20)
    start=end-__import__("datetime").timedelta(days=3)
    for i in range(0,len(symbols),BATCH_SIZE):
        batch=symbols[i:i+BATCH_SIZE]
        params={"symbols":",".join(s.replace("-",".") for s in batch),"timeframe":"5Min",
                "start":start.isoformat().replace("+00:00","Z"),"end":end.isoformat().replace("+00:00","Z"),
                "limit":10000,"feed":"sip","adjustment":"all"}
        token=None
        try:
            latest={}
            while True:
                if token: params["page_token"]=token
                req=urllib.request.Request("https://data.alpaca.markets/v2/stocks/bars?"+urllib.parse.urlencode(params),headers=headers)
                with urllib.request.urlopen(req,timeout=35) as r: body=json.load(r)
                requests+=1
                for raw,rows in (body.get("bars") or {}).items():
                    if rows: latest[raw.replace(".","-")]=float(rows[-1]["c"])
                token=body.get("next_page_token")
                if not token: break
            prices.update(latest)
        except urllib.error.HTTPError as e:
            requests+=1; errors.append({"batch":i//BATCH_SIZE,"reason":f"HTTP_{e.code}","detail":str(e.reason)})
        except Exception as e:
            requests+=1; errors.append({"batch":i//BATCH_SIZE,"reason":type(e).__name__,"detail":str(e)[:120]})
    return prices,errors,requests

def main(force=False):
    state=load(STATE,{"positions":{},"closed":[]}); events=load(EVENTS,[])
    positions=state.get("positions",{}); symbols=sorted(positions)
    if not force and not market_open():
        save(HEALTH,{"updated_at":now(),"status":"SKIPPED_MARKET_CLOSED","positions":len(symbols),"requests":0,"provider":"ALPACA_SIP_5M_DELAYED","buy_capability":False})
        print(json.dumps(load(HEALTH,{}))); return
    prices,errors,requests=alpaca_snapshot_quotes(symbols)
    trade_actions_enabled=market_open()
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
        # V3: five-minute monitor uses the exact same dynamic AFTER-FEE profit floor
        # as the full lifecycle. It never discovers BUY/ADD candidates.
        floor=profit_floor_net_pct(mfe)
        p["profit_protection_floor_net_pct"]=round(floor,6) if floor is not None else None
        p["profit_protection_signal"]=bool(floor is not None and r>0 and r<=floor)
        p["profit_giveback_pct_points"]=round(giveback,6)
        if p["profit_protection_signal"] and trade_actions_enabled:
            closed_at=now()
            final_pnl=net_pnl(p,price); final_r=net_pct(p,price)
            closed=dict(p); closed.update({"closed_at":closed_at,"exit_price":price,
                "exit_reason":"NET_PROFIT_GIVEBACK_V3",
                "realized_net_pnl_usdt":round(final_pnl,6),
                "realized_net_return_pct":round(final_r,6),
                "gross_price_return_pct":round((price/avg(p)-1)*100,6),
                "estimated_total_fees_usdt":round((sum(x["notional"] for x in p["tranches"])*FEE_RATE)+(qty(p)*price*FEE_RATE),6),
                "profit_giveback_pct_points":round(giveback,6),
                "post_exit_tracking_due_days":[1,3,5,10]})
            state.setdefault("closed",[]).append(closed)
            del positions[s]
            events.append({"type":"SELL","at":closed_at,"symbol":s,"price":price,
                "reason":"NET_PROFIT_GIVEBACK_V3","net_pnl_usdt":closed["realized_net_pnl_usdt"],
                "net_return_pct":closed["realized_net_return_pct"],
                "gross_price_return_pct":closed["gross_price_return_pct"],
                "estimated_total_fees_usdt":closed["estimated_total_fees_usdt"],
                "USER_ALERT_REQUIRED":True,"source":"POSITION_MONITOR_5M"})
            sells+=1
    state["updated_at"]=now(); state["simulation_only"]=True
    save(STATE,state); save(EVENTS,events)
    status="OK" if updated==len(symbols) else ("PARTIAL" if updated else ("OK_EMPTY" if not symbols else "UNKNOWN"))
    save(HEALTH,{"updated_at":now(),"status":status,"provider":"ALPACA_SIP_5M_DELAYED","positions_before":len(symbols),"quotes_received":len(prices),"positions_updated":updated,"missing_symbols":sorted(set(symbols)-set(prices)),"shadow_sells":sells,"requests":requests,"batch_size":BATCH_SIZE,"errors":errors,"buy_capability":False,"real_orders":False,"trade_actions_enabled":trade_actions_enabled})
    print(json.dumps(load(HEALTH,{}),ensure_ascii=False))
if __name__=="__main__": main(force="--force" in __import__("sys").argv or os.getenv("STOCK_SHADOW_FORCE_MONITOR")=="1")
