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

# Phase 2: matched negative controls + chronological out-of-sample split.
# Match at the same hour among loaded non-signal assets with similar trailing 24h volume rank.
event_keys={(e["symbol"],e["t"]) for e in events}
control_rows=[]
by_time={}
for sym,a in data.items():
    if sym=="BTCUSDT": continue
    for i in range(168,len(a)-73):
        by_time.setdefault(a[i]["t"],[]).append((sym,a,i))

for e in events:
    pool=by_time.get(e["t"],[])
    # event trailing volume
    ea=data[e["symbol"]]; ei=next((i for i,x in enumerate(ea) if x["t"]==e["t"]),None)
    if ei is None: continue
    ev=sum(x["v"]*x["c"] for x in ea[ei-23:ei+1])
    cand=[]
    for sym,a,i in pool:
        if sym==e["symbol"] or (sym,e["t"]) in event_keys: continue
        qv=sum(x["v"]*x["c"] for x in a[i-23:i+1])
        if qv<=0 or ev<=0: continue
        # nearest log-volume is a deterministic liquidity match
        import math
        cand.append((abs(math.log(qv/ev)),sym,a,i))
    if not cand: continue
    _,sym,a,i=min(cand,key=lambda x:(x[0],x[1]))
    cur=a[i]; fut=a[i+1:i+73]; b0=btc_by_t.get(cur["t"]); bend=btc_by_t.get(fut[-1]["t"])
    if not b0 or not bend: continue
    r=fut[-1]["c"]/cur["c"]-1; br=bend["c"]/b0["c"]-1
    control_rows.append({"symbol":sym,"matched_event_symbol":e["symbol"],"t":e["t"],"utc":e["utc"],
      "matched_signal":e["signal"],"ret72":r,"mfe72":max(x["h"] for x in fut)/cur["c"]-1,
      "mae72":min(x["l"] for x in fut)/cur["c"]-1,"btc72":br,"excess72":r-br})

def pair_metrics(sig,ctl):
    n=min(len(sig),len(ctl))
    if not n:return {"N_pairs":0}
    s=sig[:n]; c=ctl[:n]
    return {"N_pairs":n,
      "signal_gt30_rate":sum(x["ret72"]>.30 for x in s)/n,
      "control_gt30_rate":sum(x["ret72"]>.30 for x in c)/n,
      "signal_positive_rate":sum(x["ret72"]>0 for x in s)/n,
      "control_positive_rate":sum(x["ret72"]>0 for x in c)/n,
      "signal_btc_outperform_rate":sum(x["excess72"]>0 for x in s)/n,
      "control_btc_outperform_rate":sum(x["excess72"]>0 for x in c)/n,
      "median_signal_ret72":statistics.median(x["ret72"] for x in s),
      "median_control_ret72":statistics.median(x["ret72"] for x in c),
      "median_signal_excess72":statistics.median(x["excess72"] for x in s),
      "median_control_excess72":statistics.median(x["excess72"] for x in c)}

# chronological 70/30 OOS split, preserving matched pairs
pairs=[]
ctlmap={(x["matched_event_symbol"],x["t"],x["matched_signal"]):x for x in control_rows}
for e in events:
    c=ctlmap.get((e["symbol"],e["t"],e["signal"]))
    if c:pairs.append((e,c))
pairs.sort(key=lambda p:p[0]["t"])
cut=int(len(pairs)*.70)
train=pairs[:cut]; valid=pairs[cut:]
def pm(ps,signal=None):
    q=[p for p in ps if signal is None or p[0]["signal"]==signal]
    return pair_metrics([p[0] for p in q],[p[1] for p in q])

summary={"generated_at":datetime.now(timezone.utc).isoformat(),"status":"RESEARCH_UNVALIDATED",
 "source":"Binance Data Vision static USD-M monthly 1h klines",
 "period":"2026-05 through 2026-08","symbols_requested":len(SYMBOLS),"symbols_loaded":len(data),"symbols_skipped":skipped,
 "overall":sm(events),"by_signal":{s:sm([e for e in events if e["signal"]==s]) for s in ["PRE_MOVE","BREAKOUT","ACCELERATION"]},
 "matched_controls":{"method":"same timestamp + nearest trailing-24h quote-volume; deterministic; sector matching unavailable in this layer",
   "all":pm(pairs),"train_70pct":pm(train),"validation_30pct":pm(valid),
   "validation_by_signal":{s:pm(valid,s) for s in ["PRE_MOVE","BREAKOUT","ACCELERATION"]}},
 "limitations":["Layer 2 adds time/liquidity matched controls and chronological OOS validation",
 "Sector matching is not yet implemented","No historical OI/funding/liquidation/catalyst labels",
 "Thresholds remain UNVALIDATED; do not promote to LIVE from this layer alone"]}
with open(f"{OUT}/hunter-blind-replay-summary.json","w") as f: json.dump(summary,f,indent=2)
fields=list(events[0]) if events else ["symbol","t","utc","signal"]
with open(f"{OUT}/hunter-blind-replay-events.csv","w",newline="") as f:
    w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(events)
cf=list(control_rows[0]) if control_rows else ["symbol","matched_event_symbol","t","utc","matched_signal"]
with open(f"{OUT}/hunter-blind-replay-controls.csv","w",newline="") as f:
    w=csv.DictWriter(f,fieldnames=cf); w.writeheader(); w.writerows(control_rows)
print(json.dumps(summary,indent=2))
if len(pairs)<30: raise SystemExit("VALIDATION_INCOMPLETE: matched N<30")
