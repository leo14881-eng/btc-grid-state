#!/usr/bin/env python3
"""Stock Shadow fundamental observer. Observation-only; never changes BUY/ADD/SELL."""
import json, urllib.request, urllib.error
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path("research/results/stock-shadow")
STATE=ROOT/"portfolio-v1.json"; OUT=ROOT/"fundamentals-observer-v1.json"
UA="stock-shadow-research/1.0 leo14881-eng@users.noreply.github.com"
MAX_REFRESH=12

def now(): return datetime.now(timezone.utc).isoformat()
def load(p,d):
    try: return json.loads(p.read_text()) if p.exists() else d
    except Exception: return d
def get(url):
    req=urllib.request.Request(url,headers={"User-Agent":UA,"Accept":"application/json","Accept-Encoding":"identity"})
    with urllib.request.urlopen(req,timeout=25) as r:return json.load(r)

def ticker_map():
    urls=["https://www.sec.gov/files/company_tickers.json","https://www.sec.gov/files/company_tickers_exchange.json"]
    errs=[]
    for url in urls:
        try:
            raw=get(url)
            if isinstance(raw,dict) and "data" in raw and "fields" in raw:
                fields=raw["fields"]; return {str(dict(zip(fields,row)).get("ticker","")).upper().replace(".","-"):dict(zip(fields,row)) for row in raw["data"]},errs
            return {str(v.get("ticker","")).upper().replace(".","-"):v for v in raw.values()},errs
        except urllib.error.HTTPError as e: errs.append({"url":url,"type":"HTTPError","status":e.code,"reason":str(e.reason)})
        except Exception as e: errs.append({"url":url,"type":type(e).__name__,"message":str(e)[:120]})
    return {},errs

def main():
    state=load(STATE,{"positions":{}}); old=load(OUT,{"companies":{}})
    companies=old.get("companies",{})
    ticker_map_data,map_errors=ticker_map()
    if not ticker_map_data:
        out={"updated_at":now(),"mode":"OBSERVATION_ONLY","status":"DEGRADED","mapping_errors":map_errors,
             "companies":companies,"strategy_effect":False}
        OUT.write_text(json.dumps(out,indent=2,sort_keys=True)+"\n"); print(json.dumps({k:v for k,v in out.items() if k!="companies"})); return
    symbols=sorted(state.get("positions",{}))
    # Rotate stale/unseen holdings; bounded SEC load per run.
    targets=sorted(symbols,key=lambda s:(s in companies,companies.get(s,{}).get("updated_at","")))[:MAX_REFRESH]
    refreshed=0; errors=[]
    for s in targets:
        meta=ticker_map_data.get(s)
        if not meta:
            companies[s]={"symbol":s,"status":"NO_SEC_MAPPING","updated_at":now()}; continue
        cik=int(meta.get("cik_str") or meta.get("cik"))
        try:
            sub=get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
            recent=(sub.get("filings") or {}).get("recent") or {}
            forms=recent.get("form") or []; dates=recent.get("filingDate") or []; acc=recent.get("accessionNumber") or []
            latest=[]
            for form,date,an in zip(forms,dates,acc):
                if form in {"10-K","10-Q","8-K","10-K/A","10-Q/A","8-K/A"}:
                    latest.append({"form":form,"filing_date":date,"accession":an})
                if len(latest)>=12: break
            risk_forms=[x for x in latest if x["form"].startswith("8-K")]
            companies[s]={"symbol":s,"cik":cik,"company":sub.get("name") or meta.get("title"),
                "status":"OBSERVED","latest_material_filings":latest,
                "recent_8k_count":len(risk_forms),"updated_at":now(),
                "fundamental_state":"UNKNOWN_OBSERVATION_ONLY",
                "note":"Filings collected for evidence; no automatic strategy decision yet."}
            refreshed+=1
        except urllib.error.HTTPError as e:
            errors.append({"symbol":s,"type":"HTTPError","status":e.code})
        except Exception as e:
            errors.append({"symbol":s,"type":type(e).__name__})
    out={"updated_at":now(),"mode":"OBSERVATION_ONLY","strategy_effect":False,"positions":len(symbols),
         "tracked":sum(1 for s in symbols if s in companies),"refreshed_this_run":refreshed,
         "refresh_limit":MAX_REFRESH,"errors":errors,"status":"OK" if not errors else "PARTIAL","companies":companies}
    OUT.parent.mkdir(parents=True,exist_ok=True); OUT.write_text(json.dumps(out,indent=2,sort_keys=True)+"\n")
    print(json.dumps({k:v for k,v in out.items() if k!="companies"}))

if __name__=="__main__": main()
