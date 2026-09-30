#!/usr/bin/env python3
"""Fetch and preserve official-source evidence for unresolved Hunter candidates.
Facts are only emitted when an asset-specific official-source rule matches the
retrieved text. Unknowns remain unknown; this worker never invents valuations.
"""
import datetime as dt,json,pathlib,re,urllib.request
ROOT=pathlib.Path("research/results")
DOS=ROOT/"hunter-candidate-dossiers.json"
OUT=ROOT/"hunter-evidence-enrichment.json"
RULES={
 "DOLO":[
  ("https://docs.dolomite.io/dolo/token-mechanics.md",{
   "contract_address":r"Contract Address:\s*`(0x[a-fA-F0-9]{40})`",
   "fixed_supply":r"total fixed supply of ([0-9,]+) tokens",
   "inflation_start_rate_pct":r"year 4 at a \*\*([0-9.]+)% annual rate"}),
  ("https://docs.dolomite.io/dolo/distribution.md",{
   "tge_unlocked":r"Total unlocked at TGE:\s*([0-9,]+)",
   "tge_circulating_dolo":r"Total circulating DOLO at TGE:\s*([0-9,]+)"})],
 "AERO":[
  ("https://aerodrome.finance/docs",{
   "current_annualized_emissions_pct":r"current rate of AERO emissions is approximately ([0-9.]+)% annualized",
   "reported_total_supply":r"Total supply:\s*([0-9.]+[BM]) AERO",
   "reported_locked_supply":r"veAERO locked:\s*~([0-9.]+)% of supply"})]
}
def read(p,d):
 try:return json.loads(p.read_text())
 except (OSError,ValueError):return d
def fetch(url):
 req=urllib.request.Request(url,headers={"User-Agent":"hunter-official-evidence/1.0","Accept":"text/plain,text/html"})
 with urllib.request.urlopen(req,timeout=10) as r:return r.read().decode("utf-8","replace")
def main():
 now=dt.datetime.now(dt.timezone.utc).isoformat(); dos=read(DOS,{})
 active={x.get("asset") for x in dos.get("dossiers") or []}
 assets={}
 for sym,rules in RULES.items():
  if sym not in active:continue
  facts={};sources=[];errors=[]
  for url,patterns in rules:
   try:
    text=fetch(url); sources.append(url)
    for key,pat in patterns.items():
     m=re.search(pat,text,re.I|re.S)
     if m:facts[key]=m.group(1)
   except Exception as e:errors.append(type(e).__name__+":"+str(e)[:120])
  assets[sym]={"verified_at_utc":now,"official_sources_fetched":sources,
               "extracted_facts":facts,"errors":errors,
               "status":"OFFICIAL_SOURCE_FACTS_EXTRACTED" if facts else "OFFICIAL_SOURCE_FETCHED_NO_RULE_MATCH"}
 report={"schema":"hunter_evidence_enrichment_v1","as_of_utc":now,
         "scan_as_of_utc":dos.get("scan_as_of_utc"),"assets":assets,
         "policy":"Official-source extraction only; no inferred valuation or fabricated future supply."}
 OUT.write_text(json.dumps(report,indent=2,ensure_ascii=False)+"\n")
 print(json.dumps({"assets":len(assets),"facts":sum(len(x["extracted_facts"]) for x in assets.values())}))
if __name__=="__main__":main()
