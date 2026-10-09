#!/usr/bin/env python3
"""Position-only Stock Shadow monitor. Never discovers or opens positions."""
import json, os, urllib.parse, urllib.request, urllib.error
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
try:
    from .market_session import session_allows_trade
except ImportError:
    try:
        from market_session import session_allows_trade
    except ImportError:
        from research.stock_shadow.market_session import session_allows_trade

try:
    from .state_safety import number, optional_number, validate_inputs, portfolio_statistics, run_with_health, quote_validity, mark_quote
except ImportError:
    try:
        from state_safety import number, optional_number, validate_inputs, portfolio_statistics, run_with_health, quote_validity, mark_quote
    except ImportError:
        from research.stock_shadow.state_safety import number, optional_number, validate_inputs, portfolio_statistics, run_with_health, quote_validity, mark_quote

ROOT=Path("research/results/stock-shadow")
STATE=ROOT/"portfolio-v1.json"; EVENTS=ROOT/"trades-v1.json"; HEALTH=ROOT/"position-monitor-v1.json"; CALENDAR_CACHE=ROOT/"calendar-session-cache-v1.json"
FEE_RATE=0.002; MAX_REQUEST_TARGET_CHARS=7000
ARM_NET_PCT=1.0
PROFIT_FLOOR_NET_PCT=0.10
PROFIT_GIVEBACK_BANDS=((30.0,0.20),(15.0,0.25),(8.0,0.35),(1.0,0.50))
SOURCE_COMMIT=os.getenv("STOCK_SHADOW_SOURCE_COMMIT","LOCAL")
RUN_ID=os.getenv("STOCK_SHADOW_RUN_ID","LOCAL")
NY=ZoneInfo("America/New_York")
CALENDAR_USAGE={"http_requests":0,"cache_hits":0,"cache_misses":0,"errors":0}

def now(): return datetime.now(timezone.utc).isoformat()
def load(p,d):
    try: return json.loads(p.read_text()) if p.exists() else d
    except Exception:
        if p in (STATE, EVENTS): raise
        return d
def save(p,o):
    p.parent.mkdir(parents=True,exist_ok=True)
    tmp=p.with_suffix(p.suffix+".tmp")
    tmp.write_text(json.dumps(o,ensure_ascii=False,indent=2,sort_keys=True)+"\n")
    tmp.replace(p)

def continuity_fingerprint(state, events):
    """Immutable identity of positions/tranches/trade ledger; quote/P&L updates are allowed."""
    positions=state.get("positions",{})
    cohort=[]
    for symbol,p in sorted(positions.items()):
        tranches=tuple((str(t.get("at","")),round(float(t.get("price",0)),8),round(float(t.get("notional",0)),8),str(t.get("reason",""))) for t in p.get("tranches",[]))
        cohort.append((symbol,str(p.get("opened_at","")),tranches))
    trade_ids=tuple((str(e.get("type","")),str(e.get("symbol","")),str(e.get("at","")),round(float(e.get("price",0)),8),round(float(e.get("notional",0) or 0),8),str(e.get("reason",""))) for e in events)
    closed_ids=tuple((str(x.get("symbol","")),str(x.get("closed_at","")),str(x.get("exit_reason",""))) for x in state.get("closed",[]))
    return (tuple(cohort),trade_ids,closed_ids)

def validate_ledger(state, events):
    positions=state.get("positions",{})
    if state.get("reset_reason") or state.get("reset_at"):
        raise RuntimeError("ledger_invariant:manual_reset_marker_present")
    if any(len((p or {}).get("tranches",[]))>5 for p in positions.values()):
        raise RuntimeError("ledger_invariant:max_tranches_exceeded")
    if any(float(x.get("realized_net_pnl_usdt",0))<=0 for x in state.get("closed",[])):
        raise RuntimeError("ledger_invariant:losing_sell_present")
    if any(x.get("type")=="SELL" and float(x.get("net_pnl_usdt",0))<=0 for x in events):
        raise RuntimeError("ledger_invariant:nonpositive_sell_event")
    return True
def _alpaca_exchange_session(ts=None):
    t=(ts or datetime.now(timezone.utc)).astimezone(NY); day=t.date().isoformat()
    cached=load(CALENDAR_CACHE,{})
    if cached.get("date")==day:
        if cached.get("closed") is True:
            CALENDAR_USAGE["cache_hits"]+=1; return None
        if cached.get("open") and cached.get("close"):
            try:
                oh,om=map(int,cached["open"].split(":")); ch,cm=map(int,cached["close"].split(":"))
                CALENDAR_USAGE["cache_hits"]+=1
                return {"date":day,"open":dtime(oh,om),"close":dtime(ch,cm),"source":"ALPACA_EXCHANGE_CALENDAR_CACHE"}
            except Exception: pass
    CALENDAR_USAGE["cache_misses"]+=1
    key=os.getenv("APCA_API_KEY_ID"); secret=os.getenv("APCA_API_SECRET_KEY")
    if not key or not secret:
        CALENDAR_USAGE["errors"]+=1; return None
    url="https://paper-api.alpaca.markets/v2/calendar?"+urllib.parse.urlencode({"start":day,"end":day})
    req=urllib.request.Request(url,headers={"APCA-API-KEY-ID":key,"APCA-API-SECRET-KEY":secret,"Accept":"application/json"})
    try:
        CALENDAR_USAGE["http_requests"]+=1
        with urllib.request.urlopen(req,timeout=15) as r: rows=json.load(r)
        if not rows:
            save(CALENDAR_CACHE,{"date":day,"closed":True,"verified_at":now(),"source":"ALPACA_EXCHANGE_CALENDAR"})
            return None
        row=rows[0]; oh,om=map(int,row["open"].split(":")); ch,cm=map(int,row["close"].split(":"))
        save(CALENDAR_CACHE,{"date":day,"open":row["open"],"close":row["close"],"verified_at":now(),"source":"ALPACA_EXCHANGE_CALENDAR"})
        return {"date":day,"open":dtime(oh,om),"close":dtime(ch,cm),"source":"ALPACA_EXCHANGE_CALENDAR"}
    except Exception:
        CALENDAR_USAGE["errors"]+=1; return None

def market_open(ts=None, session=None):
    ts=ts or datetime.now(timezone.utc)
    s=session if session is not None else _alpaca_exchange_session(ts)
    return session_allows_trade(ts,s)
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
    """Batch delayed SIP 5m bars for all held symbols using maximum practical GET batches."""
    if not symbols: return {},[],0,0
    key=os.getenv("APCA_API_KEY_ID"); secret=os.getenv("APCA_API_SECRET_KEY")
    if not key or not secret: return {},[{"reason":"MISSING_ALPACA_SECRETS"}],0,0
    headers={"APCA-API-KEY-ID":key,"APCA-API-SECRET-KEY":secret,"User-Agent":"stock-shadow-position-monitor/3.0"}
    prices={}; errors=[]; requests=0
    end=datetime.now(timezone.utc)-__import__("datetime").timedelta(minutes=20)
    # Keep enough calendar lookback to cover weekends/holidays while still issuing one multi-symbol logical batch.
    start=end-__import__("datetime").timedelta(days=3)
    batches=[]; batch=[]
    for sym in symbols:
        candidate=batch+[sym]
        probe={"symbols":",".join(s.replace("-",".") for s in candidate),"timeframe":"5Min",
               "start":start.isoformat().replace("+00:00","Z"),"end":end.isoformat().replace("+00:00","Z"),
               "limit":10000,"feed":"sip","adjustment":"all"}
        target="/v2/stocks/bars?"+urllib.parse.urlencode(probe)
        if batch and len(target)>MAX_REQUEST_TARGET_CHARS:
            batches.append(batch); batch=[sym]
        else:
            batch=candidate
    if batch: batches.append(batch)
    for batch_index,batch in enumerate(batches):
        params={"symbols":",".join(s.replace("-",".") for s in batch),"timeframe":"5Min",
                "start":start.isoformat().replace("+00:00","Z"),"end":end.isoformat().replace("+00:00","Z"),
                "limit":10000,"feed":"sip","adjustment":"all"}
        token=None
        try:
            latest={}
            while True:
                if token: params["page_token"]=token
                else: params.pop("page_token",None)
                req=urllib.request.Request("https://data.alpaca.markets/v2/stocks/bars?"+urllib.parse.urlencode(params),headers=headers)
                with urllib.request.urlopen(req,timeout=35) as r: body=json.load(r)
                requests+=1
                for raw,rows in (body.get("bars") or {}).items():
                    if rows:
                        row=max(rows,key=lambda x:str(x.get("t") or ""))
                        latest[raw.replace(".","-")]={"price":row.get("c"),"price_asof":row.get("t"),"source":"ALPACA_SIP_5M_DELAYED"}
                token=body.get("next_page_token")
                if not token: break
            prices.update(latest)
        except urllib.error.HTTPError as e:
            requests+=1; errors.append({"batch":batch_index,"reason":f"HTTP_{e.code}","detail":str(e.reason)})
        except Exception as e:
            requests+=1; errors.append({"batch":batch_index,"reason":type(e).__name__,"detail":str(e)[:120]})
    return prices,errors,requests,len(batches)

def main(force=False):
    state=load(STATE,{"positions":{},"closed":[]}); events=load(EVENTS,[])
    validate_inputs(state,events)
    if state.get("reset_reason") or state.get("reset_at"):
        raise RuntimeError("state_continuity:manual_reset_marker_present")
    positions=state.get("positions",{}); symbols=sorted(positions)
    starting_positions=len(symbols); starting_events=len(events); starting_closed=len(state.get("closed",[]))
    starting_fingerprint=continuity_fingerprint(state,events)
    session=_alpaca_exchange_session()
    is_open=market_open(session=session)
    if not force and not is_open:
        save(HEALTH,{"updated_at":now(),"source_commit":SOURCE_COMMIT,"run_id":RUN_ID,"status":"SKIPPED_MARKET_CLOSED","positions":len(symbols),"requests":0,"provider":"ALPACA_SIP_5M_DELAYED","buy_capability":False,"calendar_api_usage":dict(CALENDAR_USAGE)})
        print(json.dumps(load(HEALTH,{}))); return
    prices,errors,requests,logical_batches=alpaca_snapshot_quotes(symbols)
    trade_actions_enabled=is_open
    sells=0; updated=0; invalid_quotes={}
    checked_at=datetime.fromisoformat(now())
    for s in list(symbols):
        observation=prices.get(s)
        p=positions.get(s)
        if not p: continue
        validity=quote_validity(s,observation,checked_at,"5Min")
        mark_quote(p,validity,checked_at,observation)
        if validity != "VALID":
            invalid_quotes[s]=validity
            continue
        price=observation["price"]
        if p.get("mfe_net_pct") is None or p.get("mae_net_pct") is None:
            p["extrema_history_status"]="INITIALIZED_FROM_CURRENT_OBSERVATION"
        r=net_pct(p,price); mfe=max(number(p.get("mfe_net_pct"),f"{s}.mfe_net_pct",default=r),r); mae=min(number(p.get("mae_net_pct"),f"{s}.mae_net_pct",default=r),r)
        p.update({"last_price":price,"last_at":now(),"avg_price":avg(p),"net_pnl_usdt":round(net_pnl(p,price),6),"net_return_pct":round(r,6),"mfe_net_pct":mfe,"mae_net_pct":mae})
        updated+=1
        giveback=mfe-r
        # V3: five-minute monitor uses the exact same dynamic AFTER-FEE profit floor
        # as the full lifecycle. It never discovers BUY/ADD candidates.
        floor=profit_floor_net_pct(mfe)
        p["profit_protection_floor_net_pct"]=round(floor,6) if floor is not None else None
        p["profit_protection_signal"]=bool(floor is not None and r>0 and r<=floor)
        p["profit_giveback_pct_points"]=round(giveback,6)
        closed_at=now()
        if p["profit_protection_signal"] and trade_actions_enabled and market_open(datetime.fromisoformat(closed_at),session=session):
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
    state["source_commit"]=SOURCE_COMMIT; state["run_id"]=RUN_ID
    if not trade_actions_enabled:
        if len(positions)!=starting_positions:
            raise RuntimeError(f"state_continuity:off_session_position_count_changed:{starting_positions}->{len(positions)}")
        if len(events)!=starting_events:
            raise RuntimeError(f"state_continuity:off_session_event_count_changed:{starting_events}->{len(events)}")
        if len(state.get("closed",[]))!=starting_closed:
            raise RuntimeError(f"state_continuity:off_session_closed_count_changed:{starting_closed}->{len(state.get('closed',[]))}")
        if continuity_fingerprint(state,events)!=starting_fingerprint:
            raise RuntimeError("state_continuity:off_session_forward_cohort_identity_changed")
    validate_ledger(state,events)
    save(STATE,state); save(EVENTS,events)
    summary=load(ROOT/"summary-v1.json",{})
    summary.update(portfolio_statistics(state,events))
    summary["scan_state_stale"]=summary.get("run_id") != state.get("run_id")
    save(ROOT/"summary-v1.json",summary)
    status="OK" if updated==len(symbols) else ("PARTIAL" if updated else ("OK_EMPTY" if not symbols else "DATA_UNAVAILABLE"))
    save(HEALTH,{"updated_at":now(),"source_commit":SOURCE_COMMIT,"run_id":RUN_ID,"status":status,"provider":"ALPACA_SIP_5M_DELAYED","positions_before":len(symbols),"quotes_received":len(prices),"positions_updated":updated,"missing_symbols":sorted(set(symbols)-set(prices)),"invalid_quotes":invalid_quotes,"valid_quotes":updated,"shadow_sells":sells,"requests":requests,"logical_batches":logical_batches,"batch_policy":"MAX_REQUEST_TARGET_CHARS_7000","errors":errors,"buy_capability":False,"real_orders":False,"trade_actions_enabled":trade_actions_enabled,"calendar_api_usage":dict(CALENDAR_USAGE)})
    print(json.dumps(load(HEALTH,{}),ensure_ascii=False))
if __name__=="__main__":
    run_with_health(main,ROOT/"monitor-run-health-v1.json",SOURCE_COMMIT,RUN_ID,save,
                    force="--force" in __import__("sys").argv or os.getenv("STOCK_SHADOW_FORCE_MONITOR")=="1")

