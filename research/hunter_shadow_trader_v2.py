#!/usr/bin/env python3
"""Hunter shadow v2 capital-decision engine. Forward simulation only; never places exchange orders."""
import datetime as dt,json,math,pathlib,uuid
ROOT=pathlib.Path("research/results")
SCAN=ROOT/"hunter-cex-universe-run.json"; REVIEW=ROOT/"hunter-tactical-capital-review.json"
LIQ=ROOT/"hunter-liquidity-probe.json"; SUPPLY=ROOT/"hunter-tactical-supply-risk.json"
STATE=ROOT/"hunter-shadow-v2-portfolio.json"; SUMMARY=ROOT/"hunter-shadow-v2-summary.json"
FEE_BPS=10.; TRANCHES=(1000.,1000.,1000.); REVIEW_HOURS=(24.,48.,72.); MAX_OPEN=3
MIN_RR=1.5; MAX_SPREAD_BPS=50.; MIN_DEPTH_USDT=30000.; MAX_SLIP_BPS=75.; TARGET=8.
PROTECT_ARM_PCT=2.; GIVEBACK_MAX_PCT=2.; MIN_PROTECTED_NET_PCT=.35

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
def decision(c,scan,liq,supply,kind="ENTRY",pos=None,p=None):
 if not c:return "EXIT" if kind!="ENTRY" else "REJECT",["CANDIDATE_EVIDENCE_MISSING"],{}
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
 if kind=="ENTRY" and ch is not None and ch>20:reasons.append("ANTI_CHASE_24H")
 if kind!="ENTRY" and e["btc_rel_1h"] is not None and e["btc_rel_4h"] is not None and e["btc_rel_1h"]<-2 and e["btc_rel_4h"]<-3:
  reasons.append("SEVERE_BTC_RELATIVE_BREAK")
 if reasons:return ("EXIT" if kind!="ENTRY" and ("CANDIDATE_EVIDENCE_MISSING" in reasons or "SEVERE_BTC_RELATIVE_BREAK" in reasons) else "REJECT"),reasons,e
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
 state["open_positions"]=still;open_assets={x["asset"] for x in still};slots=max(0,MAX_OPEN-len(still))
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
  add(pos,p,e,now);record(state,pos,"BUY",now,reasons,e,p);state["open_positions"].append(pos);open_assets.add(a);slots-=1
 state["updated_at_utc"]=now.isoformat();state["last_cycle_generation_id"]=scan["generation_id"];state["schema"]="hunter_shadow_v2_portfolio_v2"
 closed=state["closed_positions"];gp=sum(max(0,x["net_pnl_usdt"]) for x in closed);gl=-sum(min(0,x["net_pnl_usdt"]) for x in closed)
 summary={"schema":"hunter_shadow_v2_summary_v2","as_of_utc":now.isoformat(),"mode":"SIMULATION_ONLY_NO_REAL_ORDERS",
  "strategy":"CAPITAL_DECISION_ENGINE_V2","open_positions":len(state["open_positions"]),"closed_positions":len(closed),
  "net_pnl_usdt":round(sum(x["net_pnl_usdt"] for x in closed),2),"profit_factor":round(gp/gl,3) if gl else ("INF" if gp else None),
  "policy":{"tranches_usdt":list(TRANCHES),"price_only_stop_loss":False,"time_exit_enabled":False,"time_review_hours":list(REVIEW_HOURS),
   "entry_and_add_require_full_revalidation":True,"fail_closed_on_missing_candidate_evidence":True,"min_estimated_rr":MIN_RR,
   "max_spread_bps":MAX_SPREAD_BPS,"min_depth_2pct_usdt":MIN_DEPTH_USDT,"max_buy_slippage_bps":MAX_SLIP_BPS,"profit_review_trigger_pct":TARGET,"profit_target_is_forced_exit":False,"runner_requires_positive_1h_4h_relative_and_acceleration":True,
   "profit_protection":{"arm_mfe_pct":PROTECT_ARM_PCT,"max_giveback_pct":GIVEBACK_MAX_PCT,"min_protected_net_pct":MIN_PROTECTED_NET_PCT},
   "three_tranche_adds_are_conditional_not_mechanical":True,"max_open":MAX_OPEN},
  "capital_authority":"NONE_SHADOW_ONLY"}
 STATE.write_text(json.dumps(state,ensure_ascii=False,indent=2)+"\n");SUMMARY.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n")
 print(json.dumps({"open":[x["asset"] for x in state["open_positions"]],"closed":len(closed),"decisions":len(state["decisions"]),"summary":summary},ensure_ascii=False))
if __name__=="__main__":main()
