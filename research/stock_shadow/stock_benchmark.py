#!/usr/bin/env python3
"""Free/no-key benchmark snapshot with graceful degradation."""
import json, urllib.request
from datetime import datetime, timezone
from pathlib import Path
OUT=Path("research/results/stock-shadow/benchmark.json")
def now(): return datetime.now(timezone.utc).isoformat()
def fetch_chart(sym):
    url=f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=5d&interval=1d"
    req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0 stock-shadow-research"})
    with urllib.request.urlopen(req,timeout=20) as r: return json.load(r)
def main():
    out={"updated_at":now(),"source":"FREE_PUBLIC_CHART","benchmarks":{},"degraded":[]}
    for s in ("SPY","QQQ"):
        try:
            x=fetch_chart(s)["chart"]["result"][0]; q=x["indicators"]["quote"][0]["close"]; vals=[v for v in q if v]
            out["benchmarks"][s]={"last":vals[-1],"previous":vals[-2] if len(vals)>1 else None}
        except Exception as e: out["degraded"].append({"symbol":s,"error":type(e).__name__})
    OUT.parent.mkdir(parents=True,exist_ok=True); OUT.write_text(json.dumps(out,indent=2)+"\n")
    print(json.dumps(out))
if __name__=="__main__": main()
