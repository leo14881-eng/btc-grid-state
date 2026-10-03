#!/usr/bin/env python3
"""Hunter shadow v2 capital-decision engine. Forward simulation only; never places exchange orders."""
import datetime as dt,json,math,os,pathlib,uuid
ROOT=pathlib.Path("research/results")
SCAN=ROOT/"hunter-cex-universe-run.json"; REVIEW=ROOT/"hunter-tactical-capital-review.json"
LIQ=ROOT/"hunter-liquidity-probe.json"; SUPPLY=ROOT/"hunter-tactical-supply-risk.json"
STATE=ROOT/"hunter-shadow-v2-portfolio.json"; SUMMARY=ROOT/"hunter-shadow-v2-summary.json"; GUARD=ROOT/"hunter-shadow-v2-overfilter-guard.json"; BYBIT=ROOT/"hunter-bybit-availability.json"
FEE_BPS=10.; TRANCHES=(1000.,1000.,1000.); REVIEW_HOURS=(1.,6.,24.,48.,72.); DISCOVERY_MIN_SCORE=6.; DISCOVERY_MIN_INDEPENDENT=2
MIN_RR=1.5; MAX_SPREAD_BPS=50.; MIN_DEPTH_USDT=30000.; MAX_SLIP_BPS=75.; TARGET=8.
PROTECT_ARM_PCT=2.; GIVEBACK_MAX_PCT=2.; MIN_PROTECTED_NET_PCT=.35
MAX_CHASE_24H_PCT=20.; MAX_CHASE_FROM_DISCOVERY_PCT=12.; MIN_CHASE_RR=2.0; MIN_CHASE_REL_1H=1.5; MIN_CHASE_REL_4H=2.5
OVERFILTER_ZERO_BUY_CYCLES=3; OVERFILTER_LOOKBACK=12; OVERFILTER_MISSED_MOVE_PCT=8.; OVERFILTER_MIN_SAFE_MISSES=2\nMAX_DECISION_HISTORY=1500
CAPITAL_POOL_USDT=20000.
ENTRY_MODE="EXECUTABLE"
STRATEGY_ID="CAPITAL_DECISION_ENGINE_V2"
ID_PREFIX="SHV2"
EVENT_PREFIX="SHADOW_V2"
SHADOW_FREEZE=os.getenv("HUNTER_SHADOW_FREEZE","1")!="0"

def entry_allowed(mode,broad,current_price,executable_action):
 return broad=="BUY" and current_price is not None and (mode=="DISCOVERY" or executable_action=="BUY")

def authoritative_entry_action(c, fallback_action):
 # V2 consumes Capital Review's final entry action. V1 discovery deliberately
 # bypasses it. Legacy fixtures without trade_action retain fallback behavior.
 if ENTRY_MODE!="EXECUTABLE": return fallback_action
 a=(c or {}).get("trade_action")
 return a if a in ("BUY","WAIT","REJECT","SYSTEM_BLOCKED") else fallback_action

def used_capital(state):
 return sum(total_notional(x) for x in state.get("open_positions",[]) if x.get("tranches"))
def capital_available(state,amount):
 return CAPITAL_POOL_USDT is None or used_capital(state)+amount<=CAPITAL_POOL_USDT+1e-9

def load(p,d=None):
 try:return json.loads(p.read_text())
 except (OSError,ValueError):return {} if d is None else d
def atomic_json_write(p,obj):
 # Never expose a partially-written portfolio/summary to a concurrent reader.
 tmp=p.with_suffix(p.suffix+".tmp")
 tmp.write_text(json.dumps(obj,ensure_ascii=False,indent=2)+"\n")
 json.loads(tmp.read_text())
 tmp.replace(p)
def finite(x):
 try:
  v=float(x);return v if math.isfinite(v) else None
 except (TypeError,ValueError):return None
def parse(s):return dt.datetime.fromisoformat(str(s).replace("Z","+00:00"))
def price(scan,a):return finite(((scan.get("coins") or {}).get(a) or {}).get("reference_price"))
def sig(c):return (c or {}).get("signal") or {}
def liq_for(liq,a):return (liq.get("snapshots") or {}).get(a) or {}
def supply_for(supply,a):return (supply.get("assets") or {}).get(a)
def hard_blockers(c):
 # Portfolio usage is intentionally ignored in shadow-only simulation; evidence/execution blockers are not.
 return [x for x in ((c or {}).get("blockers") or []) if x!="PORTFOLIO_USAGE_REQUIRES_CURRENT_INPUT"]
def evidence(c,liq,supply):
 a=(c or {}).get("asset"); s=sig(c); l=liq_for(liq,a); sr=supply_for(supply,a)
 ex=(c or {}).get("execution_scenario") or {}
 return {"asset":a,"score":finite(s.get("score")),"independent":int(s.get("independent_signal_count") or 0),
  "btc_rel_1h":finite(s.get("btc_relative_1h_pct")),"btc_rel_4h":finite(s.get("btc_relative_4h_pct")),
  "rel_accel":finite(s.get("relative_acceleration_pct")),"spread_bps":finite(l.get("spread_bps")),
  "bid_depth_2pct_usdt":finite(l.get("bid_depth_2pct_usdt")),"ask_depth_2pct_usdt":finite(l.get("ask_depth_2pct_usdt")),
  "buy_slippage_bps":finite(ex.get("buy_slippage_bps")),"estimated_rr":finite(ex.get("estimated_rr")),
  "supply_verified":bool(sr and sr.get("tactical_supply_risk_verified")),"supply_status":(sr or {}).get("status"),
  "blockers":hard_blockers(c)}
def bybit_channel(bybit,a):
 spot=bybit.get("spot") or {}; alpha=bybit.get("alpha") or {}; a=str(a or "").upper()
 spot_v=(a in set(spot.get("symbols") or [])) if str(spot.get("status") or "").startswith("OK") else None
 alpha_v=(a in set(alpha.get("symbols") or [])) if str(alpha.get("status") or "").startswith("OK") else None
 if spot_v is True:return {"channel":"BYBIT_SPOT","spot":True,"alpha":alpha_v}
 if alpha_v is True:return {"channel":"BYBIT_ALPHA","spot":spot_v,"alpha":True}
 if spot_v is False and alpha_v is False:return {"channel":"NOT_ON_BYBIT","spot":False,"alpha":False}
 return {"channel":"UNKNOWN","spot":spot_v,"alpha":alpha_v}

def discovery_decision(c):
 """Broad forward-sample gate. Execution/liquidity/supply evidence is recorded, not used to erase research samples."""
 if not c:return "REJECT",["CANDIDATE_MISSING"]
 e=sig(c); reasons=[]
 if (finite(e.get("score")) or 0)<DISCOVERY_MIN_SCORE:reasons.append("DISCOVERY_SCORE_WEAK")
 if int(e.get("independent_signal_count") or 0)<DISCOVERY_MIN_INDEPENDENT:reasons.append("DISCOVERY_INDEPENDENT_SIGNALS_WEAK")
 # V1 discovery is an experiment lane: missing identity evidence is a label, not
 # proof of a bad asset. Only explicit mismatch/invalid identity blocks discovery.
 # V2 remains fail-closed because decision() still consumes every hard blocker.
 identity_blockers=[x for x in hard_blockers(c) if "IDENTITY" in x or "CONTRACT" in x]
 fatal_tokens=("MISMATCH","INVALID","WRONG_ASSET","CONFLICT")
 blockers=[x for x in identity_blockers if any(t in x for t in fatal_tokens)]
 if blockers:reasons += ["BLOCKER:"+x for x in blockers]
 return ("BUY" if not reasons else "REJECT"),(reasons or ["BROAD_DISCOVERY_GATE_PASS"])

def market_shock(scan):
 btc=((scan.get("coins") or {}).get("BTC") or {}); b=finite(btc.get("change_24h_pct"))
 vals=[finite(x.get("change_24h_pct")) for x in (scan.get("coins") or {}).values()]
 vals=[x for x in vals if x is not None]; negative=(sum(x<0 for x in vals)/len(vals)) if vals else 0
 return bool(b is not None and b<=-2 and negative>=.60),{"btc_change_24h_pct":b,"negative_breadth":round(negative,4)}

def discovery_anchor(c):
 for k in ("first_discovery_price","first_price","discovery_price"):
  v=finite((c or {}).get(k))
  if v:return v
 return None
def decision(c,scan,liq,supply,kind="ENTRY",pos=None,p=None):
 if not c:
  # Dropping out of the current shortlist is not itself a thesis failure.
  # Existing shadow positions wait for fresh evidence instead of being force-sold.
  return ("HOLD" if kind!="ENTRY" else "REJECT"),["CANDIDATE_EVIDENCE_MISSING_REVIEW_ONLY"],{}
 e=evidence(c,liq,supply); reasons=[]
 if e["score"] is None or e["score"]<8:reasons.append("SCORE_WEAK")
 if e["independent"]<2:reasons.append("INSUFFICIENT_INDEPENDENT_SIGNALS")
 if e["btc_rel_1h"] is None or e["btc_rel_4h"] is None:reasons.append("BTC_RELATIVE_MISSING")
 elif e["btc_rel_1h"]<.8 and e["btc_rel_4h"]<1.5:reasons.append("BTC_RELATIVE_WEAK")
 if e["rel_accel"] is not None and e["rel_accel"]<-1:reasons.append("RELATIVE_MOMENTUM_DECELERATING")
 if e["spread_bps"] is None or e["spread_bps"]>MAX_SPREAD_BPS:reasons.append("SPREAD_UNACCEPTABLE")
 depths=[e["bid_depth_2pct_usdt"],e["ask_depth_2pct_usdt"]]
 if any(x is None or x<MIN_DEPTH_USDT for x in depths):reasons.append("DEPTH_INSUFFICIENT")
 if e["buy_slippage_bps"] is None or e["buy_slippage_bps"]>MAX_SLIP_BPS:reasons.append("SLIPPAGE_UNACCEPTABLE")
 if e["estimated_rr"] is None or e["estimated_rr"]<MIN_RR:reasons.append("RR_BELOW_MINIMUM")
 if not e["supply_verified"]:reasons.append("SUPPLY_RISK_UNVERIFIED")
 if e["blockers"]:reasons+=["BLOCKER:"+x for x in e["blockers"]]
 ch=finite(((scan.get("coins") or {}).get(e["asset"]) or {}).get("change_24h_pct"))
 if kind=="ENTRY":
  if ch is not None and ch>MAX_CHASE_24H_PCT:
   # A strong right-side continuation is allowed only when the higher entry price
   # is compensated by materially stronger evidence and remaining reward/risk.
   chase_ok=(e["estimated_rr"] is not None and e["estimated_rr"]>=MIN_CHASE_RR and
             e["btc_rel_1h"] is not None and e["btc_rel_1h"]>=MIN_CHASE_REL_1H and
             e["btc_rel_4h"] is not None and e["btc_rel_4h"]>=MIN_CHASE_REL_4H and
             e["rel_accel"] is not None and e["rel_accel"]>0)
   if not chase_ok:reasons.append("CHASE_NOT_COMPENSATED_BY_EDGE")
  anchor=discovery_anchor(c)
  cur=p if p is not None else finite(((scan.get("coins") or {}).get(e["asset"]) or {}).get("reference_price"))
  if anchor and cur:
   chase_from_discovery=(cur/anchor-1)*100
   if chase_from_discovery>MAX_CHASE_FROM_DISCOVERY_PCT and (e["estimated_rr"] is None or e["estimated_rr"]<MIN_CHASE_RR):
    reasons.append("TOO_FAR_ABOVE_DISCOVERY_FOR_REMAINING_RR")
 if kind!="ENTRY" and e["btc_rel_1h"] is not None and e["btc_rel_4h"] is not None and e["btc_rel_1h"]<-2 and e["btc_rel_4h"]<-3:
  reasons.append("SEVERE_BTC_RELATIVE_BREAK")
 if reasons:
  if kind=="ADD" and "SEVERE_BTC_RELATIVE_BREAK" not in reasons:return "HOLD",reasons,e
  return ("EXIT" if kind!="ENTRY" and "SEVERE_BTC_RELATIVE_BREAK" in reasons else "REJECT"),reasons,e
 if kind=="ADD":
  if pos is None or p is None:return "REJECT",["ADD_CONTEXT_MISSING"],e
  avg=weighted_entry(pos)
  if p>=avg:return "HOLD",["ADD_NOT_BELOW_CURRENT_AVERAGE"],e
  # Adds are attainable but never mechanical: better price + thesis still alive + stabilization/relative resilience.
  if e["score"] is None or e["score"]<DISCOVERY_MIN_SCORE:return "HOLD",["ADD_THESIS_SCORE_NOT_REVALIDATED"],e
  if e["independent"]<DISCOVERY_MIN_INDEPENDENT:return "HOLD",["ADD_THESIS_SIGNALS_NOT_REVALIDATED"],e
  stable=((e["rel_accel"] is not None and e["rel_accel"]>=-.5) or (e["btc_rel_1h"] is not None and e["btc_rel_1h"]>=0))
  if not stable:return "HOLD",["ADD_WAITING_FOR_STABILIZATION"],e
  improvement=(avg-p)/avg*100
  return "ADD",["BETTER_PRICE","THESIS_REVALIDATED","STABILIZATION_PRESENT",f"AVERAGE_COST_IMPROVEMENT_{improvement:.2f}PCT"],e
 return ("BUY" if kind=="ENTRY" else "HOLD"),["FULL_EVIDENCE_VALIDATED"],e
def weighted_entry(pos):
 n=sum(t["notional_usdt"] for t in pos["tranches"]);return sum(t["price"]*t["notional_usdt"] for t in pos["tranches"])/n
def total_notional(pos):return sum(t["notional_usdt"] for t in pos["tranches"])
def raw_return(pos,p):return (p/weighted_entry(pos)-1)*100
def net_pnl(pos,p):
 qty=sum(t["notional_usdt"]/(t["price"]*(1+(t.get("buy_slippage_bps",0)+FEE_BPS)/10000)) for t in pos["tranches"])
 return qty*p*(1-FEE_BPS/10000)-total_notional(pos)
def scenario_returns(pos,p):
 out={}
 for n in range(1,len(pos.get("tranches",[]))+1):
  q={**pos,"tranches":pos["tranches"][:n]}
  notion=total_notional(q); pnl=net_pnl(q,p)
  out[f"{n}_tranche"]={"notional_usdt":notion,"weighted_entry_price":weighted_entry(q),"net_pnl_usdt":round(pnl,2),"net_return_pct":round(pnl/notion*100,4)}
 return out

def exit_analysis(pos,p,reason):
 scenarios=scenario_returns(pos,p); final=scenarios.get(f"{len(pos.get('tranches',[]))}_tranche",{})
 net=final.get("net_return_pct",0)
 if net>=0:failure=None
 elif reason=="THESIS_INVALIDATION":failure="THESIS_OR_SELECTION_FAILURE"
 elif reason=="PROFIT_PROTECTION":failure="PROFIT_GIVEBACK_FAILURE"
 else:failure="EXIT_OR_SELECTION_REVIEW"
 return {"tranche_scenarios_at_exit":scenarios,"failure_attribution":failure,
  "post_exit_tracking":{"hours":list(REVIEW_HOURS),"max_rebound_from_exit_pct":0.,"potential_premature_exit":False,"marks":[]}}

def update_post_exit(pos,p,now):
 t=pos.get("post_exit_tracking")
 if not t or not pos.get("closed_at_utc") or not p:return
 hours=(now-parse(pos["closed_at_utc"])).total_seconds()/3600
 if hours<0 or hours>max(REVIEW_HOURS):return
 rebound=(p/(finite(pos.get("exit_reference_price")) or p)-1)*100
 t["max_rebound_from_exit_pct"]=round(max(finite(t.get("max_rebound_from_exit_pct")) or 0,rebound),4)
 t["potential_premature_exit"]=bool(t["max_rebound_from_exit_pct"]>=8)
 # Keep bounded forward marks near the requested review horizons.
 for target in REVIEW_HOURS:
  key=f"{int(target)}h"
  if hours>=target and not any(x.get("horizon")==key for x in t["marks"]):
   t["marks"].append({"horizon":key,"observed_hours":round(hours,2),"price":p,"rebound_from_exit_pct":round(rebound,4)})

def profit_protection(pos,p):
 raw=raw_return(pos,p); mfe=finite(pos.get("mfe_pct")) or 0.
 armed=mfe>=PROTECT_ARM_PCT
 giveback=max(0.,mfe-raw)
 # Once a real profit window existed, do not deliberately let a shadow winner become a loser.
 # Exit review is triggered either near breakeven after costs or after excessive giveback.
 protect_floor=MIN_PROTECTED_NET_PCT+(2*FEE_BPS)/100.
 return {"armed":armed,"raw_pct":raw,"mfe_pct":mfe,"giveback_pct":giveback,"protect_floor_pct":protect_floor,
  "exit":bool(armed and (raw<=protect_floor or giveback>=GIVEBACK_MAX_PCT))}

def record(state,pos,action,now,reasons,e,p):
 state["decisions"].append({"at":now.isoformat(),"shadow_id":pos.get("shadow_id"),"asset":pos["asset"],"action":action,
  "price":p,"reasons":reasons,"evidence":e,"tranches":len(pos.get("tranches",[])),"notional_usdt":total_notional(pos) if pos.get("tranches") else 0})
def trade_event(state,pos,action,now,p,reason=None,pnl=None):
 event={"type":EVENT_PREFIX+"_"+action,"at":now.isoformat(),"asset":pos["asset"],"shadow_id":pos.get("shadow_id"),"price":p,
  "tranches":len(pos.get("tranches",[])),"notional_usdt":total_notional(pos) if pos.get("tranches") else 0}
 if reason is not None:event["reason"]=reason
 if pnl is not None:event["net_pnl_usdt"]=round(pnl,2)
 state["events"].append(event)
def add(pos,p,e,now):
 i=len(pos["tranches"]);pos["tranches"].append({"tranche":i+1,"at":now.isoformat(),"price":p,"notional_usdt":TRANCHES[i],
  "buy_slippage_bps":e.get("buy_slippage_bps") or 0,"reason":"INITIAL" if i==0 else "LOWER_PRICE_FULL_REVALIDATION"})
def update_overfilter_guard(state,scan,review,liq,supply,now,buy_count):
 guard=load(GUARD,{"schema":"hunter_shadow_v2_overfilter_guard_v1","cycles":[],"status":"NORMAL"})
 safe_misses=[]
 for c in review.get("candidates") or []:
  a=c.get("asset"); p=price(scan,a)
  if not a or not p:continue
  act,reasons,e=decision(c,scan,liq,supply,"ENTRY",p=p)
  # Candidate passed hard market-safety evidence but was rejected by strategy selectivity.
  hard=[r for r in reasons if r.startswith("BLOCKER:") or r in ("SPREAD_UNACCEPTABLE","DEPTH_INSUFFICIENT","SLIPPAGE_UNACCEPTABLE","SUPPLY_RISK_UNVERIFIED")]
  ch=finite(((scan.get("coins") or {}).get(a) or {}).get("change_24h_pct"))
  if act!="BUY" and not hard and ch is not None and ch>=OVERFILTER_MISSED_MOVE_PCT:
   safe_misses.append({"asset":a,"change_24h_pct":ch,"reasons":reasons,"estimated_rr":e.get("estimated_rr")})
 cycles=guard.get("cycles",[]);cycles.append({"at_utc":now.isoformat(),"generation_id":scan.get("generation_id"),"buys":buy_count,"safe_misses":safe_misses})
 cycles=cycles[-OVERFILTER_LOOKBACK:];guard["cycles"]=cycles
 zero=0
 for x in reversed(cycles):
  if x.get("buys",0)==0:zero+=1
  else:break
 recent_misses={m["asset"] for x in cycles[-OVERFILTER_ZERO_BUY_CYCLES:] for m in x.get("safe_misses",[])}
 over=zero>=OVERFILTER_ZERO_BUY_CYCLES and len(recent_misses)>=OVERFILTER_MIN_SAFE_MISSES
 guard.update({"as_of_utc":now.isoformat(),"consecutive_zero_buy_cycles":zero,"recent_safe_missed_assets":sorted(recent_misses),
  "status":"OVER_FILTERING" if over else "NORMAL",
  "optimizer_action":"RELAX_ONE_SHADOW_DIMENSION_AND_AB_TEST" if over else "NONE",
  "live_capital_rules_changed":False,"capital_authority":"NONE_SHADOW_ONLY"})
 atomic_json_write(GUARD,guard);return guard
def main():
 if SHADOW_FREEZE:
  print(json.dumps({"status":"SHADOW_STRATEGY_FREEZE","strategy":STRATEGY_ID,"writes":0,"capital_pool_usdt":CAPITAL_POOL_USDT}))
  return
 now=dt.datetime.now(dt.timezone.utc);scan=load(SCAN);review=load(REVIEW);liq=load(LIQ);supply=load(SUPPLY);bybit=load(BYBIT,{})
 if not scan.get("binance_complete") or review.get("scan_generation_id")!=scan.get("generation_id"):raise SystemExit("V2_INPUT_GENERATION_MISMATCH")
 btc=price(scan,"BTC")
 if not btc:raise SystemExit("BTC_PRICE_MISSING")
 cm={c.get("asset"):c for c in review.get("candidates") or [] if c.get("asset")}
 state=load(STATE,{"schema":"hunter_shadow_v2_portfolio_v2","mode":"SIMULATION_ONLY_NO_REAL_ORDERS","open_positions":[],"closed_positions":[],"events":[],"decisions":[]})
 state.setdefault("open_positions",[]);state.setdefault("closed_positions",[]);state.setdefault("events",[]);state.setdefault("decisions",[])
 for old in state["closed_positions"]:update_post_exit(old,price(scan,old.get("asset")),now)
 still=[]
 for pos in state["open_positions"]:
  p=price(scan,pos["asset"])
  if not p:record(state,pos,"HOLD",now,["CURRENT_PRICE_MISSING"],{},pos.get("last_price"));still.append(pos);continue
  c=cm.get(pos["asset"]);raw=raw_return(pos,p);pos["mfe_pct"]=round(max(pos.get("mfe_pct",0),raw),4);pos["mae_pct"]=round(min(pos.get("mae_pct",0),raw),4)
  pos["last_price"]=p;pos["last_marked_at_utc"]=now.isoformat();hours=(now-parse(pos["opened_at_utc"])).total_seconds()/3600;pos["holding_hours"]=round(hours,2)
  act,reasons,e=decision(c,scan,liq,supply,"ADD" if len(pos["tranches"])<3 else "HOLD",pos,p)
  if act=="ADD":
   next_amount=TRANCHES[len(pos["tranches"])]
   if capital_available(state,next_amount):
    add(pos,p,e,now);record(state,pos,"ADD",now,reasons,e,p);trade_event(state,pos,"ADD",now,p,"LOWER_PRICE_FULL_REVALIDATION");raw=raw_return(pos,p)
   else:
    act="HOLD";reasons=["CAPITAL_POOL_FULL_ADD_DEFERRED"];record(state,pos,"HOLD",now,reasons,e,p)
  elif act=="EXIT":
   shock,shock_e=market_shock(scan)
   if shock:
    record(state,pos,"HOLD",now,["MARKET_SHOCK_REVIEW","DEFER_RELATIVE_BREAK_EXIT"],{**e,**shock_e},p);pos["market_shock_review"]=shock_e;still.append(pos);continue
   pnl=net_pnl(pos,p);notion=total_notional(pos);br=(btc/pos["btc_entry_price"]-1)*100
   pos.update({"closed_at_utc":now.isoformat(),"exit_reference_price":p,"exit_reason":"THESIS_INVALIDATION",
    "weighted_entry_price":weighted_entry(pos),"total_notional_usdt":notion,"net_pnl_usdt":round(pnl,2),
    "net_return_pct":round(pnl/notion*100,4),"btc_return_pct":round(br,4),"btc_relative_return_pct":round(pnl/notion*100-br,4)})
   pos.update(exit_analysis(pos,p,pos["exit_reason"]));record(state,pos,"EXIT",now,reasons,e,p);trade_event(state,pos,"SELL",now,p,pos["exit_reason"],pnl);state["closed_positions"].append(pos);continue
  else:record(state,pos,"HOLD",now,reasons,e,p)
  protection=profit_protection(pos,p)
  if protection["exit"]:
   pnl=net_pnl(pos,p);notion=total_notional(pos);br=(btc/pos["btc_entry_price"]-1)*100
   pos.update({"closed_at_utc":now.isoformat(),"exit_reference_price":p,"exit_reason":"PROFIT_PROTECTION",
    "weighted_entry_price":weighted_entry(pos),"total_notional_usdt":notion,"net_pnl_usdt":round(pnl,2),
    "net_return_pct":round(pnl/notion*100,4),"btc_return_pct":round(br,4),"btc_relative_return_pct":round(pnl/notion*100-br,4),
    "profit_protection":protection})
   pos.update(exit_analysis(pos,p,pos["exit_reason"]));record(state,pos,"EXIT",now,["PROFIT_PROTECTION_ARMED","GIVEBACK_OR_PROTECTED_FLOOR"],e,p);trade_event(state,pos,"SELL",now,p,pos["exit_reason"],pnl);state["closed_positions"].append(pos);continue
  if raw>=TARGET:
   r1=e.get("btc_rel_1h"); r4=e.get("btc_rel_4h"); accel=e.get("rel_accel")
   runner=act!="REJECT" and r1 is not None and r4 is not None and accel is not None and r1>0 and r4>0 and accel>0
   if runner:
    record(state,pos,"HOLD",now,["PROFIT_TARGET_REACHED_BUT_RELATIVE_MOMENTUM_STILL_STRONG","RUNNER_MODE"],e,p)
   else:
    pnl=net_pnl(pos,p);notion=total_notional(pos);br=(btc/pos["btc_entry_price"]-1)*100
    pos.update({"closed_at_utc":now.isoformat(),"exit_reference_price":p,"exit_reason":"PROFIT_REVIEW_MOMENTUM_FADED",
     "weighted_entry_price":weighted_entry(pos),"total_notional_usdt":notion,"net_pnl_usdt":round(pnl,2),
     "net_return_pct":round(pnl/notion*100,4),"btc_return_pct":round(br,4),"btc_relative_return_pct":round(pnl/notion*100-br,4)})
    pos.update(exit_analysis(pos,p,pos["exit_reason"]));record(state,pos,"EXIT",now,["PROFIT_TARGET_REACHED","RELATIVE_MOMENTUM_NOT_STRONG_ENOUGH_TO_RUN"],e,p);trade_event(state,pos,"SELL",now,p,pos["exit_reason"],pnl);state["closed_positions"].append(pos);continue
  still.append(pos)
 state["open_positions"]=still;open_assets={x["asset"] for x in still};buy_count=0
 ranked=sorted(review.get("candidates") or [],key=lambda c:finite(sig(c).get("score")) or 0,reverse=True)
 for c in ranked:
  a=c.get("asset")
  if not a or a in open_assets:continue
  broad,broad_reasons=discovery_decision(c);p=price(scan,a);fallback,reasons,e=decision(c,scan,liq,supply,"ENTRY")
  act=authoritative_entry_action(c,fallback)
  if not entry_allowed(ENTRY_MODE,broad,p,act):
   if broad!="BUY": reject_reasons=broad_reasons
   elif not p: reject_reasons=["CURRENT_PRICE_MISSING"]
   elif ENTRY_MODE=="EXECUTABLE" and act in ("WAIT","SYSTEM_BLOCKED"):
    reject_reasons=[act]+list(c.get("research_gaps") or [])+list(c.get("blockers") or [])
   else: reject_reasons=reasons
   dummy={"asset":a,"tranches":[]};record(state,dummy,act if act in ("WAIT","SYSTEM_BLOCKED") else "REJECT",now,reject_reasons,e,p);continue
  pos={"shadow_id":ID_PREFIX+"-"+now.strftime("%Y%m%dT%H%M%S")+"-"+a+"-"+uuid.uuid4().hex[:6],"asset":a,"opened_at_utc":now.isoformat(),
   "scan_generation_id":scan["generation_id"],"btc_entry_price":btc,"tranches":[],"mfe_pct":0.,"mae_pct":0.,"last_price":p,
   "last_marked_at_utc":now.isoformat(),"capital_authority":"NONE_SHADOW_ONLY",
   "discovery_gate":"BROAD_FORWARD_SAMPLE","execution_channel":bybit_channel(bybit,a),
   "executable_gate":{"pass":act=="BUY","reasons":reasons,"source":"CAPITAL_REVIEW_FINAL_ACTION","purpose":"SINGLE_AUTHORITATIVE_ENTRY_DECISION"}}
  if not capital_available(state,TRANCHES[0]):
   record(state,{"asset":a,"tranches":[]},"REJECT",now,["CAPITAL_POOL_FULL_ENTRY_DEFERRED"],e,p);continue
  add(pos,p,e,now);record(state,pos,"BUY",now,(broad_reasons if ENTRY_MODE=="DISCOVERY" else reasons),e,p);trade_event(state,pos,"BUY",now,p,("DISCOVERY_ENTRY" if ENTRY_MODE=="DISCOVERY" else "EXECUTABLE_ENTRY"));state["open_positions"].append(pos);open_assets.add(a);buy_count+=1
 guard=update_overfilter_guard(state,scan,review,liq,supply,now,buy_count)
 # Trade events and positions are durable audit history. High-frequency HOLD/REJECT\n # decisions are diagnostic only and must not make the authoritative portfolio grow forever.\n if len(state["decisions"])>MAX_DECISION_HISTORY:\n  state["decision_history_truncated"]=int(state.get("decision_history_truncated") or 0)+len(state["decisions"])-MAX_DECISION_HISTORY\n  state["decisions"]=state["decisions"][-MAX_DECISION_HISTORY:]\n state["updated_at_utc"]=now.isoformat();state["last_cycle_generation_id"]=scan["generation_id"];state["schema"]="hunter_shadow_v2_portfolio_v2";state["overfilter_guard_status"]=guard["status"]
 closed=state["closed_positions"];gp=sum(max(0,x["net_pnl_usdt"]) for x in closed);gl=-sum(min(0,x["net_pnl_usdt"]) for x in closed)
 summary={"schema":"hunter_shadow_v2_summary_v2","as_of_utc":now.isoformat(),"mode":"SIMULATION_ONLY_NO_REAL_ORDERS",
  "strategy":STRATEGY_ID,"open_positions":len(state["open_positions"]),"closed_positions":len(closed),
  "net_pnl_usdt":round(sum(x["net_pnl_usdt"] for x in closed),2),"profit_factor":round(gp/gl,3) if gl else ("INF" if gp else None),
  "policy":{"tranches_usdt":list(TRANCHES),"price_only_stop_loss":False,"time_exit_enabled":False,"time_review_hours":list(REVIEW_HOURS),
   "entry_mode":ENTRY_MODE,"entry_requires_full_execution_validation":ENTRY_MODE=="EXECUTABLE","add_requires_revalidation":True,"fail_closed_on_missing_candidate_evidence":True,"min_estimated_rr":MIN_RR,
   "max_spread_bps":MAX_SPREAD_BPS,"min_depth_2pct_usdt":MIN_DEPTH_USDT,"max_buy_slippage_bps":MAX_SLIP_BPS,"profit_review_trigger_pct":TARGET,"profit_target_is_forced_exit":False,"runner_requires_positive_1h_4h_relative_and_acceleration":True,
   "profit_protection":{"arm_mfe_pct":PROTECT_ARM_PCT,"max_giveback_pct":GIVEBACK_MAX_PCT,"min_protected_net_pct":MIN_PROTECTED_NET_PCT},
   "three_tranche_adds_are_conditional_not_mechanical":True,"capital_pool_usdt":CAPITAL_POOL_USDT,"max_open":None,"discovery_sample_cap":None,"first_tranche":("AFTER_RESEARCH_DISCOVERY_ADMISSION" if ENTRY_MODE=="DISCOVERY" else "ONLY_AFTER_FULL_EXECUTABLE_DECISION_GATE"),"bybit_channel_is_label_not_discovery_gate":True,"post_exit_tracking_hours":list(REVIEW_HOURS),"tranche_counterfactuals_at_exit":True,
   "overfilter_guard":{"zero_buy_cycles":OVERFILTER_ZERO_BUY_CYCLES,"missed_move_pct":OVERFILTER_MISSED_MOVE_PCT,"min_safe_misses":OVERFILTER_MIN_SAFE_MISSES,"status":guard["status"]}},
  "capital_authority":"NONE_SHADOW_ONLY"}
 atomic_json_write(STATE,state);atomic_json_write(SUMMARY,summary)
 print(json.dumps({"open":[x["asset"] for x in state["open_positions"]],"closed":len(closed),"decisions":len(state["decisions"]),"summary":summary},ensure_ascii=False))
if __name__=="__main__":main()
