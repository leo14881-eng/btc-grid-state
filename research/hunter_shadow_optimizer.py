#!/usr/bin/env python3
"""Conservative self-optimizer for Hunter shadow rules. Never changes live-capital gates."""
import datetime as dt,json,pathlib
ROOT=pathlib.Path("research/results"); PORT=ROOT/"hunter-shadow-portfolio.json"; RULES=ROOT/"hunter-shadow-rules.json"; AUDIT=ROOT/"hunter-shadow-optimizer-audit.jsonl"
DEFAULT={"schema":"hunter_shadow_rules_v1","active_version":"shadow-v1","generation":1,"rules":{"min_score":8.0,"min_independent_signals":2,"min_btc_relative_1h_pct":0.8,"min_btc_relative_4h_pct":1.5,"max_change_24h_pct":20.0,"max_buy_slippage_bps":75.0,"target_pct":8.0,"invalidation_pct":-6.0,"max_hold_hours":24.0,"max_open":3,"notional_usdt":1000.0},"status":"BASELINE","capital_authority":"NONE_SHADOW_ONLY"}
MIN_CLOSED=20; MIN_NEW=10
def load(p,d): 
 try:return json.loads(p.read_text())
 except (OSError,ValueError):return d
def metrics(xs):
 gp=sum(max(0,x.get("net_pnl_usdt",0)) for x in xs); gl=-sum(min(0,x.get("net_pnl_usdt",0)) for x in xs)
 return {"n":len(xs),"net_pnl_usdt":round(sum(x.get("net_pnl_usdt",0) for x in xs),2),"win_rate_pct":round(100*sum(x.get("net_return_pct",0)>0 for x in xs)/len(xs),2) if xs else None,"profit_factor":round(gp/gl,3) if gl else (999.0 if gp else None),"btc_alpha_pct":round(sum(x.get("btc_relative_return_pct",0) for x in xs)/len(xs),4) if xs else None,"worst_mae_pct":round(min([x.get("mae_pct",0) for x in xs]+[0]),4)}
def main():
 now=dt.datetime.now(dt.timezone.utc).isoformat(); rules=load(RULES,DEFAULT); port=load(PORT,{"closed_positions":[]}); xs=port.get("closed_positions",[])
 last_n=int(rules.get("optimized_at_closed_n",0)); m=metrics(xs); action="HOLD_INSUFFICIENT_NEW_FORWARD_EVIDENCE"; proposal=None
 if len(xs)>=MIN_CLOSED and len(xs)-last_n>=MIN_NEW:
  good=(m["net_pnl_usdt"]>0 and (m["profit_factor"] or 0)>=1.2 and (m["btc_alpha_pct"] or -999)>0 and (m["win_rate_pct"] or 0)>=45)
  bad=(m["net_pnl_usdt"]<0 or ((m["profit_factor"] or 0)<0.9 and len(xs)>=30))
  nr=dict(rules["rules"])
  if good:
   # Expand only one shadow dimension by 10%; forward data must validate the next generation.
   nr["min_score"]=round(max(6.0,nr["min_score"]*0.9),3); action="PROMOTE_NEW_SHADOW_GENERATION"
  elif bad:
   # Fail closed: tighten one dimension; live-capital rules remain untouched.
   nr["min_score"]=round(min(20.0,nr["min_score"]*1.1),3); action="ROLLBACK_TIGHTEN_SHADOW_GENERATION"
  else: action="HOLD_NO_STATISTICAL_EDGE"
  if action!="HOLD_NO_STATISTICAL_EDGE":
   proposal={"from":rules["active_version"],"to":f"shadow-v{int(rules.get('generation',1))+1}","change":{"min_score":[rules["rules"]["min_score"],nr["min_score"]]}}
   rules.update({"active_version":proposal["to"],"generation":int(rules.get("generation",1))+1,"rules":nr,"optimized_at_closed_n":len(xs),"last_optimizer_action":action,"last_optimizer_at_utc":now,"last_metrics":m,"status":"FORWARD_VALIDATION"})
 rules.setdefault("history",[]); 
 if proposal: rules["history"].append({"at_utc":now,"action":action,"proposal":proposal,"metrics":m})
 rules["capital_authority"]="NONE_SHADOW_ONLY"; RULES.write_text(json.dumps(rules,ensure_ascii=False,indent=2)+"\n")
 with AUDIT.open("a") as f:f.write(json.dumps({"at_utc":now,"action":action,"metrics":m,"proposal":proposal,"live_rules_changed":False})+"\n")
 print(json.dumps({"action":action,"metrics":m,"active_version":rules["active_version"]}))
if __name__=="__main__":main()
