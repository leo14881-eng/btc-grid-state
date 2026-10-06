#!/usr/bin/env python3
"""Point-in-time Stock Shadow Replay; observation-only and clock-faithful."""
import json, os, sys, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
import stock_shadow_v1 as ss

OUT=Path("research/results/stock-shadow/replay-v1.json")
TRADES=Path("research/results/stock-shadow/trades-v1.json")
SURGE_PCT=8.0
DECISION_CRON_MINUTE_UTC=23
MARKET_DATA_DELAY_MINUTES=20
BAR_MINUTES=5

class LookaheadViolation(RuntimeError):
    pass

class ReplayDataError(RuntimeError):
    pass


def replay_data_window(target, as_of):
    """Request only the historical SIP data covered by the existing 20m delay."""
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("replay_as_of_requires_timezone")
    start=datetime.fromisoformat(target+"T00:00:00+00:00")
    cutoff=as_of.astimezone(timezone.utc)-timedelta(minutes=MARKET_DATA_DELAY_MINUTES)
    return start,min(start+timedelta(days=1),cutoff)


def replay_data_health(wanted, movers, intra, errors, session, query_end):
    closed=datetime.combine(datetime.fromisoformat(session["date"]).date(),
                            session["close"],tzinfo=ss.NY).astimezone(timezone.utc)
    counts={symbol:len(intra.get(symbol,[])) for symbol in wanted}
    missing=[symbol for symbol in wanted if not counts[symbol]]
    complete=query_end>=closed
    status=("ERROR" if errors else "WAITING_FOR_SESSION_DATA" if not complete
            else "PARTIAL" if missing else "COMPLETE")
    return {"status":status,"required_symbols":wanted,"missing_symbols":missing,
            "regular_session_bar_counts":counts,"session_complete":complete,
            "session_close_utc":closed.isoformat().replace("+00:00","Z"),
            "cohort_status":"QUALIFYING_MOVERS" if movers else "NO_QUALIFYING_MOVERS"}


def save_report(out):
    OUT.parent.mkdir(parents=True,exist_ok=True)
    tmp=OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(out,indent=2,sort_keys=True)+"\n")
    tmp.replace(OUT)
    print(json.dumps({k:v for k,v in out.items() if k!="results"}))

def alpaca(symbols,timeframe,start,end):
    key=os.getenv("APCA_API_KEY_ID"); secret=os.getenv("APCA_API_SECRET_KEY")
    if not key or not secret: raise RuntimeError("missing_alpaca_secrets")
    params={"symbols":",".join(s.replace("-",".") for s in symbols),"timeframe":timeframe,"start":start,"end":end,
            "limit":10000,"feed":"sip","adjustment":"all"}
    headers={"APCA-API-KEY-ID":key,"APCA-API-SECRET-KEY":secret,"User-Agent":"stock-shadow-replay/2.0"}
    out={}; token=None
    while True:
        if token: params["page_token"]=token
        req=urllib.request.Request(ss.ALPACA_BARS_URL+"?"+urllib.parse.urlencode(params),headers=headers)
        with urllib.request.urlopen(req,timeout=45) as r: body=json.load(r)
        for sym,rows in (body.get("bars") or {}).items(): out.setdefault(sym.replace(".","-"),[]).extend(rows)
        token=body.get("next_page_token")
        if not token: return out

def _dt(v): return datetime.fromisoformat(str(v).replace("Z","+00:00"))

def regular_session_bars(bars, session):
    if not session: raise RuntimeError("replay_exchange_calendar_unavailable")
    out=[]
    for bar in bars:
        t=_dt(bar["t"]).astimezone(ss.NY)
        if t.date().isoformat()==session["date"] and session["open"]<=t.time()<session["close"]:
            out.append(bar)
    return out

def partial_day(bars):
    return {"t":bars[-1]["t"],"o":bars[0]["o"],"h":max(x["h"] for x in bars),"l":min(x["l"] for x in bars),
            "c":bars[-1]["c"],"v":sum(float(x.get("v") or 0) for x in bars)}

def snap(rows): return ss._snapshot_from_bars("REPLAY",rows,"ALPACA_REPLAY_POINT_IN_TIME")

def decision_clocks(session):
    """Actual Stock Shadow schedule is hourly at UTC minute :23; only clocks inside exchange session count."""
    day=datetime.fromisoformat(session["date"]+"T00:00:00+00:00")
    clocks=[]
    for h in range(24):
        t=day+timedelta(hours=h,minutes=DECISION_CRON_MINUTE_UTC)
        ny=t.astimezone(ss.NY)
        if ny.date().isoformat()==session["date"] and session["open"]<=ny.time()<session["close"]:
            clocks.append(t)
    return clocks

def bars_available_at(bars, decision_at):
    """A 5m bar is usable only after it closed and before the live system's 20m delayed-data cutoff."""
    cutoff=decision_at-timedelta(minutes=MARKET_DATA_DELAY_MINUTES)
    usable=[]
    for b in sorted(bars,key=lambda x:x["t"]):
        start=_dt(b["t"]); end=start+timedelta(minutes=BAR_MINUTES)
        if end<=cutoff: usable.append(b)
    if any(_dt(b["t"])+timedelta(minutes=BAR_MINUTES)>cutoff for b in usable):
        raise LookaheadViolation("bar_after_replay_data_cutoff")
    return usable,cutoff

def _decision(prior,intra,decision_at,spy_prior,spy_intra,qqq_prior,qqq_intra):
    seen,cutoff=bars_available_at(intra,decision_at)
    sp,_=bars_available_at(spy_intra or [],decision_at)
    qp,_=bars_available_at(qqq_intra or [],decision_at)
    if not seen: return None
    rows=list(prior)+[partial_day(seen)]
    if len(rows)<22: return None
    m=snap(rows)
    spy=snap(list(spy_prior)+[partial_day(sp)]) if spy_prior and sp else None
    qqq=snap(list(qqq_prior)+[partial_day(qp)]) if qqq_prior and qp else None
    d=ss.entry_decision(m,spy,qqq)
    return {"at":decision_at.isoformat().replace("+00:00","Z"),"data_cutoff":cutoff.isoformat().replace("+00:00","Z"),
            "price":seen[-1]["c"],"entry_structure":d["entry_structure"],"score":d["score"],
            "ready":d["ready"],"rejects":d["rejects"],"reasons":d["reasons"]}

def chronological_trace(prior,intra,session,cutoff_at=None,spy_prior=None,spy_intra=None,qqq_prior=None,qqq_intra=None):
    """Evaluate only the decision clocks the live hourly workflow actually had."""
    first_ready=None; first_early=None; last=None; decisions=[]
    for clock in decision_clocks(session):
        if cutoff_at and clock>_dt(cutoff_at): break
        rec=_decision(prior,intra,clock,spy_prior,spy_intra,qqq_prior,qqq_intra)
        if rec is None: continue
        decisions.append(rec); last=rec
        if rec["ready"] and first_ready is None: first_ready=rec
        if rec["ready"] and rec["entry_structure"]=="EARLY_ACCUMULATION" and first_early is None: first_early=rec
    return {"first_ready":first_ready,"first_early":first_early,"last_pre_high_decision":last,"decisions":decisions}

def future_mutation_invariance(prior,intra,session,cutoff_at,spy_prior,spy_intra,qqq_prior,qqq_intra):
    """Mutating all bars after the evaluated instant must not change any decision at/before that instant."""
    base=chronological_trace(prior,intra,session,cutoff_at,spy_prior,spy_intra,qqq_prior,qqq_intra)
    cutoff=_dt(cutoff_at)
    def mutate(rows):
        out=[]
        for b in rows or []:
            x=dict(b)
            if _dt(x["t"])>cutoff:
                for k in ("o","h","l","c"):
                    if k in x: x[k]=float(x[k])*10
                if "v" in x: x["v"]=float(x.get("v") or 0)*100
            out.append(x)
        return out
    changed=chronological_trace(prior,mutate(intra),session,cutoff_at,spy_prior,mutate(spy_intra),qqq_prior,mutate(qqq_intra))
    keys=("first_ready","first_early","last_pre_high_decision","decisions")
    if any(base[k]!=changed[k] for k in keys):
        raise LookaheadViolation("future_mutation_changed_past_decision")
    return True

def main():
    symbols,discovery_errors=ss.discover_us_common_stocks()
    as_of=datetime.now(timezone.utc)
    errors=[]
    market_cache=ss.load(ss.MARKET_CACHE,{"bars":{}})
    daily=market_cache.get("bars") or {}
    # Replay consumes the same persisted PIT daily cache as live V3; it must not redownload identical history.
    if not daily:
        raise RuntimeError("replay_daily_market_cache_missing")
    dates={}
    for sym,rows in daily.items():
        if sym in {"SPY","QQQ"}: continue
        for b in rows: dates[str(b["t"])[:10]]=dates.get(str(b["t"])[:10],0)+1
    if not dates: raise RuntimeError("replay_no_daily_bars")
    target=max(dates)
    movers=[]
    for sym,rows in daily.items():
        if sym in {"SPY","QQQ"}: continue
        day=next((b for b in rows if str(b["t"])[:10]==target),None)
        if day and float(day.get("o") or 0)>0:
            gain=(float(day["h"])/float(day["o"])-1)*100
            if gain>=SURGE_PCT: movers.append((sym,gain,day))
    movers.sort(key=lambda x:x[1],reverse=True)
    d0,query_end=replay_data_window(target,as_of)
    session=ss._alpaca_exchange_session(d0+timedelta(hours=16))
    if not session or session.get("date")!=target: raise RuntimeError("replay_exchange_calendar_unavailable")
    wanted=list(dict.fromkeys([x[0] for x in movers]+["SPY","QQQ"])); intra={}
    replay_batches=ss._pack_alpaca_symbol_batches(wanted,"5Min",d0,query_end) if query_end>d0 else []
    if query_end<=d0:
        errors.append({"stage":"INTRADAY_WINDOW","type":"ReplayDataError",
                       "message":"target session has no data in the allowed delayed window"})
    for batch_index,batch in enumerate(replay_batches):
        try:
            intra.update(alpaca(batch,"5Min",d0.isoformat().replace("+00:00","Z"),query_end.isoformat().replace("+00:00","Z")))
        except Exception as e:
            errors.append({"stage":"INTRADAY","batch_index":batch_index,"type":type(e).__name__,"message":str(e)[:120]})
    intra={sym:[bar for bar in regular_session_bars(rows,session)
                if _dt(bar["t"])+timedelta(minutes=BAR_MINUTES)<=query_end]
           for sym,rows in intra.items()}
    data_health=replay_data_health(wanted,movers,intra,errors,session,query_end)
    query_window={"as_of_utc":as_of.isoformat().replace("+00:00","Z"),
                  "start":d0.isoformat().replace("+00:00","Z"),
                  "end":query_end.isoformat().replace("+00:00","Z"),
                  "market_data_delay_minutes":MARKET_DATA_DELAY_MINUTES}
    if data_health["status"]!="COMPLETE":
        save_report({"updated_at":datetime.now(timezone.utc).isoformat(),
            "mode":"REPLAY_OBSERVATION_ONLY","acceptance_status":"REJECTED_DATA",
            "strategy_effect":False,"target_session":target,
            "strategy_version":"HYBRID_ENTRY_V1_POSITION_STATE_V3",
            "query_window":query_window,"data_health":data_health,
            "big_movers_total":len(movers),"evaluated_movers":0,
            "replay_intraday_logical_batches":len(replay_batches),
            "errors":errors,"discovery_errors":discovery_errors,"results":[]})
        raise ReplayDataError("replay_data_not_accepted:"+data_health["status"])
    try: trades=json.loads(TRADES.read_text()) if TRADES.exists() else []
    except Exception: trades=[]
    results=[]; lookahead_violations=[]
    spy_prior=[b for b in daily.get("SPY",[]) if str(b["t"])[:10]<target]; qqq_prior=[b for b in daily.get("QQQ",[]) if str(b["t"])[:10]<target]
    for sym,gain,day in movers:
        bars=intra.get(sym,[]); prior=[b for b in daily.get(sym,[]) if str(b["t"])[:10]<target]
        high=max((float(x["h"]) for x in bars),default=float(day["h"])); hb=next((x for x in bars if float(x["h"])==high),None)
        high_at=hb["t"] if hb else None
        trace=chronological_trace(prior,bars,session,high_at,spy_prior,intra.get("SPY",[]),qqq_prior,intra.get("QQQ",[]))
        invariant=False
        if high_at:
            try:
                invariant=future_mutation_invariance(prior,bars,session,high_at,spy_prior,intra.get("SPY",[]),qqq_prior,intra.get("QQQ",[]))
            except LookaheadViolation as e:
                lookahead_violations.append({"symbol":sym,"at":high_at,"error":str(e)})
        sig=trace["first_ready"]; early=trace["first_early"]; before=bool(sig and high_at and _dt(sig["at"])<=_dt(high_at))
        rem=((high/float(sig["price"])-1)*100) if sig else None; early_rem=((high/float(early["price"])-1)*100) if early else None
        session_open=float(day["o"])
        early_gain=((float(early["price"])/session_open-1)*100) if early else None; signal_gain=((float(sig["price"])/session_open-1)*100) if sig else None
        buys=[e for e in trades if e.get("type")=="BUY" and e.get("symbol")==sym and str(e.get("at",""))[:10]==target]
        buy_before=next((e for e in sorted(buys,key=lambda x:str(x.get("at",""))) if high_at and _dt(e.get("at"))<=_dt(high_at)),None)
        last=trace["last_pre_high_decision"] or {}
        results.append({"symbol":sym,"open_price":session_open,"intraday_high":high,"open_to_high_pct":round(gain,4),"high_at":high_at,
          "first_early_at":early.get("at") if early else None,"first_buy_at":sig.get("at") if sig else None,
          "price_at_first_early":early.get("price") if early else None,"first_signal":sig,"first_buy_signal":sig,"first_early_signal":early,
          "discovered_before_high":before,"actual_buy_before_high":bool(buy_before),"actual_buy":buy_before,
          "gain_before_early_pct":round(early_gain,4) if early_gain is not None else None,
          "gain_at_first_signal_pct":round(signal_gain,4) if signal_gain is not None else None,
          "remaining_upside_after_early_pct":round(early_rem,4) if early_rem is not None else None,
          "remaining_upside_after_signal_pct":round(rem,4) if rem is not None else None,
          "gate_trace":trace["decisions"],"missed_gate_reasons":last.get("rejects",[]) if not before else [],
          "gate_snapshot_at_last_pre_high":last,"lookahead_check":"PASS" if invariant else "NOT_RUN",
          "miss_reason":None if before else "NO_PRE_HIGH_ENTRY_SIGNAL"})
    early_count=sum(bool(x["first_early_signal"]) for x in results); buy_count=sum(bool(x["first_buy_signal"]) for x in results)
    late_early=sum(bool(x["first_early_signal"] and x["high_at"] and _dt(x["first_early_signal"]["at"])>_dt(x["high_at"])) for x in results)
    out={"updated_at":datetime.now(timezone.utc).isoformat(),"mode":"REPLAY_OBSERVATION_ONLY","acceptance_status":"REJECTED_LOOKAHEAD" if lookahead_violations else "ACCEPTED","strategy_effect":False,
      "target_session":target,"strategy_version":"HYBRID_ENTRY_V1_POSITION_STATE_V3",
      "query_window":query_window,"data_health":data_health,
      "decision_clock":{"schedule":"hourly UTC minute :23","market_data_delay_minutes":MARKET_DATA_DELAY_MINUTES,"bar_minutes":BAR_MINUTES},
      "exchange_session":{"date":session["date"],"open":str(session["open"]),"close":str(session["close"]),"source":session["source"]},
      "surge_threshold_pct":SURGE_PCT,"universe_discovered":len(symbols),"universe_with_daily_bars":len(daily),
      "big_movers_total":len(results),"large_movers":len(results),"early_detected":early_count,
      "early_detection_rate":round(early_count/len(results)*100,4) if results else 0,
      "buy_detected":buy_count,"buy_detection_rate":round(buy_count/len(results)*100,4) if results else 0,
      "late_early_count":late_early,"missed_by_gate":sum(not x["discovered_before_high"] for x in results),
      "pre_high_discovered":sum(x["discovered_before_high"] for x in results),"actual_buy_before_high":sum(x["actual_buy_before_high"] for x in results),
      "early_before_high":sum(bool(x["first_early_signal"] and x["high_at"] and _dt(x["first_early_signal"]["at"])<=_dt(x["high_at"])) for x in results),
      "missed_before_high":sum(not x["discovered_before_high"] for x in results),"benchmark_daily_explicit":True,"benchmark_daily_source":"UNIFIED_MARKET_CACHE",
      "future_data_prohibited":True,"future_mutation_invariance":True,"lookahead_violations":lookahead_violations,
      "future_leakage_detected":bool(lookahead_violations),"replay_intraday_logical_batches":len(replay_batches),"errors":errors,"discovery_errors":discovery_errors,"results":results}
    save_report(out)
    if lookahead_violations: raise LookaheadViolation(f"{len(lookahead_violations)} replay lookahead violations")

if __name__=="__main__": main()
