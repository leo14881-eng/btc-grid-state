#!/usr/bin/env python3
"""SEC public-feed health/event collector; no API key."""
import json, urllib.request
from datetime import datetime, timezone
from pathlib import Path
OUT=Path("research/results/stock-shadow/sec-status.json")

def main():
    urls=[
        "https://www.sec.gov/files/company_tickers.json",
        "https://data.sec.gov/submissions/CIK0000320193.json",
        "https://efts.sec.gov/LATEST/search-index?q=Apple&dateRange=all",
    ]
    headers={"User-Agent":"stock-shadow-research/1.0 leo14881-eng@users.noreply.github.com","Accept":"application/json"}
    o={"updated_at":datetime.now(timezone.utc).isoformat(),"source":"SEC_EDGAR","ok":False}
    errors=[]
    for url in urls:
        try:
            req=urllib.request.Request(url,headers=headers)
            with urllib.request.urlopen(req,timeout=20) as r:
                d=json.load(r)
            o.update({"ok":True,"endpoint":url,"records":len(d)})
            break
        except Exception as e:
            errors.append(type(e).__name__)
    if not o["ok"]:
        o["error"]="|".join(errors) or "UNKNOWN"
    OUT.parent.mkdir(parents=True,exist_ok=True)
    OUT.write_text(json.dumps(o,indent=2)+"\n")
    print(json.dumps(o))

if __name__=="__main__":
    main()
