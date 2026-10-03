#!/usr/bin/env python3
"""SEC public-feed health/event collector; no API key."""
import json, urllib.request
from datetime import datetime, timezone
from pathlib import Path
OUT=Path("research/results/stock-shadow/sec-status.json")
def main():
    url="https://www.sec.gov/files/company_tickers.json"
    req=urllib.request.Request(url,headers={"User-Agent":"stock-shadow research contact github.com/leo14881-eng"})
    o={"updated_at":datetime.now(timezone.utc).isoformat(),"source":"SEC_EDGAR","ok":False}
    try:
        with urllib.request.urlopen(req,timeout=20) as r: d=json.load(r)
        o.update({"ok":True,"company_index_count":len(d)})
    except Exception as e: o["error"]=type(e).__name__
    OUT.parent.mkdir(parents=True,exist_ok=True); OUT.write_text(json.dumps(o,indent=2)+"\n"); print(json.dumps(o))
if __name__=="__main__": main()
