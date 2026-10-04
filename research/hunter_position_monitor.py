#!/usr/bin/env python3
"""5-minute V1/V2 existing-position manager. No discovery, no new entries, no real orders."""
import datetime as dt,json,math,os,pathlib,urllib.request
from research import hunter_shadow_trader_v2 as eng
from research import hunter_shadow_trader as v1
from research import hunter_early_signals as signals
from research import hunter_liquidity_probe as books
from research.hunter_policy import C,LANES,VERSION,stamp
from concurrent.futures import ThreadPoolExecutor,as_completed
ROOT=pathlib.Path("research/results");BN=os.getenv("HUNTER_BINANCE_API","https://data-api.binance.vision")
V1=ROOT/"hunter-shadow-portfolio.json";V2=ROOT/"hunter-shadow-v2-portfolio.json";OUT=ROOT/"hunter-position-monitor.json"
V1_SUMMARY=ROOT/"hunter-shadow-summary.json";V2_SUMMARY=ROOT/"hunter-shadow-v2-summary.json";V1_GUARD=ROOT/"hunter-shadow-v1-overfilter-guard.json";V2_GUARD=ROOT/"hunter-shadow-v2-overfilter-guard.json"
REVIEW=ROOT/"hunter-tactical-capital-review.json";LIQ=ROOT/"hunter-liquidity-probe.json";SUPPLY=ROOT/"hunter-tactical-supply-risk.json";UNIVERSE=ROOT/"hunter-cex-universe-run.json"
def load(p):
 try:return json.loads(p.read_text())
 except (OSError,ValueError):return {}
def get(url):
 req=urllib.request.Request(url,headers={"User-Agent":"hunter-position-monitor/3.0","Accept":"application/json"})
 with urllib.request.urlopen(req,timeout=25) as r:return json.load(r)
def assets(states):
 # Observe open positions plus closed positions whose 72h opportunity window is
 # incomplete. This is observation-only and cannot create entries or alter exits.
 return sorted({p.get("asset") for s in states for p in ((s.get("open_positions") or [])+(s.get("closed_positions") or []))
                if p.get("asset") and (not p.get("closed_at_utc") or not p.get("observation_complete"))})
def batch_market(wanted):
 rows=get(BN+"/api/v3/ticker/24hr");by={x.get("symbol"):x for x in rows if isinstance(x,dict)};out={}
 for a in sorted(set(wanted)|{"BTC"}):
  x=by.get(a+"USDT") or {}
  try:p=float(x.get("lastPrice"));ch=float(x.get("priceChangePercent"))
  except (TypeError,ValueError):continue
  if math.isfinite(p) and p>0:out[a]={"reference_price":p,"change_24h_pct":ch}
 return out
def v1_review():
 try:
  _,review=v1.load_early_into_review();return review
 except (OSError,ValueError,KeyError):return load(REVIEW)
def crypto_exclusions():
 u=load(UNIVERSE);xs=(((u.get("venue_status") or {}).get("binance") or {}).get("excluded_bstocks") or [])
 if not xs: raise RuntimeError("CRYPTO_SCOPE_BSTOCK_CLASSIFICATION_MISSING")
 return set(xs)
def configure_lane(v1_mode):
 if v1_mode:
  v1.configure_v1();return
 eng.STATE=V2;eng.SUMMARY=V2_SUMMARY;eng.GUARD=V2_GUARD;eng.CAPITAL_POOL_USDT=LANES["V2"]["capital_pool_usdt"];eng.ENTRY_MODE=LANES["V2"]["entry_mode"];eng.DISCOVERY_MIN_SCORE=LANES["V2"]["discovery_min_score"];eng.DISCOVERY_MIN_INDEPENDENT=C["DISCOVERY_MIN_INDEPENDENT"];eng.STRATEGY_ID=LANES["V2"]["strategy"];eng.ID_PREFIX="SHV2";eng.EVENT_PREFIX="SHADOW_V2"
def run_lane(path,label,market,review,liq,supply,now,v1_mode=False,excluded=None):
 state=load(path);excluded=set(excluded or [])
 quarantine=eng.quarantine_non_crypto_history(state,excluded)
 before={p.get("asset"):len(p.get("tranches") or []) for p in state.get("open_positions") or []}
 configure_lane(v1_mode)
 scan={"coins":market};btc=(market.get("BTC") or {}).get("reference_price")
 eng.manage_existing_positions(state,scan,review,liq,supply,now,btc)
 # Closed positions remain observation subjects through SELL+72h. Live marks may
 # extend the all-time opportunity peak; exact horizon peaks are reconstructed by
 # the bounded historical backfill, never inferred from a late current price.
 for pos in state.get("closed_positions") or []:
  if pos.get("observation_complete"):continue
  p=(market.get(pos.get("asset")) or {}).get("reference_price")
  if p:eng.update_post_exit(pos,p,now)
  eng.backfill_opportunity_history(pos,now)
 if len(state.get("decisions") or [])>eng.MAX_DECISION_HISTORY:
  n=len(state["decisions"])-eng.MAX_DECISION_HISTORY;state["decision_history_truncated"]=int(state.get("decision_history_truncated") or 0)+n;state["decisions"]=state["decisions"][-eng.MAX_DECISION_HISTORY:]
 if len(state.get("events") or [])>eng.MAX_EVENT_HISTORY:
  n=len(state["events"])-eng.MAX_EVENT_HISTORY;state["event_history_truncated"]=int(state.get("event_history_truncated") or 0)+n;state["events"]=state["events"][-eng.MAX_EVENT_HISTORY:]
 after={p.get("asset"):len(p.get("tranches") or []) for p in state.get("open_positions") or []}
 added=sorted(a for a,n in after.items() if n>before.get(a,0));closed=sorted(set(before)-set(after))
 state["policy_version"]=VERSION;state["updated_at_utc"]=now.isoformat();eng.atomic_json_write(path,state)
 summary_path=(V1_SUMMARY if path==V1 else V2_SUMMARY if path==V2 else path.with_name(path.stem+"-summary.json"))
 guard_path=V1_GUARD if v1_mode else V2_GUARD
 eng.atomic_json_write(summary_path,eng.build_summary(state,now,guard_status=(load(guard_path).get("status") or "NORMAL")))
 return {"lane":label,"open":len(after),"added":added,"closed":closed,"quarantined_non_crypto":quarantine["assets"],"quarantine_counts":{k:v for k,v in quarantine.items() if k!="assets"}}
def refresh_management_evidence(states,market,review,liq,now):
 """Refresh bounded rotating holdings, including names no longer EARLY. No entry path."""
 import copy
 review=copy.deepcopy(review);liq=copy.deepcopy(liq)
 wanted=sorted({p.get("asset") for st in states for p in st.get("open_positions") or [] if p.get("asset")})
 previous=load(OUT);cursor=int(previous.get("evidence_refresh_cursor") or 0)
 count=min(C["MONITOR_EVIDENCE_BATCH"],len(wanted))
 selected=[wanted[(cursor+i)%len(wanted)] for i in range(count)] if wanted else []
 pairs=[a+"USDT" for a in selected]+["BTCUSDT"]
 generation="MONITOR_"+now.strftime("%Y%m%dT%H%M%S%fZ")
 current={};failures={}
 try:
  r1=signals.rolling(pairs,"1h");r4=signals.rolling(pairs,"4h");micro=signals.micro(pairs)
  for a in selected:
   row=signals.score_row(a+"USDT",a,r1,r4,r1.get("BTCUSDT"),r4.get("BTCUSDT"),micro)
   if row:current[a]=row
   else:failures[a]="SIGNAL_REFRESH_FAILED"
 except Exception as exc:
  failures.update({a:"SIGNAL_REFRESH_FAILED:"+type(exc).__name__ for a in selected})
 by={x.get("asset"):x for x in review.get("candidates") or []}
 def book(a):
  raw=books.live_fetch(books.BN+"/api/v3/depth?symbol="+a+"USDT&limit=100")
  row=books.measure(raw,dt.datetime.now(dt.timezone.utc));row["execution_scenarios"]={}
  for amount in (1000,2000,3000,4000):
   try:row["execution_scenarios"][str(amount)]=books.estimate(raw,amount)
   except ValueError:pass
  return row
 with ThreadPoolExecutor(max_workers=16) as executor:
  futures={executor.submit(book,a):a for a in selected if a in current}
  for future in as_completed(futures):
   a=futures[future]
   try:
    snapshot=future.result();liq.setdefault("snapshots",{})[a]=snapshot
    c=by.setdefault(a,{"asset":a,"blockers":[]})
    c["signal"]=current[a];c["signal_evidence"]=stamp(a,generation,now.isoformat())
    c["execution_scenario"]=snapshot["execution_scenarios"].get("3000",{})
    # Old microstructure failures must be replaced by this fresh snapshot.
    market_flags={"LIVE_ORDERBOOK_MISSING","LIVE_ORDERBOOK_STALE","LIVE_ORDERBOOK_INVALID","SPREAD_EXCEEDS_50_BPS","DEPTH_BELOW_30K_USDT","BTC_RELATIVE_SIGNAL_MISSING","SIGNAL_EVIDENCE_STALE"}
    c["blockers"]=[b for b in c.get("blockers") or [] if b not in market_flags]
    c["management_only"]=True
   except Exception as exc:failures[a]="BOOK_REFRESH_FAILED:"+type(exc).__name__
 for a in failures:
  c=by.setdefault(a,{"asset":a,"blockers":[]});c["signal_evidence"]={};c["management_refresh_error"]=failures[a]
 review["candidates"]=list(by.values())
 return review,liq,{"generation_id":generation,"observed_at_utc":now.isoformat(),"attempted":selected,"failures":failures,
                   "evidence_refresh_cursor":(cursor+count)%len(wanted) if wanted else 0,"policy_version":VERSION}

def main():
 states=[load(V1),load(V2)];wanted=assets(states);now=dt.datetime.now(dt.timezone.utc)
 if not wanted:
  eng.atomic_json_write(OUT,{"as_of_utc":now.isoformat(),"assets":[],"status":"NO_OPEN_POSITIONS","scope":"EXISTING_POSITIONS_ONLY","new_entry_enabled":False})
  print(json.dumps({"assets":[],"status":"NO_OPEN_POSITIONS"}))
  return
 market=batch_market(wanted);missing=sorted(set(wanted)-set(market))
 if missing:raise SystemExit("FAST_MONITOR_MARKET_DATA_MISSING "+",".join(missing))
 liq=load(LIQ);supply=load(SUPPLY);review=load(REVIEW);excluded=crypto_exclusions()
 review,liq,refresh=refresh_management_evidence(states,market,review,liq,now)
 results=[run_lane(V1,"SHADOW_V1",market,review,liq,supply,now,True,excluded),run_lane(V2,"SHADOW_V2",market,review,liq,supply,now,False,excluded)]
 eng.atomic_json_write(OUT,{"as_of_utc":now.isoformat(),"assets":wanted,"batch_endpoint":"/api/v3/ticker/24hr","scope":"EXISTING_POSITIONS_ONLY","new_entry_enabled":False,"shared_manager":"hunter_shadow_trader_v2.manage_existing_positions","results":results,"evidence_refresh":refresh,"evidence_refresh_cursor":refresh["evidence_refresh_cursor"],"policy_version":VERSION})
 print(json.dumps({"assets":wanted,"results":results},ensure_ascii=False))
if __name__=="__main__":main()
