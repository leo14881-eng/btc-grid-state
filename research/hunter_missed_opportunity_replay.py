#!/usr/bin/env python3
"""Daily Missed Opportunity Replay — read-only Hunter audit; never trades."""
import datetime as dt,json,os,pathlib,urllib.parse,urllib.request
from concurrent.futures import ThreadPoolExecutor,as_completed
from zoneinfo import ZoneInfo
ROOT=pathlib.Path("research/results")
SCAN=ROOT/"hunter-cex-universe-run.json"; UNIVERSE_HISTORY=ROOT/"hunter-universe-history.json"; EARLY=ROOT/"hunter-early-signal-history.json"
V1=ROOT/"hunter-shadow-portfolio.json"; V2=ROOT/"hunter-shadow-v2-portfolio.json"
REVIEW=ROOT/"hunter-tactical-capital-review.json"; OUT=ROOT/"hunter-missed-opportunity-replay.json"
BN=os.getenv("HUNTER_BINANCE_API","https://data-api.binance.vision")
TOP_N=int(os.getenv("HUNTER_REPLAY_TOP_N","10"))

def load(p):
 try:return json.loads(p.read_text())
 except Exception:return {}
def get(path,params):
 q=urllib.parse.urlencode(params); req=urllib.request.Request(BN+path+"?"+q,headers={"User-Agent":"hunter-missed-opportunity/1.0"})
 with urllib.request.urlopen(req,timeout=20) as r:return json.load(r)
def iso(ms):return dt.datetime.fromtimestamp(ms/1000,dt.timezone.utc).isoformat()
def today_bounds():
 tz=ZoneInfo("Asia/Ho_Chi_Minh"); local_now=dt.datetime.now(tz)
 local_start=local_now.replace(hour=0,minute=0,second=0,microsecond=0)
 return local_start.astimezone(dt.timezone.utc),local_now.astimezone(dt.timezone.utc),local_start.date().isoformat()
def day_stats(sym,start,now):
 rows=get("/api/v3/klines",{"symbol":sym,"interval":"15m","startTime":int(start.timestamp()*1000),"endTime":int(now.timestamp()*1000),"limit":1000})
 if not rows:return None
 o=float(rows[0][1]); hi=max(float(x[2]) for x in rows); lo=min(float(x[3]) for x in rows); last=float(rows[-1][4])
 return {"day_open":o,"day_high":hi,"day_low":lo,"last":last,"day_high_gain_pct":round((hi/o-1)*100,4),"last_gain_pct":round((last/o-1)*100,4),"high_at_utc":iso(max(rows,key=lambda x:float(x[2]))[0]),"_candles":rows}
def first_event(state,asset,types):
 ev=[x for x in state.get("events",[]) if x.get("asset")==asset and x.get("type") in types]
 ev.sort(key=lambda x:x.get("at") or x.get("at_utc") or "")
 return ev[0] if ev else None
def first_decision(state,asset):
 ds=[x for x in state.get("decisions",[]) if x.get("asset")==asset]
 ds.sort(key=lambda x:x.get("at") or "")
 return ds[0] if ds else None
def post_detection(s,h,v1buy):
 first_at=h.get("first_early_at_utc"); first_price=h.get("first_early_price") or ((v1buy or {}).get("price")); candles=s.get("_candles") or []
 evidence_at=first_at or ((v1buy or {}).get("at") or (v1buy or {}).get("at_utc"))
 status="EXACT_EARLY_RECORDED" if first_at else ("FIRST_EARLY_EXACT_UNKNOWN_BUT_AT_OR_BEFORE_V1_BUY" if v1buy else "NO_EARLY_EVIDENCE")
 if not evidence_at or not first_price:return {"eligibility_status":status,"first_eligibility_evidence_at":evidence_at,"max_gain_after_first_early_pct":None}
 try:t=dt.datetime.fromisoformat(str(evidence_at).replace("Z","+00:00")).timestamp()*1000
 except Exception:return {"eligibility_status":status,"first_eligibility_evidence_at":evidence_at,"max_gain_after_first_early_pct":None}
 after=[float(x[2]) for x in candles if x[0]>=t]
 if not after:return {"eligibility_status":status,"first_eligibility_evidence_at":evidence_at,"max_gain_after_first_early_pct":None}
 high=max(after);high_time=iso(max((x for x in candles if x[0]>=t),key=lambda x:float(x[2]))[0])
 return {"eligibility_status":status,"first_eligibility_evidence_at":evidence_at,"max_gain_after_first_early_pct":round((high/float(first_price)-1)*100,4),"post_early_high_at_utc":high_time}

def universe_time(scan,asset):
 # Current durable scan is authoritative for membership; historical first-seen
 # is UNKNOWN until universe history is accumulated. Never fabricate it.
 return scan.get("as_of_utc") if asset in (scan.get("coins") or {}) else None
def main():
 start,now,local_date=today_bounds(); scan=load(SCAN); uhist=load(UNIVERSE_HISTORY); hist=load(EARLY); v1=load(V1); v2=load(V2); review=load(REVIEW)
 excluded=set((((scan.get("venue_status") or {}).get("binance") or {}).get("excluded_bstocks") or []))
 candidates=[]
 for a,c in (scan.get("coins") or {}).items():
  if a in excluded or not c.get("pairs"):continue
  sym=c["pairs"][0].get("pair")
  if not sym or not sym.endswith("USDT"):continue
  candidates.append((float(c.get("change_24h_pct") or -999),a,sym))
 # Exact daily replay: inspect every crypto Universe pair. Bounded concurrency keeps
 # wall-clock time practical without the old top-40 prefilter blind spot.
 stats=[]
 def fetch_one(item):
  _,a,sym=item
  try:return a,sym,day_stats(sym,start,now)
  except Exception:return a,sym,None
 with ThreadPoolExecutor(max_workers=int(os.getenv("HUNTER_REPLAY_WORKERS","12"))) as ex:
  futs=[ex.submit(fetch_one,x) for x in candidates]
  for f in as_completed(futs):
   a,sym,s=f.result()
   if s:stats.append((s["day_high_gain_pct"],a,sym,s))
 stats.sort(reverse=True); rows=[]
 by_review={x.get("asset"):x for x in review.get("candidates",[]) if x.get("asset")}
 for _,a,sym,s in stats[:TOP_N]:
  s=dict(s)
  u=(uhist.get("assets") or {}).get(a) or {}
  h=(hist.get("assets") or {}).get(a) or {}
  v1buy=first_event(v1,a,{"SHADOW_V1_BUY","SHADOW_BUY"})
  v2buy=first_event(v2,a,{"SHADOW_V2_BUY","SHADOW_BUY"})
  vd=[x for x in v2.get("deferred_buy_opportunities",[]) if x.get("asset")==a]
  vd.sort(key=lambda x:x.get("at_utc") or ""); deferred=vd[0] if vd else None
  r=by_review.get(a) or {}
  pd=post_detection(s,h,v1buy);s.pop("_candles",None)
  rows.append({"rank":len(rows)+1,"asset":a,"pair":sym,"day":s,
   "universe":{"present":a in (scan.get("coins") or {}),"first_seen_at_utc":u.get("first_seen_at_utc"),"first_seen_price":u.get("first_seen_price"),"first_generation_id":u.get("first_generation_id"),"current_scan_seen_at_utc":universe_time(scan,a),"status":"RECORDED" if u.get("first_seen_at_utc") else ("HISTORICAL_FIRST_SEEN_UNKNOWN" if a in (scan.get("coins") or {}) else "NOT_PRESENT")},
   "early":{"ever_recorded":bool(h),"first_at_utc":h.get("first_early_at_utc"),"first_price":h.get("first_early_price"),"first_generation_id":h.get("first_generation_id")},
   "v1":{"buy_generated":bool(v1buy),"first_buy":v1buy},
   "capital_review":{"current_trade_action":r.get("trade_action"),"entry_stage":r.get("entry_stage"),"reference_price":r.get("reference_price"),"estimated_rr":(r.get("execution_scenario") or {}).get("estimated_rr")},
   "v2":{"buy_generated":bool(v2buy),"first_buy":v2buy,"first_deferred":deferred,"first_decision":first_decision(v2,a),
         "why_not_bought":None if v2buy else ((deferred or {}).get("reason") or ((first_decision(v2,a) or {}).get("reasons") or ["NO_DURABLE_V2_DECISION_EVIDENCE"]))},
   "post_detection":pd})
 out={"schema":"hunter_missed_opportunity_replay_v1","generated_at_utc":now.isoformat(),"date_local":local_date,"timezone":"Asia/Ho_Chi_Minh","mode":"READ_ONLY_NO_TRADING","top_n":TOP_N,
  "selection":"All current Binance crypto-only USDT Universe assets ranked by Ho Chi Minh local-day open-to-intraday-high gain; no current-24h top-N prefilter",
  "limitations":["Universe first-seen is durable from hunter-universe-history deployment onward; older membership is never backdated.","EARLY first-seen predates this module only when durable hunter-early-signal-history evidence exists."],"rows":rows}
 OUT.write_text(json.dumps(out,ensure_ascii=False,indent=2)+"\n")
 print(json.dumps({"date":out["date_local"],"leaders":[{"asset":x["asset"],"day_high_gain_pct":x["day"]["day_high_gain_pct"],"first_early":x["early"]["first_at_utc"],"v1":x["v1"]["buy_generated"],"v2":x["v2"]["buy_generated"],"why_not":x["v2"]["why_not_bought"]} for x in rows]},ensure_ascii=False))
if __name__=="__main__":main()
