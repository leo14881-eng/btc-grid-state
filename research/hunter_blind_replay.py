#!/usr/bin/env python3
"""Hunter Blind Full-Universe Replay v1.0 — RESEARCH ONLY.

Reconstructs a point-in-time Binance Spot USDT universe from Binance Data Vision
archive keys. Missing perfect dated exchangeInfo is a coverage limitation, not a
global STOP. Only archive-observed symbols are used; no current-symbol allowlist.
"""
import csv, io, json, math, os, statistics, urllib.parse, urllib.request, zipfile
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
from datetime import datetime, timezone

OUT="research/results"; os.makedirs(OUT,exist_ok=True)
S3="https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
BASE="https://data.binance.vision/data/spot/monthly/klines"
START_YM="2022-01"; END_YM="2025-12"
# PRE-REGISTERED before outcome computation.
MIN_HISTORY_DAYS=90
LIQUIDITY_TOP_N=100
OBSERVATION_STEP_DAYS=7
FEATURE_VERSION="spot-daily-v1"
RULE_VERSION="blind-full-universe-v1.3-frozen-window-reporting"
MAX_DOWNLOAD_WORKERS=12
STABLE_BASES={"USDC","BUSD","TUSD","FDUSD","USDP","DAI","USDS","UST","USTC","EUR","TRY","BRL","GBP","AUD","RUB","UAH","BIDR","IDRT","NGN","VAI","PAX","SUSD"}
LEVERAGED_SUFFIXES=("UP","DOWN","BULL","BEAR")

def get(url,timeout=45):
    req=urllib.request.Request(url,headers={"User-Agent":"hunter-blind-replay/1.0"})
    with urllib.request.urlopen(req,timeout=timeout) as r:return r.read()

def list_prefixes(prefix):
    """List S3 CommonPrefixes; this is archive-derived discovery, not current exchangeInfo."""
    token=None; out=[]
    while True:
        q={"list-type":"2","delimiter":"/","prefix":prefix,"max-keys":"1000"}
        if token:q["continuation-token"]=token
        root=ET.fromstring(get(S3+"?"+urllib.parse.urlencode(q)))
        ns={"s":"http://s3.amazonaws.com/doc/2006-03-01/"}
        out += [x.text for x in root.findall("s:CommonPrefixes/s:Prefix",ns)]
        trunc=(root.findtext("s:IsTruncated",default="false",namespaces=ns)=="true")
        if not trunc:break
        token=root.findtext("s:NextContinuationToken",default=None,namespaces=ns)
        if not token:break
    return out

def discover_symbols():
    prefs=list_prefixes("data/spot/monthly/klines/")
    syms=[]
    for p in prefs:
        s=p.rstrip("/").split("/")[-1]
        if not s.endswith("USDT") or s=="BTCUSDT": syms.append(s) if s=="BTCUSDT" else None; continue
        b=s[:-4]
        if b in STABLE_BASES or any(b.endswith(x) for x in LEVERAGED_SUFFIXES):continue
        syms.append(s)
    if "BTCUSDT" not in syms:syms.append("BTCUSDT")
    return sorted(set(syms))

def months():
    y,m=map(int,START_YM.split("-")); ey,em=map(int,END_YM.split("-"))
    a=[]
    while (y,m)<=(ey,em):
        a.append(f"{y:04d}-{m:02d}"); m+=1
        if m==13:y+=1;m=1
    return a

MONTHS=months()

def download_month(sym,month):
    name=f"{sym}-1d-{month}.zip"; url=f"{BASE}/{sym}/1d/{name}"
    try:data=get(url,30)
    except Exception:return []
    try:
        z=zipfile.ZipFile(io.BytesIO(data)); raw=z.read(z.namelist()[0]).decode("utf-8-sig")
    except Exception:return []
    out=[]
    for r in csv.reader(io.StringIO(raw)):
        if not r or not r[0].strip().isdigit():continue
        try:
            out.append({"t":int(r[0]),"o":float(r[1]),"h":float(r[2]),"l":float(r[3]),"c":float(r[4]),
                        "v":float(r[5]),"qv":float(r[7]),"n":int(float(r[8])),"tbq":float(r[10])})
        except Exception:pass
    return out

def load(sym):
    a=[]
    for m in MONTHS:a.extend(download_month(sym,m))
    a.sort(key=lambda x:x["t"])
    # de-duplicate archive rows
    return list({x["t"]:x for x in a}.values())

symbols=discover_symbols()
# Persist the archive-derived symbol manifest for survivorship cross-checking.
# This is observational output only: it does not alter the frozen signal, thresholds,
# candidate selection, outcomes, calibration, or OOS windows.
with open(f"{OUT}/hunter-archive-symbol-manifest.json","w") as f:
    json.dump({"source":"Binance Data Vision S3 archive prefixes",
      "period":f"{START_YM} through {END_YM}",
      "universe_method":"HISTORICAL_FILE_RECONSTRUCTED_UNIVERSE",
      "symbol_count":len(symbols),"symbols":symbols},f,indent=2)
    f.write("\n")
data={}; skipped=[]
# Download symbols concurrently. Each symbol still loads its monthly archives
# deterministically; concurrency changes transport speed only, not research rules.
with ThreadPoolExecutor(max_workers=MAX_DOWNLOAD_WORKERS) as ex:
    futs={ex.submit(load,s):s for s in symbols}
    done=0
    for fut in as_completed(futs):
        s=futs[fut]; done+=1
        try:a=fut.result()
        except Exception as e:
            a=[]; print(f"[{done}/{len(symbols)}] {s}: DOWNLOAD_ERROR {type(e).__name__}",flush=True)
        if a:data[s]=sorted(a,key=lambda x:x["t"])
        else:skipped.append(s)
        print(f"[{done}/{len(symbols)}] {s}: {len(a)} daily rows",flush=True)

btc=data.get("BTCUSDT")
if not btc:raise SystemExit("DATA_SOURCE_FAILURE: BTC Spot archive unavailable")
btc_by_t={x["t"]:x for x in btc}
idx={s:{x["t"]:i for i,x in enumerate(a)} for s,a in data.items()}
all_dates=sorted({x["t"] for a in data.values() for x in a if START_YM<=datetime.fromtimestamp(x["t"]/1000,tz=timezone.utc).strftime("%Y-%m")<=END_YM})
obs_dates=all_dates[::OBSERVATION_STEP_DAYS]

events=[]; controls=[]; coverage_rows=[]; candidate_days=0
for t in obs_dates:
    eligible=[]
    for s,a in data.items():
        if s=="BTCUSDT":continue
        i=idx[s].get(t)
        if i is None or i<MIN_HISTORY_DAYS:continue
        # 30d quote-volume liquidity, PIT only
        qv=sum(x["qv"] for x in a[i-29:i+1])
        if qv>0:eligible.append((qv,s,i))
    eligible.sort(reverse=True); universe=eligible[:LIQUIDITY_TOP_N]
    usize=len(universe); scanned=0
    for _,s,i in universe:
        a=data[s]
        if i<MIN_HISTORY_DAYS:continue
        cur=a[i]; b0=btc_by_t.get(t)
        if not b0:continue
        # Feature eligibility is PIT-only. Never require future outcome availability
        # to count discovery coverage: that would leak the future into scanned_count.
        p30=a[i-30]; p90=a[i-90]
        b30=btc_by_t.get(p30["t"])
        if not b30:continue
        scanned+=1; candidate_days+=1
        ret30=cur["c"]/p30["c"]-1; ret90=cur["c"]/p90["c"]-1
        btc30=b0["c"]/b30["c"]-1; rel30=ret30-btc30
        qv7=sum(x["qv"] for x in a[i-6:i+1]); qvprev=sum(x["qv"] for x in a[i-13:i-6])
        volratio=qv7/qvprev if qvprev else 0
        taker=sum(x["tbq"] for x in a[i-6:i+1]); total=sum(x["qv"] for x in a[i-6:i+1])
        taker_share=taker/total if total else 0
        dist_high=cur["c"]/max(x["h"] for x in a[i-89:i+1])-1
        horizons={}
        for h in (30,90,180,365):
            if i+h>=len(a):continue
            end=a[i+h]; bend=btc_by_t.get(end["t"])
            if not bend:continue
            fut=a[i+1:i+h+1]
            if not fut:continue
            r=end["c"]/cur["c"]-1; br=bend["c"]/b0["c"]-1
            horizons[str(h)]={"absolute_return":r,"btc_relative_return":r-br,
              "mae":min(x["l"] for x in fut)/cur["c"]-1,
              "mfe":max(x["h"] for x in fut)/cur["c"]-1,
              "time_to_mfe_days":1+max(range(len(fut)),key=lambda j:fut[j]["h"])}
        control={"symbol":s,"t":t,"utc":datetime.fromtimestamp(t/1000,tz=timezone.utc).date().isoformat(),
          "ret30":ret30,"ret90":ret90,"btc_rel30":rel30,"volume_ratio_7d":volratio,
          "taker_buy_share_7d":taker_share,"distance_90d_high":dist_high,"horizons":horizons}
        controls.append(control)
        # deterministic market-data signal; thresholds remain frozen.
        pre=(ret90<0.50 and rel30>0.03 and volratio>1.20 and taker_share>0.50 and dist_high<0)
        early=(rel30>0.08 and volratio>1.35 and taker_share>0.52 and dist_high>-0.08)
        if not(pre or early):continue
        events.append({**control,"stage":"PRE_MOVE" if pre else "EARLY_MOVE",
          "universe_size":usize,"universe_rank":1+[x[1] for x in universe].index(s)})
    coverage_rows.append({"t":t,"universe_size":usize,"scanned_count":scanned,"coverage_ratio":scanned/usize if usize else 0})

# immutable-like first discovery + 30d cooldown per symbol/stage
events.sort(key=lambda e:(e["symbol"],e["stage"],e["t"])); ded=[]; last={}
for e in events:
    k=(e["symbol"],e["stage"])
    if e["t"]-last.get(k,-10**18)<30*86400000:continue
    ded.append(e);last[k]=e["t"]
events=ded

def med(vals):return statistics.median(vals) if vals else None
metrics={}
for h in ("30","90","180","365"):
    rows=[e["horizons"][h] for e in events if h in e["horizons"]]
    metrics[h]={"N":len(rows),"median_absolute_return":med([x["absolute_return"] for x in rows]),
      "median_btc_relative_return":med([x["btc_relative_return"] for x in rows]),
      "median_mae":med([x["mae"] for x in rows]),"median_mfe":med([x["mfe"] for x in rows]),
      "median_time_to_mfe_days":med([x["time_to_mfe_days"] for x in rows])}

# Simple baselines MUST be selected from the full eligible PIT control universe,
# not from Hunter hits. Otherwise the benchmark is conditioned on Hunter itself.
rs=[e for e in controls if e["btc_rel30"]>0.08]
vm=[e for e in controls if e["ret30"]>0 and e["volume_ratio_7d"]>1.35]
def baseline(rows,h="90"):
    v=[e["horizons"][h]["btc_relative_return"] for e in rows if h in e["horizons"]]
    return {"N":len(v),"median_btc_relative_return":med(v)}
baselines={"btc_buy_hold":{"median_btc_relative_return":0.0},
           "simple_btc_relative_momentum":baseline(rs),
           "simple_volume_momentum":baseline(vm)}
hunter90=metrics["90"]["median_btc_relative_return"]
best=max([x["median_btc_relative_return"] for x in baselines.values() if x["median_btc_relative_return"] is not None])
coverage_num=sum(x["scanned_count"] for x in coverage_rows)
coverage_den=sum(x["universe_size"] for x in coverage_rows)
coverage=coverage_num/coverage_den if coverage_den else 0
def window_report(start_date,end_date):
    ev=[e for e in events if start_date<=e["utc"]<=end_date]
    ct=[e for e in controls if start_date<=e["utc"]<=end_date]
    wm={}
    for h in ("30","90","180","365"):
        rows=[e["horizons"][h] for e in ev if h in e["horizons"]]
        wm[h]={"N":len(rows),"median_absolute_return":med([x["absolute_return"] for x in rows]),
          "median_btc_relative_return":med([x["btc_relative_return"] for x in rows]),
          "median_mae":med([x["mae"] for x in rows]),"median_mfe":med([x["mfe"] for x in rows]),
          "median_time_to_mfe_days":med([x["time_to_mfe_days"] for x in rows])}
    wrs=[e for e in ct if e["btc_rel30"]>0.08]
    wvm=[e for e in ct if e["ret30"]>0 and e["volume_ratio_7d"]>1.35]
    wb={"btc_buy_hold":{"median_btc_relative_return":0.0},
        "simple_btc_relative_momentum":baseline(wrs),
        "simple_volume_momentum":baseline(wvm)}
    h90=wm["90"]["median_btc_relative_return"]
    bvals=[x["median_btc_relative_return"] for x in wb.values() if x["median_btc_relative_return"] is not None]
    return {"window":f"{start_date}/{end_date}","raw_n":len(ev),
      "effective_n":len({(e["symbol"],e["utc"][:7]) for e in ev}),
      "metrics":wm,"baselines":wb,
      "hunter_minus_best_simple_baseline_90d":h90-max(bvals) if h90 is not None and bvals else None}

window_results={
 "calibration_2022_2023":window_report("2022-01-01","2023-12-31"),
 "oos_2024":window_report("2024-01-01","2024-12-31"),
 "oos_2025":window_report("2025-01-01","2025-12-31")
}

# Conservative effective N proxy: unique symbol-month clusters.
clusters={(e["symbol"],e["utc"][:7]) for e in events}
effective_n=len(clusters)
summary={"generated_at":datetime.now(timezone.utc).isoformat(),"status":"RESEARCH_SIGNAL",
 "venue":"Binance Spot","quote_asset":"USDT","source":"Binance Data Vision archive-derived symbol universe",
 "period":f"{START_YM} through {END_YM}","universe_method":"HISTORICAL_FILE_RECONSTRUCTED_UNIVERSE",
 "completeness":"PARTIAL","survivorship_label":"SURVIVORSHIP_INCOMPLETE_MVP",
 "preregistered":{"min_history_days":MIN_HISTORY_DAYS,"liquidity_top_n":LIQUIDITY_TOP_N,
   "observation_step_days":OBSERVATION_STEP_DAYS,"feature_version":FEATURE_VERSION,"rule_version":RULE_VERSION,"max_download_workers":MAX_DOWNLOAD_WORKERS},
 "universe_size":len(symbols),"symbols_with_archive_data":len(data),"scanned_count":coverage_num,
 "coverage_ratio":coverage,"raw_n":len(events),"effective_n":effective_n,
 "metrics":metrics,"baselines":baselines,"window_results":window_results,
 "hunter_minus_best_simple_baseline_90d":hunter90-best if hunter90 is not None else None,
 "n_min_30_met":effective_n>=30,"k_calibration_supported":False,
 "k_status":"UNSET/SHADOW",
 "limitations":["Archive-derived universe completeness is not proven; run remains SURVIVORSHIP_INCOMPLETE_MVP.",
 "Effective N is a conservative unique symbol-month cluster count, not a formal dependence-adjusted estimator.",
 "Peer-relative return and Detection Lead/Lag require a frozen peer taxonomy/leader definition and remain UNKNOWN rather than fabricated.",
 "Future outcome availability is excluded from discovery coverage accounting; horizons are reported only where honestly available.",
 "Simple baselines are selected from the full eligible PIT control universe, not Hunter-selected events.",
 "Frozen calibration and OOS windows are now reported separately without changing signal thresholds or tuning on OOS outcomes.",
 "This run cannot calibrate k until VERIFIED coverage and frozen OOS requirements are satisfied."]}

with open(f"{OUT}/hunter-blind-replay-summary.json","w") as f:json.dump(summary,f,indent=2)
flat=[]
for e in events:
    r={k:v for k,v in e.items() if k!="horizons"}
    for h,v in e["horizons"].items():
        for k,x in v.items():r[f"{h}d_{k}"]=x
    flat.append(r)
fields=sorted({k for r in flat for k in r}) if flat else ["symbol","t","utc","stage"]
with open(f"{OUT}/hunter-blind-replay-events.csv","w",newline="") as f:
    w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(flat)
with open(f"{OUT}/hunter-blind-replay-controls.csv","w",newline="") as f:
    w=csv.DictWriter(f,fieldnames=["baseline","N","median_btc_relative_return"]);w.writeheader()
    for k,v in baselines.items():w.writerow({"baseline":k,"N":v.get("N",""),"median_btc_relative_return":v["median_btc_relative_return"]})

# Mirror run progress into SSOT without claiming verified full-universe validation.
try:
    with open("hunter-replay-v1.json") as f:state=json.load(f)
    a=state["active_run"];a.update({"phase":"REPLAY_SUBSET_COMPLETED","universe_size":summary["universe_size"],
      "scanned_count":summary["scanned_count"],"coverage_ratio":summary["coverage_ratio"],
      "raw_n":summary["raw_n"],"effective_n":summary["effective_n"],
      "blocker":"Archive reconstruction ran and produced numeric results; survivorship completeness remains unproven.",
      "next_step":"Verify archive-universe completeness and peer taxonomy; then run frozen 2024/2025 OOS windows and only calibrate k if VERIFIED Effective N>=30."})
    with open("hunter-replay-v1.json","w") as f:json.dump(state,f,indent=2);f.write("\n")
except Exception as e:print("STATE_MIRROR_WARNING:",e,flush=True)
print(json.dumps(summary,indent=2))
