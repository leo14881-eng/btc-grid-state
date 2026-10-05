#!/usr/bin/env python3
"""Partial historical replay for Hunter Leading Risk shadow phase.

Only completed spot klines are reconstructed. Historical order-book withdrawal,
OI/funding, liquidation queues and stablecoin microstructure are not fabricated.
"""
import datetime as dt,json,math,os,pathlib,time,urllib.parse,urllib.request
try:
 from research.hunter_policy import C
except ModuleNotFoundError as exc:
 if exc.name!="research":raise
 from hunter_policy import C

ROOT=pathlib.Path("research/results");OUT=ROOT/"hunter-leading-risk-replay.json"
BN=os.getenv("HUNTER_BINANCE_API","https://data-api.binance.vision")
SYMBOLS=("BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","DOGEUSDT","ADAUSDT","SUIUSDT","ENAUSDT")
WINDOWS=[
 ("2025-10-10",dt.datetime(2025,10,10,12,tzinfo=dt.timezone.utc),dt.datetime(2025,10,11,12,tzinfo=dt.timezone.utc)),
 ("2024-08-05",dt.datetime(2024,8,4,12,tzinfo=dt.timezone.utc),dt.datetime(2024,8,6,12,tzinfo=dt.timezone.utc)),
 ("2025-02-03",dt.datetime(2025,2,2,12,tzinfo=dt.timezone.utc),dt.datetime(2025,2,4,12,tzinfo=dt.timezone.utc))]
def get(url):
 last=None
 for attempt in range(4):
  try:
   req=urllib.request.Request(url,headers={"User-Agent":"hunter-leading-replay/1.0","Accept":"application/json"})
   with urllib.request.urlopen(req,timeout=20) as r:return json.load(r)
  except Exception as exc:
   last=exc
   if attempt<3:time.sleep(.5*(2**attempt))
 raise last
def bars(sym,start,end):
 q=urllib.parse.urlencode({"symbol":sym,"interval":"5m","startTime":int(start.timestamp()*1000),"endTime":int(end.timestamp()*1000),"limit":1000})
 out=[]
 for x in get(BN+"/api/v3/klines?"+q):
  try:t=int(x[6]);p=float(x[4])
  except (TypeError,ValueError,IndexError):continue
  if p>0 and math.isfinite(p):out.append((t,p))
 return out
def align(series):
 maps={s:{t:p for t,p in rows} for s,rows in series.items()};common=set.intersection(*(set(x) for x in maps.values()))
 ts=sorted(common);return ts,{s:[maps[s][t] for t in ts] for s in maps}
def pct(a,b):return (b/a-1)*100 if a else 0
def run_window(name,start,end):
 try:ts,px=align({s:bars(s,start,end) for s in SYMBOLS})
 except Exception as exc:return {"name":name,"status":"DATA_UNAVAILABLE","error":type(exc).__name__+":"+str(exc)[:160],"aligned_bars":0}
 if len(ts)<100:return {"name":name,"status":"INSUFFICIENT_DATA","aligned_bars":len(ts)}
 btc=px["BTCUSDT"];alts=[s for s in SYMBOLS if s!="BTCUSDT"];signals=[]
 for i in range(12,len(ts)):
  b5=pct(btc[i-1],btc[i]);b15=pct(btc[i-3],btc[i]);b60=pct(btc[i-12],btc[i])
  alt1=[pct(px[s][i-12],px[s][i]) for s in alts]
  neg=sum(x<0 for x in alt1)/len(alt1);loss5=sum(x<=-5 for x in alt1)/len(alt1)
  rel=[alt1[j]-b60 for j in range(len(alts))];relneg=sum(x<0 for x in rel)/len(rel)
  groups={"breadth":neg>=C["LEADING_BREADTH_NEGATIVE_FRACTION"] or loss5>=C["LEADING_BREADTH_LOSS5_FRACTION"],
          "relative":relneg>=C["LEADING_RELATIVE_NEGATIVE_FRACTION"],
          "btc_structure":b15<=C["LEADING_BTC_15M_PCT"] or b60<=C["LEADING_BTC_1H_PCT"] or (b5<=C["LEADING_BTC_5M_PCT"] and b15<0)}
  score=(20 if groups["breadth"] else 0)+(20 if groups["relative"] else 0)+(15 if groups["btc_structure"] else 0)
  n=sum(groups.values());raw="PRE_CRASH_1" if score>=C["LEADING_PRE1_SCORE"] and n>=2 else "WATCH" if score>=C["LEADING_WATCH_SCORE"] else "NORMAL"
  signals.append((i,raw,score))
 basket=[sum(px[s][i]/px[s][0] for s in alts)/len(alts) for i in range(len(ts))]
 trough=min(range(len(basket)),key=lambda i:basket[i]);peak_before=max(range(trough+1),key=lambda i:basket[i]);crash_start=peak_before
 pre=[x for x in signals if x[0]<=trough and x[1].startswith("PRE_CRASH")]
 watch=[x for x in signals if x[0]<=trough and x[1]=="WATCH"]
 first=(pre or watch or [None])[0]
 lead=None if first is None else round((ts[crash_start]-ts[first[0]])/60000,2)
 at_move=None if first is None else round((basket[first[0]]/basket[crash_start]-1)*100,4)
 return {"name":name,"status":"OK","aligned_bars":len(ts),"basket_trough_pct":round((basket[trough]/basket[crash_start]-1)*100,4),
         "first_partial_warning":None if first is None else {"level":first[1],"at_utc":dt.datetime.fromtimestamp(ts[first[0]]/1000,dt.timezone.utc).isoformat(),
         "lead_minutes_vs_local_peak":lead,"basket_move_at_warning_pct":at_move},
         "partial_precrash_count":len(pre),"partial_watch_count":len(watch)}
def run():
 rows=[run_window(*w) for w in WINDOWS]
 report={"schema":"hunter_leading_risk_replay_v1","as_of_utc":dt.datetime.now(dt.timezone.utc).isoformat(),
         "scope":"LEADING_RISK_PARTIAL_PATH_REPLAY_NOT_STRATEGY_BACKTEST",
         "windows":rows,"limitations":["Historical spot klines reconstruct only BTC structure, breadth and BTC-relative deterioration.",
         "Historical order-book withdrawal, OI/funding acceleration, liquidations and stablecoin microstructure are unavailable and are not fabricated.",
         "Lead time can be negative when the local peak occurs before enough partial evidence exists; that is a miss, not rewritten as success.",
         "No historical replay proves executable exit fills."],"capital_authority":"NONE_SHADOW_ONLY","real_trading_enabled":False}
 OUT.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n");print(json.dumps(report,ensure_ascii=False));return report
if __name__=="__main__":run()
