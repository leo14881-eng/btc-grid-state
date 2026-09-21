#!/usr/bin/env python3
"""
Hunter Blind Full-Universe Replay v0.1 (research only)
Uses Binance public USDT-M futures API. No API key.
Purpose: blind, reproducible validation of price/volume/taker/BTC-relative signals.
Does NOT change frozen Hunter production logic and does NOT authorize trades.
"""
import csv, json, math, os, statistics, time, urllib.parse, urllib.request
from datetime import datetime, timezone

BASE="https://fapi.binance.com"
OUT="research/results"
os.makedirs(OUT, exist_ok=True)
EXCLUDE_SUFFIX=("UPUSDT","DOWNUSDT","BULLUSDT","BEARUSDT")

def get(path, params=None, tries=5):
    url=BASE+path
    if params: url += "?" + urllib.parse.urlencode(params)
    last=None
    for i in range(tries):
        try:
            req=urllib.request.Request(url, headers={"User-Agent":"hunter-replay/0.1"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            last=e; time.sleep(1.5*(i+1))
    raise RuntimeError(f"GET failed {url}: {last}")

def klines(symbol, interval="1h", limit=1000):
    raw=get("/fapi/v1/klines",{"symbol":symbol,"interval":interval,"limit":limit})
    rows=[]
    for x in raw:
        vol=float(x[5]); tb=float(x[9]); sell=max(vol-tb,0.0)
        rows.append({"t":int(x[0]),"o":float(x[1]),"h":float(x[2]),"l":float(x[3]),"c":float(x[4]),
                     "v":vol,"n":int(x[8]),"tb":tb,"ts":(tb/sell if sell>0 else 99.0)})
    return rows

info=get("/fapi/v1/exchangeInfo")
symbols=[]
for s in info["symbols"]:
    sym=s["symbol"]
    if s.get("status")=="TRADING" and s.get("quoteAsset")=="USDT" and s.get("contractType")=="PERPETUAL" and not sym.endswith(EXCLUDE_SUFFIX):
        symbols.append(sym)

btc=klines("BTCUSDT")
btc_by_t={r["t"]:r for r in btc}
events=[]; skipped=0
# Keep run bounded and reproducible: top liquid contracts by current 24h quote volume.
tickers=get("/fapi/v1/ticker/24hr")
liq={x["symbol"]:float(x.get("quoteVolume",0) or 0) for x in tickers}
symbols=sorted(symbols,key=lambda s:liq.get(s,0),reverse=True)[:120]

for si,sym in enumerate(symbols):
    try:
        a=klines(sym)
        if len(a)<240: continue
        # strictly past-only rolling features; event outcome uses next 72h.
        for i in range(168, len(a)-73):
            cur=a[i]
            prev24=a[i-24]
            prev7=a[i-168]
            ret24=cur["c"]/prev24["c"]-1
            ret7=cur["c"]/prev7["c"]-1
            vols=[z["v"] for z in a[i-48:i-24]]
            basevol=statistics.median(vols) if vols else 0
            vol24=sum(z["v"] for z in a[i-23:i+1])
            prior24=sum(z["v"] for z in a[i-47:i-23])
            volchg=(vol24/prior24-1) if prior24>0 else 0
            tb=sum(z["tb"] for z in a[i-23:i+1])
            vv=sum(z["v"] for z in a[i-23:i+1]); sell=max(vv-tb,0)
            taker=tb/sell if sell>0 else 99
            # breakout = close > max close of preceding 7d (excluding current bar)
            resistance=max(z["c"] for z in a[i-168:i])
            breakout=cur["c"]>resistance
            b0=btc_by_t.get(cur["t"]); b24=btc_by_t.get(a[i-24]["t"])
            if not b0 or not b24: continue
            btc24=b0["c"]/b24["c"]-1
            rel24=ret24-btc24
            # v0.1 observable market-only signals (OI/catalyst intentionally not fabricated)
            pre_move=(ret7<0.15 and volchg>0.25 and taker>1.05 and rel24>0)
            breakout_sig=(breakout and volchg>0.50)
            accel=(ret24>0.30 and volchg>0.80 and taker>1.20)
            if not (pre_move or breakout_sig or accel): continue
            fut=a[i+1:i+73]
            end=fut[-1]["c"]/cur["c"]-1
            mfe=max(z["h"] for z in fut)/cur["c"]-1
            mae=min(z["l"] for z in fut)/cur["c"]-1
            b_end=btc_by_t.get(fut[-1]["t"])
            btc72=(b_end["c"]/b0["c"]-1) if b_end else None
            events.append({"symbol":sym,"t":cur["t"],"utc":datetime.fromtimestamp(cur["t"]/1000,tz=timezone.utc).isoformat(),
                "signal":"PRE_MOVE" if pre_move else ("BREAKOUT" if breakout_sig else "ACCELERATION"),
                "ret24":ret24,"ret7":ret7,"volchg24":volchg,"taker_ratio24":taker,"btc_rel24":rel24,
                "ret72":end,"mfe72":mfe,"mae72":mae,"btc72":btc72,"excess72":(end-btc72 if btc72 is not None else None)})
        time.sleep(.08)
    except Exception as e:
        skipped+=1
        print("SKIP",sym,str(e)[:180])

# De-overlap same symbol/signal: retain first discovery, then 72h cooldown.
events.sort(key=lambda x:(x["symbol"],x["signal"],x["t"]))
ded=[]; last={}
for e in events:
    k=(e["symbol"],e["signal"])
    if e["t"]-last.get(k,-10**18) < 72*3600*1000: continue
    ded.append(e); last[k]=e["t"]
events=ded

def summarize(rows):
    if not rows:return {"N":0}
    vals=lambda k:[r[k] for r in rows if r.get(k) is not None]
    return {"N":len(rows),
      "ret72_gt_30_rate":sum(r["ret72"]>.30 for r in rows)/len(rows),
      "ret72_positive_rate":sum(r["ret72"]>0 for r in rows)/len(rows),
      "btc_outperform_rate":sum((r["excess72"] or -999)>0 for r in rows)/len(rows),
      "median_ret72":statistics.median(vals("ret72")),
      "median_mfe72":statistics.median(vals("mfe72")),
      "median_mae72":statistics.median(vals("mae72")),
      "median_excess72":statistics.median(vals("excess72")) if vals("excess72") else None}

summary={"generated_at":datetime.now(timezone.utc).isoformat(),"status":"RESEARCH_UNVALIDATED",
 "method":"blind past-only market-data replay; top 120 liquid Binance USDT perpetuals; 1000x1h lookback; 72h outcomes; 72h per-symbol/signal cooldown",
 "limitations":["No catalyst labels in this layer","No historical OI/funding/liquidations in layer 1","Thresholds are v0.1 research hypotheses, not LIVE"],
 "symbols_scanned":len(symbols),"symbols_skipped":skipped,"overall":summarize(events),
 "by_signal":{s:summarize([e for e in events if e["signal"]==s]) for s in ["PRE_MOVE","BREAKOUT","ACCELERATION"]}}
with open(f"{OUT}/hunter-blind-replay-summary.json","w") as f: json.dump(summary,f,indent=2)
fields=list(events[0].keys()) if events else ["symbol","t","utc","signal"]
with open(f"{OUT}/hunter-blind-replay-events.csv","w",newline="") as f:
    w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(events)
print(json.dumps(summary,indent=2))
if summary["overall"]["N"]<30:
    raise SystemExit("VALIDATION_INCOMPLETE: N<30")
