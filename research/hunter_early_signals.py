#!/usr/bin/env python3
"""Hunter v3 early-signal funnel: Binance-only, research-only, never trades."""
import datetime as dt,json,math,os,pathlib,urllib.parse,urllib.request
from concurrent.futures import ThreadPoolExecutor,as_completed
ROOT=pathlib.Path("research/results")
SCAN=ROOT/"hunter-cex-universe-run.json"
OUT=ROOT/"hunter-early-signals.json"
BN=os.getenv("HUNTER_BINANCE_API","https://data-api.binance.vision")
BATCH=80

def get(url,timeout=20):
    req=urllib.request.Request(url,headers={"User-Agent":"hunter-v3-early/1.0","Accept":"application/json"})
    with urllib.request.urlopen(req,timeout=timeout) as r:return json.load(r)
def finite(v):
    try:
        x=float(v);return x if math.isfinite(x) else None
    except (TypeError,ValueError,OverflowError):return None
def rolling(symbols,window):
    """Derive 1h/4h return from Binance spot klines with bounded concurrency."""
    interval="1h";bars=2 if window=="1h" else 5
    def one(sym):
        q=urllib.parse.urlencode({"symbol":sym,"interval":interval,"limit":bars})
        rows=get(BN+"/api/v3/klines?"+q,8)
        if not isinstance(rows,list) or len(rows)<bars:return None
        start=finite(rows[0][1]);last=finite(rows[-1][4])
        if not start or not last or start<=0:return None
        return sym,{"return_pct":(last/start-1)*100,
                   "quote_volume":sum(finite(x[7]) or 0 for x in rows)}
    out={}
    with ThreadPoolExecutor(max_workers=64) as ex:
        futs={ex.submit(one,s):s for s in symbols}
        for fut in as_completed(futs):
            try:
                row=fut.result()
                if row:out[row[0]]=row[1]
            except Exception:
                continue
    if "BTCUSDT" not in out:raise RuntimeError("BTC benchmark kline unavailable")
    return out

def score_row(sym,base,r1,r4,btc1,btc4):
    a=r1.get(sym);b=r4.get(sym)
    if not a or not b or not btc1 or not btc4:return None
    rel1=a["return_pct"]-btc1["return_pct"];rel4=b["return_pct"]-btc4["return_pct"]
    accel=rel1-rel4/4.0
    # Early attention, not a buy signal: reward relative strength and acceleration.
    score=max(0,rel1)*2+max(0,rel4)+max(0,accel)*1.5
    independent=sum((rel1>=0.8,rel4>=1.5,accel>=0.5))
    stage="EARLY" if independent>=2 else "WATCH"
    return {"base":base,"pair":sym,"stage":stage,"score":round(score,4),
      "btc_relative_1h_pct":round(rel1,4),"btc_relative_4h_pct":round(rel4,4),
      "relative_acceleration_pct":round(accel,4),
      "return_1h_pct":round(a["return_pct"],4),"return_4h_pct":round(b["return_pct"],4),
      "independent_signal_count":independent,"research_only":True}
def build(scan,r1,r4,now):
    pairs={c["pairs"][0]["pair"]:base for base,c in (scan.get("coins") or {}).items()
           if c.get("pairs") and c["pairs"][0].get("venue")=="binance"}
    btc1=r1.get("BTCUSDT");btc4=r4.get("BTCUSDT")
    if not btc1 or not btc4:raise ValueError("BTC benchmark missing")
    rows=[x for sym,base in pairs.items() if (x:=score_row(sym,base,r1,r4,btc1,btc4))]
    rows.sort(key=lambda x:(x["stage"]!="EARLY",-x["score"],x["base"]))
    early=[x for x in rows if x["stage"]=="EARLY"]
    return {"schema":"hunter_early_signals_v3","as_of_utc":now.isoformat(),
      "scan_generation_id":scan.get("generation_id"),"capital_authority":"NONE_RESEARCH_ONLY",
      "method":"1h/4h BTC-relative strength + relative acceleration; no 24h-gain prerequisite",
      "early_count":len(early),"early":early,"watch":rows[:30]}
def main():
    scan=json.loads(SCAN.read_text())
    if not scan.get("binance_complete"):raise SystemExit("incomplete Binance scan")
    pairs=[c["pairs"][0]["pair"] for c in (scan.get("coins") or {}).values()
           if c.get("pairs") and c["pairs"][0].get("venue")=="binance"]
    symbols=sorted(set(pairs+["BTCUSDT"]))
    r1=rolling(symbols,"1h");r4=rolling(symbols,"4h")
    report=build(scan,r1,r4,dt.datetime.now(dt.timezone.utc))
    OUT.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps({"early_count":report["early_count"],"top":report["early"][:10]},ensure_ascii=False))
if __name__=="__main__":main()
