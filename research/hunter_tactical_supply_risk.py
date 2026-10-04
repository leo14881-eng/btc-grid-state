#!/usr/bin/env python3
"""Dynamic tactical supply/dilution evidence for current Hunter candidates.

Uses the current generation's already-fetched CoinGecko supply observations
from forward research, but only after Identity Audit has corroborated the
asset. Static dated unlock evidence remains an override where available.

This is a short-horizon dilution screen, not a claim that every future unlock
is known and never a buy authorization.
"""
import datetime as dt,json,pathlib

ROOT=pathlib.Path("research/results")
OUT=ROOT/"hunter-tactical-supply-risk.json"
DOS=ROOT/"hunter-candidate-dossiers.json"
FORWARD=ROOT/"hunter-forward-research.json"
IDENTITY=ROOT/"hunter-identity-audit.json"

STATIC_EVIDENCE={
 "SAND":{"status":"FULLY_UNLOCKED","next_unlock":None,"source":"https://tokenomist.ai/the-sandbox/unlock-events","note":"vesting schedule ended 2025"},
 "AXS":{"status":"FULLY_UNLOCKED","next_unlock":None,"source":"https://app.blockworks.com/projects/axie-infinity/token-unlocks","note":"vesting completed 2026-01-07"},
 "YGG":{"status":"SCHEDULED_LOW_NEAR_TERM","next_unlock":"2026-10-27","next_unlock_total_supply_pct":0.34,"source":"https://www.coingecko.com/en/coins/yield-guild-games","note":"next scheduled release 3.42M YGG"},
 "NIGHT":{"status":"SCHEDULED_LOW_NEAR_TERM","next_unlock":"2026-10-07","next_unlock_total_supply_pct":0.1,"source":"https://app.tokenomics.com/tokenomics/midnight-network/unlocks","note":"next scheduled release ~19.4M NIGHT"}
}
FULLY_CIRCULATING_THRESHOLD=0.98

def read(p,d=None):
 try:return json.loads(p.read_text())
 except (OSError,ValueError):return {} if d is None else d

def identity_map(doc):
 rows=doc.get("assets") or doc.get("candidates") or doc.get("results") or []
 if isinstance(rows,dict): return rows
 return {(x.get("asset") or x.get("symbol")):x for x in rows if isinstance(x,dict)}

def main():
 now=dt.datetime.now(dt.timezone.utc)
 dossier=read(DOS,{})
 forward=read(FORWARD,{})
 identity=identity_map(read(IDENTITY,{}))
 active={d.get("asset") for d in dossier.get("dossiers") or [] if d.get("asset")}
 # Capital Review also evaluates EARLY/forward candidates that may not yet have
 # a dossier. Include every symbol researched in this generation.
 results=forward.get("research_results") or {}
 if isinstance(results,dict): active.update(results.keys())

 assets={}
 for sym in sorted(active):
  if sym in STATIC_EVIDENCE:
   row=dict(STATIC_EVIDENCE[sym]); pct=row.get("next_unlock_total_supply_pct",0) or 0
   row.update({"verified_at_utc":now.isoformat(),"evidence_method":"DATED_UNLOCK_EVIDENCE",
               "tactical_supply_risk_verified":row["status"]=="FULLY_UNLOCKED" or pct<=0.5})
   assets[sym]=row
   continue

  item=results.get(sym) if isinstance(results,dict) else None
  obs=(item or {}).get("observations") or {}
  ident=identity.get(sym) or {}
  circ=obs.get("circulating_supply"); total=obs.get("total_supply"); cg=obs.get("coingecko_id")
  row={"verified_at_utc":now.isoformat(),"status":"UNKNOWN",
       "evidence_method":"CURRENT_GENERATION_SUPPLY_SCREEN",
       "source":f"https://www.coingecko.com/en/coins/{cg}" if cg else None,
       "circulating_supply":circ,"total_supply":total,
       "identity_corroborated":bool(ident.get("capital_identity_pass")),
       "tactical_supply_risk_verified":False}
  try:
   ratio=float(circ)/float(total)
  except (TypeError,ValueError,ZeroDivisionError):
   ratio=None
  row["circulating_to_total_ratio"]=round(ratio,6) if ratio is not None else None

  # A current-generation supply observation can clear only the narrow tactical
  # dilution gate when identity is corroborated and >=98% of reported total
  # supply is already circulating. Lower ratios remain unknown/blocked because
  # this screen cannot prove the timing of future unlocks.
  if row["identity_corroborated"] and ratio is not None and ratio>=FULLY_CIRCULATING_THRESHOLD:
   row["status"]="CURRENT_SUPPLY_EFFECTIVELY_FULLY_CIRCULATING"
   row["tactical_supply_risk_verified"]=True
   row["note"]=">=98% of current reported total supply is circulating; clears tactical dilution screen only"
  elif ratio is not None:
   row["status"]="MATERIAL_NONCIRCULATING_SUPPLY_REQUIRES_UNLOCK_RESEARCH"
   row["note"]="Current circulating/total ratio is below tactical threshold; unlock timing still required"
  else:
   row["note"]="Current supply observation unavailable; unlock/supply research required"
  assets[sym]=row

 report={"schema":"hunter_tactical_supply_risk_v2","as_of_utc":now.isoformat(),
   "scan_generation_id":dossier.get("scan_generation_id") or forward.get("scan_generation_id"),
   "scan_as_of_utc":dossier.get("scan_as_of_utc") or forward.get("universe_scan_as_of_utc"),
   "fully_circulating_threshold":FULLY_CIRCULATING_THRESHOLD,
   "assets":assets,
   "policy":"DYNAMIC_CURRENT_GENERATION_SUPPLY_SCREEN__LOWER_CIRCULATION_REMAINS_FAIL_CLOSED__NO_AUTO_TRADE"}
 OUT.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
 print(json.dumps({"assets":len(assets),"verified":[s for s,x in assets.items() if x["tactical_supply_risk_verified"]],
   "needs_unlock_research":[s for s,x in assets.items() if not x["tactical_supply_risk_verified"]]},ensure_ascii=False))

if __name__=="__main__":main()
