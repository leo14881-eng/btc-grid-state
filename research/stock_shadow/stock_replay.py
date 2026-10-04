#!/usr/bin/env python3
"""Chronological Stock Shadow Replay; observation-only, no future-data leakage."""
import json, os, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path
import stock_shadow_v1 as ss
OUT=Path("research/results/stock-shadow/replay-v1.json"); SURGE_PCT=8.0

def alpaca(symbols,timeframe,start,end):
    key=os.getenv("APCA_API_KEY_ID"); secret=os.getenv("APCA_API_SECRET_KEY")
    if not key or not secret: raise RuntimeError("missing_alpaca_secrets")
    params={"symbols":",".join(s.replace("-",".") for s in symbols),"timeframe":timeframe,"start":start,"end":end,
            "limit":10000,"feed":"sip","adjustment":"all"}
    headers={"APCA-API-KEY-ID":key,"APCA-API-SECRET-KEY":secret,"User-Agent":"stock-shadow-replay/1.0"}
    out={}; token=None
    while True:
        if token: params["page_token"]=token
        req=urllib.request.Request(ss.ALPACA_BARS_URL+"?"+urllib.parse.urlencode(params),headers=headers)
        with urllib.request.urlopen(req,timeout=45) as r: body=json.load(r)
        for sym,rows in (body.get("bars") or {}).items(): out.setdefault(sym.replace(".","-"),[]).extend(rows)
        token=body.get("next_page_token")
        if not token: return out

def partial_day(bars):
    return {"t":bars[-1]["t"],"o":bars[0]["o"],"h":max(x["h"] for x in bars),"l":min(x["l"] for x in bars),
            "c":bars[-1]["c"],"v":sum(float(x.get("v") or 0) for x in bars)}

def snap(rows): return ss._snapshot_from_bars("REPLAY",rows,"ALPACA_REPLAY_CHRONOLOGICAL")

def chronological_signal(prior,intra,spy_prior=None,spy_intra=None,qqq_prior=None,qqq_intra=None):
    seen=[]; sp=[]; qp=[]; sm={x["t"]:x for x in (spy_intra or [])}; qm={x["t"]:x for x in (qqq_intra or [])}
    for bar in sorted(intra,key=lambda x:x["t"]):
        seen.append(bar)
        if bar["t"] in sm: sp.append(sm[bar["t"]])
        if bar["t"] in qm: qp.append(qm[bar["t"]])
        rows=list(prior)+[partial_day(seen)]
        if len(rows)<22: continue
        m=snap(rows)
        spy=snap(list(spy_prior)+[partial_day(sp)]) if spy_prior and sp else None
        qqq=snap(list(qqq_prior)+[partial_day(qp)]) if qqq_prior and qp else None
        d=ss.entry_decision(m,spy,qqq)
        if d["ready"]:
            return {"at":bar["t"],"price":bar["c"],"entry_structure":d["entry_structure"],"score":d["score"],
                    "rejects":d["rejects"],"reasons":d["reasons"]}
    return None

def main():
    symbols,discovery_errors=ss.discover_us_common_stocks()
    end=datetime.now(timezone.utc)-timedelta(minutes=20); start=end-timedelta(days=45)
    daily={}; errors=[]
    for i in range(0,len(symbols),ss.ALPACA_BATCH_SIZE):
        try: daily.update(alpaca(symbols[i:i+ss.ALPACA_BATCH_SIZE],"1Day",start.isoformat().replace("+00:00","Z"),end.isoformat().replace("+00:00","Z")))
        except Exception as e: errors.append({"stage":"DAILY","batch_start":i,"type":type(e).__name__,"message":str(e)[:120]})
    dates={}
    for rows in daily.values():
        for b in rows: dates[str(b["t"])[:10]]=dates.get(str(b["t"])[:10],0)+1
    if not dates: raise RuntimeError("replay_no_daily_bars")
    target=max(dates)
    movers=[]
    for sym,rows in daily.items():
        day=next((b for b in rows if str(b["t"])[:10]==target),None)
        if day and float(day.get("o") or 0)>0:
            gain=(float(day["h"])/float(day["o"])-1)*100
            if gain>=SURGE_PCT: movers.append((sym,gain,day))
    movers.sort(key=lambda x:x[1],reverse=True)
    d0=datetime.fromisoformat(target+"T00:00:00+00:00"); d1=d0+timedelta(days=1)
    wanted=list(dict.fromkeys([x[0] for x in movers]+["SPY","QQQ"])); intra={}
    for i in range(0,len(wanted),200):
        try: intra.update(alpaca(wanted[i:i+200],"5Min",d0.isoformat().replace("+00:00","Z"),d1.isoformat().replace("+00:00","Z")))
        except Exception as e: errors.append({"stage":"INTRADAY","batch_start":i,"type":type(e).__name__,"message":str(e)[:120]})
    results=[]
    spy_prior=[b for b in daily.get("SPY",[]) if str(b["t"])[:10]<target]; qqq_prior=[b for b in daily.get("QQQ",[]) if str(b["t"])[:10]<target]
    for sym,gain,day in movers:
        bars=intra.get(sym,[]); prior=[b for b in daily.get(sym,[]) if str(b["t"])[:10]<target]
        sig=chronological_signal(prior,bars,spy_prior,intra.get("SPY",[]),qqq_prior,intra.get("QQQ",[]))
        high=max((float(x["h"]) for x in bars),default=float(day["h"])); hb=next((x for x in bars if float(x["h"])==high),None)
        before=bool(sig and hb and sig["at"]<=hb["t"]); rem=((high/float(sig["price"])-1)*100) if sig else None
        results.append({"symbol":sym,"open_to_high_pct":round(gain,4),"high":high,"high_at":hb["t"] if hb else None,
                        "first_signal":sig,"discovered_before_high":before,
                        "remaining_upside_after_signal_pct":round(rem,4) if rem is not None else None,
                        "miss_reason":None if before else "NO_PRE_HIGH_ENTRY_SIGNAL"})
    out={"updated_at":datetime.now(timezone.utc).isoformat(),"mode":"REPLAY_OBSERVATION_ONLY","strategy_effect":False,
         "target_session":target,"surge_threshold_pct":SURGE_PCT,"universe_discovered":len(symbols),"universe_with_daily_bars":len(daily),
         "large_movers":len(results),"pre_high_discovered":sum(x["discovered_before_high"] for x in results),
         "future_data_prohibited":True,"errors":errors,"discovery_errors":discovery_errors,"results":results}
    OUT.parent.mkdir(parents=True,exist_ok=True); tmp=OUT.with_suffix(".tmp"); tmp.write_text(json.dumps(out,indent=2,sort_keys=True)+"\n"); tmp.replace(OUT)
    print(json.dumps({k:v for k,v in out.items() if k!="results"}))
if __name__=="__main__": main()
