#!/usr/bin/env python3
"""Hunter shadow v2 capital-decision engine. Forward simulation only; never places exchange orders."""
import datetime as dt,json,math,pathlib,uuid
ROOT=pathlib.Path("research/results")
SCAN=ROOT/"hunter-cex-universe-run.json"; REVIEW=ROOT/"hunter-tactical-capital-review.json"
LIQ=ROOT/"hunter-liquidity-probe.json"; SUPPLY=ROOT/"hunter-tactical-supply-risk.json"
STATE=ROOT/"hunter-shadow-v2-portfolio.json"; SUMMARY=ROOT/"hunter-shadow-v2-summary.json"; GUARD=ROOT/"hunter-shadow-v2-overfilter-guard.json"
FEE_BPS=10.; TRANCHES=(1000.,1000.,1000.); REVIEW_HOURS=(24.,48.,72.); MAX_OPEN=3
MIN_RR=1.5; MAX_SPREAD_BPS=50.; MIN_DEPTH_USDT=30000.; MAX_SLIP_BPS=75.; TARGET=8.
PROTECT_ARM_PCT=2.; GIVEBACK_MAX_PCT=2.; MIN_PROTECTED_NET_PCT=.35
MAX_CHASE_24H_PCT=20.; MAX_CHASE_FROM_DISCOVERY_PCT=12.; MIN_CHASE_RR=2.0; MIN_CHASE_REL_1H=1.5; MIN_CHASE_REL_4H=2.5
OVERFILTER_ZERO_BUY_CYCLES=3; OVERFILTER_LOOKBACK=12; OVERFILTER_MISSED_MOVE_PCT=8.; OVERFILTER_MIN_SAFE_MISSES=2

def load(p,d=None):
 try:return json.loads(p.read_text())
 except (OSError,ValueError):return {} if d is None else d
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
 if reasons:return ("EXIT" if kind!="ENTRY" and "SEVERE_BTC_RELATIVE_BREAK" in reasons else "REJECT"),reasons,e
 if kind=="ADD":
  if pos is None or p is None:return "REJECT",["ADD_CONTEXT_MISSING"],e
  first=pos["tranches"][0]["price"]; dd=(p/first-1)*100; need=(-4.,-8.)[len(pos["tranches"])-1]
  if dd>need:return "HOLD",[f"ADD_PRICE_NOT_FAVORABLE_{need}"],e
  return "ADD",["LOWER_PRICE","FULL_EVIDENCE_REVALIDATED","RR_ACCEPTABLE"],e
 return ("BUY" if kind=="ENTRY" else "HOLD"),["FULL_EVIDENCE_VALIDATED"],e
def weighted_entry(pos):
 n=sum(t["notional_usdt"] for t in pos["tranches"]);return sum(t["price"]*t["notional_usdt"] for t in pos["tranches"])/n
def total_notional(pos):return sum(t["notional_usdt"] for t in pos["tranches"])
def raw_return(pos,p):return (p/weighted_entry(pos)-1)*100
def net_pnl(pos,p):
 qty=sum(t["notional_usdt"]/(t["price"]*(1+(t.get("buy_slippage_bps",0)+FEE_BPS)/10000)) for t in pos["tranches"])
 return qty*p*(1-FEE_BPS/10000)-total_notional(pos)
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
 GUARD.write_text(json.dumps(guard,ensure_ascii=False,indent=2)+"\n");return guard
def main():
 now=dt.datetime.now(dt.timezone.utc);scan=load(SCAN);review=load(REVIEW);liq=load(LIQ);supply=load(SUPPLY)
 if not scan.get("binance_complete") or review.get("scan_generation_id")!=scan.get("generation_id"):raise SystemExit("V2_INPUT_GENERATION_MISMATCH")
 btc=price(scan,"BTC")
 if not btc:raise SystemExit("BTC_PRICE_MISSING")
 cm={c.get("asset"):c for c in review.get("candidates") or [] if c.get("asset")}
 state=load(STATE,{"schema":"hunter_shadow_v2_portfolio_v2","mode":"SIMULATION_ONLY_NO_REAL_ORDERS","open_positions":[],"closed_positions":[],"events":[],"decisions":[]})
 state.setdefault("open_positions",[]);state.setdefault("closed_positions",[]);state.setdefault("events",[]);state.setdefault("decisions",[])
 still=[]
 for pos in state["open_positions"]:
  p=price(scan,pos["asset"])
  if not p:record(state,pos,"HOLD",now,["CURRENT_PRICE_MISSING"],{},pos.get("last_price"));still.append(pos);continue
  c=cm.get(pos["asset"]);raw=raw_return(pos,p);pos["mfe_pct"]=round(max(pos.get("mfe_pct",0),raw),4);pos["mae_pct"]=round(min(pos.get("mae_pct",0),raw),4)
  pos["last_price"]=p;pos["last_marked_at_utc"]=now.isoformat();hours=(now-parse(pos["opened_at_utc"])).total_seconds()/3600;pos["holding_hours"]=round(hours,2)
  act,reasons,e=decision(c,scan,liq,supply,"ADD" if len(pos["tranches"])<3 else "HOLD",pos,p)
  if act=="ADD":add(pos,p,e,now);record(state,pos,"ADD",now,reasons,e,p);raw=raw_return(pos,p)
  elif act=="EXIT":
   pnl=net_pnl(pos,p);notion=total_notional(pos);br=(btc/pos["btc_entry_price"]-1)*100
   pos.update({"closed_at_utc":now.isoformat(),"exit_reference_price":p,"exit_reason":"THESIS_INVALIDATION",
    "weighted_entry_price":weighted_entry(pos),"total_notional_usdt":notion,"net_pnl_usdt":round(pnl,2),
    "net_return_pct":round(pnl/notion*100,4),"btc_return_pct":round(br,4),"btc_relative_return_pct":round(pnl/notion*100-br,4)})
   record(state,pos,"EXIT",now,reasons,e,p);state["closed_positions"].append(pos);continue
  else:record(state,pos,"HOLD",now,reasons,e,p)
  protection=profit_protection(pos,p)
  if protection["exit"]:
   pnl=net_pnl(pos,p);notion=total_notional(pos);br=(btc/pos["btc_entry_price"]-1)*100
   pos.update({"closed_at_utc":now.isoformat(),"exit_reference_price":p,"exit_reason":"PROFIT_PROTECTION",
    "weighted_entry_price":weighted_entry(pos),"total_notional_usdt":notion,"net_pnl_usdt":round(pnl,2),
    "net_return_pct":round(pnl/notion*100,4),"btc_return_pct":round(br,4),"btc_relative_return_pct":round(pnl/notion*100-br,4),
    "profit_protection":protection})
   record(state,pos,"EXIT",now,["PROFIT_PROTECTION_ARMED","GIVEBACK_OR_PROTECTED_FLOOR"],e,p);state["closed_positions"].append(pos);continue
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
    record(state,pos,"EXIT",now,["PROFIT_TARGET_REACHED","RELATIVE_MOMENTUM_NOT_STRONG_ENOUGH_TO_RUN"],e,p);state["closed_positions"].append(pos);continue
  still.append(pos)
 state["open_positions"]=still;open_assets={x["asset"] for x in still};slots=max(0,MAX_OPEN-len(still));buy_count=0
 ranked=sorted(review.get("candidates") or [],key=lambda c:finite(sig(c).get("score")) or 0,reverse=True)
 for c in ranked:
  if slots<=0:break
  a=c.get("asset")
  if not a or a in open_assets:continue
  act,reasons,e=decision(c,scan,liq,supply,"ENTRY");p=price(scan,a)
  if act!="BUY":
   dummy={"asset":a,"tranches":[]};record(state,dummy,"REJECT",now,reasons,e,p);continue
  pos={"shadow_id":"SHV2-"+now.strftime("%Y%m%dT%H%M%S")+"-"+a+"-"+uuid.uuid4().hex[:6],"asset":a,"opened_at_utc":now.isoformat(),
   "scan_generation_id":scan["generation_id"],"btc_entry_price":btc,"tranches":[],"mfe_pct":0.,"mae_pct":0.,"last_price":p,
   "last_marked_at_utc":now.isoformat(),"capital_authority":"NONE_SHADOW_ONLY"}
  add(pos,p,e,now);record(state,pos,"BUY",now,reasons,e,p);state["open_positions"].append(pos);open_assets.add(a);slots-=1;buy_count+=1
 guard=update_overfilter_guard(state,scan,review,liq,supply,now,buy_count)
 state["updated_at_utc"]=now.isoformat();state["last_cycle_generation_id"]=scan["generation_id"];state["schema"]="hunter_shadow_v2_portfolio_v2";state["overfilter_guard_status"]=guard["status"]
 closed=state["closed_positions"];gp=sum(max(0,x["net_pnl_usdt"]) for x in closed);gl=-sum(min(0,x["net_pnl_usdt"]) for x in closed)
 summary={"schema":"hunter_shadow_v2_summary_v2","as_of_utc":now.isoformat(),"mode":"SIMULATION_ONLY_NO_REAL_ORDERS",
  "strategy":"CAPITAL_DECISION_ENGINE_V2","open_positions":len(state["open_positions"]),"closed_positions":len(closed),
  "net_pnl_usdt":round(sum(x["net_pnl_usdt"] for x in closed),2),"profit_factor":round(gp/gl,3) if gl else ("INF" if gp else None),
  "policy":{"tranches_usdt":list(TRANCHES),"price_only_stop_loss":False,"time_exit_enabled":False,"time_review_hours":list(REVIEW_HOURS),
   "entry_and_add_require_full_revalidation":True,"fail_closed_on_missing_candidate_evidence":True,"min_estimated_rr":MIN_RR,
   "max_spread_bps":MAX_SPREAD_BPS,"min_depth_2pct_usdt":MIN_DEPTH_USDT,"max_buy_slippage_bps":MAX_SLIP_BPS,"profit_review_trigger_pct":TARGET,"profit_target_is_forced_exit":False,"runner_requires_positive_1h_4h_relative_and_acceleration":True,
   "profit_protection":{"arm_mfe_pct":PROTECT_ARM_PCT,"max_giveback_pct":GIVEBACK_MAX_PCT,"min_protected_net_pct":MIN_PROTECTED_NET_PCT},
   "three_tranche_adds_are_conditional_not_mechanical":True,"max_open":MAX_OPEN,
   "overfilter_guard":{"zero_buy_cycles":OVERFILTER_ZERO_BUY_CYCLES,"missed_move_pct":OVERFILTER_MISSED_MOVE_PCT,"min_safe_misses":OVERFILTER_MIN_SAFE_MISSES,"status":guard["status"]}},
  "capital_authority":"NONE_SHADOW_ONLY"}
 STATE.write_text(json.dumps(state,ensure_ascii=False,indent=2)+"\n");SUMMARY.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n")
 print(json.dumps({"open":[x["asset"] for x in state["open_positions"]],"closed":len(closed),"decisions":len(state["decisions"]),"summary":summary},ensure_ascii=False))
if __name__=="__main__":main()
