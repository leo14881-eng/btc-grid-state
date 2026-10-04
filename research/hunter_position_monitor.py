#!/usr/bin/env python3
"""5-minute V1/V2 existing-position manager. No discovery, no new entries, no real orders."""
import datetime as dt,json,math,os,pathlib,urllib.request
from research import hunter_shadow_trader_v2 as eng
from research import hunter_shadow_trader as v1
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
def assets(states):return sorted({p.get("asset") for s in states for p in (s.get("open_positions") or []) if p.get("asset")})
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
 eng.STATE=V2;eng.SUMMARY=V2_SUMMARY;eng.GUARD=V2_GUARD;eng.CAPITAL_POOL_USDT=20000.;eng.ENTRY_MODE="EXECUTABLE";eng.DISCOVERY_MIN_SCORE=6;eng.DISCOVERY_MIN_INDEPENDENT=2;eng.STRATEGY_ID="CAPITAL_DECISION_ENGINE_V2";eng.ID_PREFIX="SHV2";eng.EVENT_PREFIX="SHADOW_V2"
def run_lane(path,label,market,review,liq,supply,now,v1_mode=False,excluded=None):
 state=load(path);excluded=set(excluded or [])
 quarantined=[p for p in (state.get("open_positions") or []) if p.get("asset") in excluded]
 if quarantined:
  state["open_positions"]=[p for p in state.get("open_positions") or [] if p.get("asset") not in excluded]
  state.setdefault("excluded_non_crypto_positions",[]).extend(quarantined)
 before={p.get("asset"):len(p.get("tranches") or []) for p in state.get("open_positions") or []}
 configure_lane(v1_mode)
 scan={"coins":market};btc=(market.get("BTC") or {}).get("reference_price")
 eng.manage_existing_positions(state,scan,review,liq,supply,now,btc)
 after={p.get("asset"):len(p.get("tranches") or []) for p in state.get("open_positions") or []}
 added=sorted(a for a,n in after.items() if n>before.get(a,0));closed=sorted(set(before)-set(after))
 state["updated_at_utc"]=now.isoformat();eng.atomic_json_write(path,state)
 summary_path=V1_SUMMARY if v1_mode else V2_SUMMARY
 guard_path=V1_GUARD if v1_mode else V2_GUARD
 eng.atomic_json_write(summary_path,eng.build_summary(state,now,guard_status=(load(guard_path).get("status") or "NORMAL")))
 return {"lane":label,"open":len(after),"added":added,"closed":closed,"quarantined_non_crypto":sorted(p.get("asset") for p in quarantined)}
def main():
 states=[load(V1),load(V2)];wanted=assets(states);now=dt.datetime.now(dt.timezone.utc)
 if not wanted:
  eng.atomic_json_write(OUT,{"as_of_utc":now.isoformat(),"assets":[],"status":"NO_OPEN_POSITIONS","scope":"EXISTING_POSITIONS_ONLY","new_entry_enabled":False})
  print(json.dumps({"assets":[],"status":"NO_OPEN_POSITIONS"}))
  return
 market=batch_market(wanted);missing=sorted(set(wanted)-set(market))
 if missing:raise SystemExit("FAST_MONITOR_MARKET_DATA_MISSING "+",".join(missing))
 liq=load(LIQ);supply=load(SUPPLY);review=load(REVIEW);excluded=crypto_exclusions()
 results=[run_lane(V1,"SHADOW_V1",market,v1_review(),liq,supply,now,True,excluded),run_lane(V2,"SHADOW_V2",market,review,liq,supply,now,False,excluded)]
 eng.atomic_json_write(OUT,{"as_of_utc":now.isoformat(),"assets":wanted,"batch_endpoint":"/api/v3/ticker/24hr","scope":"EXISTING_POSITIONS_ONLY","new_entry_enabled":False,"shared_manager":"hunter_shadow_trader_v2.manage_existing_positions","results":results})
 print(json.dumps({"assets":wanted,"results":results},ensure_ascii=False))
if __name__=="__main__":main()
