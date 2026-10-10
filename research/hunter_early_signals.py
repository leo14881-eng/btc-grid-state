#!/usr/bin/env python3
"""Hunter v4 PRE_MOVE funnel: multi-horizon volume/structure/BTC-relative signals; research only."""
import datetime as dt,json,math,os,pathlib,urllib.parse,urllib.request
from concurrent.futures import ThreadPoolExecutor,as_completed
try:
 from research.hunter_policy import C,LANES,VERSION,POLICY,fresh,stamp,chase_blockers
except ModuleNotFoundError as exc:
 if exc.name != 'research':raise
 from hunter_policy import C,LANES,VERSION,POLICY,fresh,stamp,chase_blockers
ROOT=pathlib.Path("research/results")
SCAN=ROOT/"hunter-cex-universe-run.json"
OUT=ROOT/"hunter-early-signals.json"
HISTORY=ROOT/"hunter-early-signal-history.json"
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

def micro(symbols):
    """15m PRE_MOVE features: volume acceleration, compression, range position and short momentum."""
    def one(sym):
        q=urllib.parse.urlencode({"symbol":sym,"interval":"15m","limit":25})
        rows=get(BN+"/api/v3/klines?"+q,8)
        if not isinstance(rows,list) or len(rows)<25:return None
        closes=[finite(x[4]) for x in rows]; highs=[finite(x[2]) for x in rows]; lows=[finite(x[3]) for x in rows]
        vols=[finite(x[7]) or 0 for x in rows]
        if any(x is None or x<=0 for x in closes+highs+lows):return None
        recent=sum(vols[-2:])/2; baseline=sum(vols[-14:-2])/12
        vol_accel=recent/baseline if baseline>0 else 0
        def rng(seqh,seql):
            hi=max(seqh);lo=min(seql);mid=(hi+lo)/2
            return (hi-lo)/mid*100 if mid>0 else 999
        range_recent=rng(highs[-8:],lows[-8:]); range_prior=rng(highs[-24:-8],lows[-24:-8])
        compression=range_prior/max(range_recent,0.05)
        mom15=(closes[-1]/closes[-2]-1)*100
        mom60=(closes[-1]/closes[-5]-1)*100
        hi24=max(highs[-24:]); lo24=min(lows[-24:])
        range_pos=(closes[-1]-lo24)/(hi24-lo24) if hi24>lo24 else .5
        return sym,{"volume_acceleration":vol_accel,"compression_ratio":compression,
                    "return_15m_pct":mom15,"return_60m_pct":mom60,"range_position":range_pos,
                    "v2_candle_receipt":{"schema":"hunter_v2_candles_v1","exchange":"binance","market":"spot",
                        "symbol":sym,"interval":"15m","fetched_at":dt.datetime.now(dt.timezone.utc).isoformat(),
                        "source":"/api/v3/klines","rows":rows}}
    out={}
    with ThreadPoolExecutor(max_workers=64) as ex:
        futs={ex.submit(one,s):s for s in symbols}
        for fut in as_completed(futs):
            try:
                row=fut.result()
                if row:out[row[0]]=row[1]
            except Exception:continue
    return out

def score_row(sym,base,r1,r4,btc1,btc4,microdata=None):
    a=r1.get(sym);b=r4.get(sym)
    if not a or not b or not btc1 or not btc4:return None
    rel1=a["return_pct"]-btc1["return_pct"];rel4=b["return_pct"]-btc4["return_pct"]
    accel=rel1-rel4/4.0
    m=microdata.get(sym,{}) if microdata else {}
    va=finite(m.get("volume_acceleration")) or 0.; comp=finite(m.get("compression_ratio")) or 0.
    m15=finite(m.get("return_15m_pct")) or 0.; rp=finite(m.get("range_position")) or .5
    pre_volume=va>=C["EARLY_VOLUME_ACCEL"]; pre_compression=comp>=C["EARLY_COMPRESSION"]; pre_turn=(m15>=0 and rel1>=C["EARLY_TURN_REL"])
    # Early attention only: bounded micro evidence supplements, never replaces, BTC-relative evidence.
    score=max(0,rel1)*2+max(0,rel4)+max(0,accel)*1.5 + min(max(va-1,0),2)*.5 + min(max(comp-1,0),2)*.35
    independent=sum((rel1>=C["MIN_REL_1H"],rel4>=C["MIN_REL_4H"],accel>=C["MIN_ACCEL"],pre_volume,pre_compression,pre_turn))
    stage="EARLY" if independent>=C["DISCOVERY_MIN_INDEPENDENT"] else "WATCH"
    return {"base":base,"pair":sym,"stage":stage,"score":round(score,4),
      "btc_relative_1h_pct":round(rel1,4),"btc_relative_4h_pct":round(rel4,4),
      "relative_acceleration_pct":round(accel,4),
      "return_1h_pct":round(a["return_pct"],4),"return_4h_pct":round(b["return_pct"],4),
      "independent_signal_count":independent,
      "volume_acceleration_15m":round(va,4),"compression_ratio":round(comp,4),
      "return_15m_pct":round(m15,4),"range_position_6h":round(rp,4),
      "pre_move_components":{"volume":pre_volume,"compression":pre_compression,"relative_turn":pre_turn},
      "research_only":True}
def build(scan,r1,r4,microdata,now,regional_signals=None):
    pairs={p["pair"]:base for base,c in (scan.get("coins") or {}).items()
           for p in c.get("pairs") or [] if p.get("venue")=="binance"}
    btc1=r1.get("BTCUSDT");btc4=r4.get("BTCUSDT")
    if not btc1 or not btc4:raise ValueError("BTC benchmark missing")
    rows=[x for sym,base in pairs.items() if (x:=score_row(sym,base,r1,r4,btc1,btc4,microdata))]
    bybit_only={base for base,c in (scan.get("coins") or {}).items()
                if "bybit" in (c.get("venues") or []) and "binance" not in (c.get("venues") or [])}
    signals=regional_signals or {}
    for base in sorted(bybit_only & signals.keys()):
        row=signals[base]
        if row.get("base")==base and row.get("source_venue")=="bybit" and row.get("execution_supported") is False:
            rows.append(row)
    unscored=sorted(bybit_only-{x["base"] for x in rows})
    rows.sort(key=lambda x:(x["stage"]!="EARLY",-x["score"],x["base"]))
    early=[x for x in rows if x["stage"]=="EARLY"]
    report={"schema":"hunter_early_signals_v4","as_of_utc":now.isoformat(),
      "scan_generation_id":scan.get("generation_id"),"capital_authority":"NONE_RESEARCH_ONLY",
      "method":"Venue-matched 1h/4h BTC-relative strength + relative acceleration; no 24h-gain prerequisite",
      "policy_version":VERSION,"early_count":len(early),"early":early,"all_signals":rows,"watch":rows[:30]}
    report["bybit_only_unscored"]=unscored
    report["bybit_only_signal_count"]=len(bybit_only)-len(unscored)
    return report
def persist_first_early(report):
    """Durable first-seen EARLY evidence for lead-time/missed-opportunity audits."""
    try: hist=json.loads(HISTORY.read_text())
    except (OSError,ValueError): hist={"schema":"hunter_early_signal_history_v1","assets":{}}
    assets=hist.setdefault("assets",{}); now=report.get("as_of_utc")
    for row in report.get("early") or []:
        a=row.get("base"); p=finite((((json.loads(SCAN.read_text()).get("coins") or {}).get(a) or {}).get("reference_price")))
        if not a: continue
        rec=assets.setdefault(a,{"first_early_at_utc":now,"first_early_price":p,"first_generation_id":report.get("scan_generation_id"),"first_signal":row,"observations":0})
        rec["observations"]=int(rec.get("observations") or 0)+1
        rec["last_early_at_utc"]=now; rec["last_early_price"]=p; rec["last_signal"]=row
    hist["updated_at_utc"]=now
    tmp=HISTORY.with_suffix(".json.tmp"); tmp.write_text(json.dumps(hist,ensure_ascii=False,indent=2)+"\n"); json.loads(tmp.read_text()); tmp.replace(HISTORY)

def main():
    scan=json.loads(SCAN.read_text())
    if not scan.get("binance_complete"):raise SystemExit("incomplete Binance scan")
    pairs=[p["pair"] for c in (scan.get("coins") or {}).values()
           for p in c.get("pairs") or [] if p.get("venue")=="binance"]
    symbols=sorted(set(pairs+["BTCUSDT"]))
    r1=rolling(symbols,"1h");r4=rolling(symbols,"4h");microdata=micro(symbols)
    regional_signals={}
    if scan.get("bybit_complete"):
        worker_source=(scan.get("venue_status") or {}).get("bybit",{}).get("source")=="OFFICIAL_BYBIT_V5_VIA_WORKER"
        try:
            from research import hunter_bybit_regional as regional
            from research import hunter_bybit_worker as worker
        except ModuleNotFoundError:
            import hunter_bybit_regional as regional
            import hunter_bybit_worker as worker
        path=worker.DEFAULT if worker_source else pathlib.Path(os.getenv("HUNTER_BYBIT_REGIONAL_SNAPSHOT",
            "research/results/hunter-bybit-regional-snapshot.json"))
        snapshot=json.loads(path.read_text())
        (worker if worker_source else regional).validate(snapshot,dt.datetime.now(dt.timezone.utc))
        if worker_source and snapshot["snapshot_sha256"]!=(scan.get("venue_status") or {}).get("bybit",{}).get("snapshot_sha256"):
            raise SystemExit("BYBIT_SIGNAL_SNAPSHOT_HASH_MISMATCH")
        if snapshot["captured_at_utc"]!=(scan.get("venue_status") or {}).get("bybit",{}).get("captured_at_utc"):
            raise SystemExit("BYBIT_SIGNAL_SNAPSHOT_GENERATION_MISMATCH")
        captured=dt.datetime.fromisoformat(snapshot["captured_at_utc"].replace("Z","+00:00"))
        # A valid universe snapshot (45 minutes) is not necessarily a fresh
        # entry signal. Keep stale Bybit assets visible as unscored research.
        if 0 <= (dt.datetime.now(dt.timezone.utc)-captured).total_seconds() <= 900:
            regional_signals=snapshot.get("early_signals") or {}
    report=build(scan,r1,r4,microdata,dt.datetime.now(dt.timezone.utc),regional_signals)
    OUT.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
    persist_first_early(report)
    print(json.dumps({"early_count":report["early_count"],"top":report["early"][:10]}))
if __name__=="__main__":main()
