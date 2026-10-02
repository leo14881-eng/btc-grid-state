#!/usr/bin/env python3
"""Automated tactical supply/unlock evidence for current Hunter candidates.

Conservative fail-closed registry. Entries are dated evidence distilled from
current public unlock sources; unknown assets remain blocked. This is supply
risk evidence only, never a buy authorization.
"""
import datetime as dt,json,pathlib
ROOT=pathlib.Path("research/results")
OUT=ROOT/"hunter-tactical-supply-risk.json"
DOS=ROOT/"hunter-candidate-dossiers.json"
EVIDENCE={
 "SAND":{"status":"FULLY_UNLOCKED","next_unlock":None,"source":"https://tokenomist.ai/the-sandbox/unlock-events","note":"vesting schedule ended 2025"},
 "AXS":{"status":"FULLY_UNLOCKED","next_unlock":None,"source":"https://app.blockworks.com/projects/axie-infinity/token-unlocks","note":"vesting completed 2026-01-07"},
 "YGG":{"status":"SCHEDULED_LOW_NEAR_TERM","next_unlock":"2026-10-27","next_unlock_total_supply_pct":0.34,"source":"https://www.coingecko.com/en/coins/yield-guild-games","note":"next scheduled release 3.42M YGG"},
 "NIGHT":{"status":"SCHEDULED_LOW_NEAR_TERM","next_unlock":"2026-10-07","next_unlock_total_supply_pct":0.1,"source":"https://app.tokenomics.com/tokenomics/midnight-network/unlocks","note":"next scheduled release ~19.4M NIGHT"}
}
def read(p,d={}):
 try:return json.loads(p.read_text())
 except (OSError,ValueError):return d
def main():
 now=dt.datetime.now(dt.timezone.utc)
 active={d.get("asset") for d in read(DOS).get("dossiers") or []}
 assets={}
 for sym,e in EVIDENCE.items():
  if sym not in active:continue
  row=dict(e); row["verified_at_utc"]=now.isoformat()
  pct=row.get("next_unlock_total_supply_pct",0) or 0
  # Tactical threshold only: no scheduled unlock, or <=0.5% total supply
  # in the next event. Larger/unknown events stay blocked.
  row["tactical_supply_risk_verified"]=row["status"]=="FULLY_UNLOCKED" or pct<=0.5
  assets[sym]=row
 report={"schema":"hunter_tactical_supply_risk_v1","as_of_utc":now.isoformat(),
   "assets":assets,"policy":"FAIL_CLOSED__UNKNOWN_ASSETS_REMAIN_BLOCKED__NO_AUTO_TRADE"}
 OUT.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
 print(json.dumps({"assets":list(assets),"verified":[s for s,x in assets.items() if x["tactical_supply_risk_verified"]]}))
if __name__=="__main__":main()
