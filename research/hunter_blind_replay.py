#!/usr/bin/env python3
"""Hunter Blind Replay v0.3 — RESEARCH ONLY.
Static Binance Data Vision archive; no Binance REST/Futures API.
Layer-1 validates market-structure signals only; it does not fabricate OI/catalyst fields.
"""
import csv, io, json, os, statistics, urllib.request, zipfile
from datetime import datetime, timezone

OUT="research/results"; os.makedirs(OUT,exist_ok=True)
BASE="https://data.binance.vision/data/futures/um/monthly/klines"
# Fixed universe avoids exchangeInfo dependency and selection look-ahead.
SYMBOLS="""BTC ETH SOL XRP BNB DOGE ADA AVAX LINK DOT LTC BCH SUI NEAR AAVE UNI TRX ETC ATOM FIL ICP ARB OP INJ SEI TIA WIF PEPE CRV ENA JTO RUNE FET TAO TON POL APT LDO DYDX SAND MANA GALA ALGO XLM HBAR EGLD KAS RENDER ZEC DASH COMP MKR SNX THETA FLOW CHZ GRT ENS IMX STX MINA SAGA ZETA""".split()
SYMBOLS=[x+"USDT" for x in SYMBOLS]
MONTHS=["2026-05","2026-06","2026-07","2026-08"]

def download(sym,month):
    name=f"{sym}-1h-{month}.zip"
    url=f"{BASE}/{sym}/1h/{name}"
    req=urllib.request.Request(url,headers={"User-Agent":"hunter-replay/0.3"})
    try:
        with urllib.request.urlopen(req,timeout=30) as r: data=r.read()
    except Exception as e:
        return []
    try:
        z=zipfile.ZipFile(io.BytesIO(data)); raw=z.read(z.namelist()[0]).decode("utf-8-sig")
    except Exception: return []
    out=[]
    for row in csv.reader(io.StringIO(raw)):
        if not row or not row[0].strip().isdigit(): continue
        try:
            vol=float(row[5]); tb=float(row[9])
            out.append({"t":int(row[0]),"o":float(row[1]),"h":float(row[2]),"l":float(row[3]),"c":float(row[4]),
                        "v":vol,"n":int(float(row[8])),"tb":tb})
        except Exception: pass
    return out

def load(sym):
    a=[]
    for m in MONTHS: a.extend(download(sym,m))
    a.sort(key=lambda x:x["t"])
    return a

data={}; skipped=[]
for n,s in enumerate(SYMBOLS,1):
    a=load(s)
    if len(a)>=500: data[s]=a
    else: skipped.append(s)
    print(f"[{n}/{len(SYMBOLS)}] {s}: {len(a)} rows",flush=True)

btc=data.get("BTCUSDT")
if not btc: raise SystemExit("DATA_SOURCE_FAILURE: BTC archive unavailable")
btc_by_t={r["t"]:r for r in btc}
events=[]
for sym,a in data.items():
    if sym=="BTCUSDT": continue
    for i in range(168,len(a)-73):
        cur=a[i]; p24=a[i-24]; p7=a[i-168]
        ret24=cur["c"]/p24["c"]-1; ret7=cur["c"]/p7["c"]-1
        v24=sum(x["v"] for x in a[i-23:i+1]); vp=sum(x["v"] for x in a[i-47:i-23])
        volchg=v24/vp-1 if vp else 0
        tb=sum(x["tb"] for x in a[i-23:i+1]); sell=max(v24-tb,0); taker=tb/sell if sell else 99
        b0=btc_by_t.get(cur["t"]); b24=btc_by_t.get(p24["t"])
        if not b0 or not b24: continue
        rel24=ret24-(b0["c"]/b24["c"]-1)
        resistance=max(x["c"] for x in a[i-168:i])
        pre=(ret7<.15 and volchg>.25 and taker>1.05 and rel24>0)
        brk=(cur["c"]>resistance and volchg>.50)
        acc=(ret24>.30 and volchg>.80 and taker>1.20)
        if not(pre or brk or acc): continue
        fut=a[i+1:i+73]; bend=btc_by_t.get(fut[-1]["t"])
        ret72=fut[-1]["c"]/cur["c"]-1
        btc72=bend["c"]/b0["c"]-1 if bend else None
        events.append({"symbol":sym,"t":cur["t"],"utc":datetime.fromtimestamp(cur["t"]/1000,tz=timezone.utc).isoformat(),
          "signal":"PRE_MOVE" if pre else ("BREAKOUT" if brk else "ACCELERATION"),
          "ret24":ret24,"ret7":ret7,"volchg24":volchg,"taker_ratio24":taker,"btc_rel24":rel24,
          "ret72":ret72,"mfe72":max(x["h"] for x in fut)/cur["c"]-1,"mae72":min(x["l"] for x in fut)/cur["c"]-1,
          "btc72":btc72,"excess72":ret72-btc72 if btc72 is not None else None})

# independent discovery cooldown: no repeated same-symbol/signal within 72h
events.sort(key=lambda x:(x["symbol"],x["signal"],x["t"])); ded=[]; last={}
for e in events:
    k=(e["symbol"],e["signal"])
    if e["t"]-last.get(k,-10**18)<72*3600*1000: continue
    ded.append(e); last[k]=e["t"]
events=ded

def sm(rows):
    if not rows:return {"N":0}
    ex=[r["excess72"] for r in rows if r["excess72"] is not None]
    return {"N":len(rows),"ret72_gt_30_rate":sum(r["ret72"]>.30 for r in rows)/len(rows),
      "ret72_positive_rate":sum(r["ret72"]>0 for r in rows)/len(rows),
      "btc_outperform_rate":sum(x>0 for x in ex)/len(ex) if ex else None,
      "median_ret72":statistics.median(r["ret72"] for r in rows),
      "median_mfe72":statistics.median(r["mfe72"] for r in rows),
      "median_mae72":statistics.median(r["mae72"] for r in rows),
      "median_excess72":statistics.median(ex) if ex else None}

summary={"generated_at":datetime.now(timezone.utc).isoformat(),"status":"RESEARCH_UNVALIDATED",
 "source":"Binance Data Vision static USD-M monthly 1h klines",
 "period":"2026-05 through 2026-08","symbols_requested":len(SYMBOLS),"symbols_loaded":len(data),"symbols_skipped":skipped,
 "overall":sm(events),"by_signal":{s:sm([e for e in events if e["signal"]==s]) for s in ["PRE_MOVE","BREAKOUT","ACCELERATION"]},
 "limitations":["Layer 1 only: no historical OI/funding/liquidation/catalyst labels","No matched negative-control layer yet","Thresholds remain UNVALIDATED until controls and out-of-sample validation are complete"]}
with open(f"{OUT}/hunter-blind-replay-summary.json","w") as f: json.dump(summary,f,indent=2)
fields=list(events[0]) if events else ["symbol","t","utc","signal"]
with open(f"{OUT}/hunter-blind-replay-events.csv","w",newline="") as f:
    w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(events)
print(json.dumps(summary,indent=2))
if summary["overall"]["N"]<30: raise SystemExit("VALIDATION_INCOMPLETE: N<30")
