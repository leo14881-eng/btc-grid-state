#!/usr/bin/env python3
"""Conservative self-optimizer for Hunter shadow rules. Never changes live-capital gates."""
import datetime as dt,json,pathlib
ROOT=pathlib.Path("research/results"); PORT=ROOT/"hunter-shadow-portfolio.json"; RULES=ROOT/"hunter-shadow-rules.json"; AUDIT=ROOT/"hunter-shadow-optimizer-audit.jsonl"
def metrics(xs):
 gp=sum(max(0,x.get("net_pnl_usdt",0)) for x in xs); gl=-sum(min(0,x.get("net_pnl_usdt",0)) for x in xs)
 return {"n":len(xs),"net_pnl_usdt":round(sum(x.get("net_pnl_usdt",0) for x in xs),2),"win_rate_pct":round(100*sum(x.get("net_return_pct",0)>0 for x in xs)/len(xs),2) if xs else None,"profit_factor":round(gp/gl,3) if gl else (999.0 if gp else None),"btc_alpha_pct":round(sum(x.get("btc_relative_return_pct",0) for x in xs)/len(xs),4) if xs else None,"worst_mae_pct":round(min([x.get("mae_pct",0) for x in xs]+[0]),4)}
def main():
 try:
  from research.hunter_policy import VERSION
 except ModuleNotFoundError as exc:
  if exc.name != 'research':raise
  from hunter_policy import VERSION
 # Optimizer is advisory only: no separate legacy schema may mutate active policy.
 port=json.loads(PORT.read_text()) if PORT.exists() else {}
 print(json.dumps({"status":"SHADOW_STRATEGY_FREEZE","policy_version":VERSION,"writes":0,"metrics":metrics(port.get("closed_positions") or [])}))
if __name__=="__main__":main()
