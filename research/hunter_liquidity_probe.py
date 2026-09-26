#!/usr/bin/env python3
"""Fresh public Binance spot order-book evidence for both Hunter research lanes.

A public depth response is market microstructure evidence, NOT verified project
fundamentals or a capital authorization. All failures are explicit.
"""
import datetime as dt
import json
import math
import os
import pathlib
import urllib.parse
import urllib.request

ROOT=pathlib.Path("research/results")
SCAN=ROOT/"hunter-cex-universe-run.json"
DOSSIERS=ROOT/"hunter-candidate-dossiers.json"
OUT=ROOT/"hunter-liquidity-probe.json"
BN=os.getenv("HUNTER_BINANCE_API","https://data-api.binance.vision")
PER_LANE=8

def read(path):
    return json.loads(path.read_text())

def finite(v):
    try:
        x=float(v)
        return x if math.isfinite(x) else None
    except (TypeError,ValueError,OverflowError):
        return None

def measure(book,now):
    bids=book.get("bids") or []
    asks=book.get("asks") or []
    if not bids or not asks:
        raise ValueError("EMPTY_BOOK")
    b=finite(bids[0][0]);a=finite(asks[0][0])
    if b is None or a is None or b<=0 or a<=b:
        raise ValueError("INVALID_OR_CROSSED_BOOK")
    mid=(a+b)/2
    spread=(a-b)/mid*10000
    def side_depth(rows,side):
        depth=0.0
        for item in rows:
            p=finite(item[0]);q=finite(item[1])
            if p is None or q is None or p<=0 or q<0:
                raise ValueError("INVALID_LEVEL")
            if (side=="bid" and p>=mid*.98) or (side=="ask" and p<=mid*1.02):
                depth+=p*q
        return round(depth,2)
    return {"as_of_utc":now.isoformat(),"best_bid":b,"best_ask":a,
            "spread_bps":round(spread,3),
            "bid_depth_2pct_usdt":side_depth(bids,"bid"),
            "ask_depth_2pct_usdt":side_depth(asks,"ask"),
            "depth_levels_per_side":min(len(bids),len(asks)),
            "partial_book":True,
            "capital_authority":"NONE__MARKET_EVIDENCE_ONLY"}

def targets(dossiers,scan):
    coins=scan.get("coins") or {}
    selected=[];seen=set()
    for lane in ("early_entry_watchlist","continuation_watchlist"):
        n=0
        for sym in dossiers.get(lane) or []:
            if n>=PER_LANE:break
            coin=coins.get(sym) or {}
            pairs=[p for p in coin.get("pairs") or [] if p.get("venue")=="binance"]
            if not pairs or sym in seen:continue
            pair=max(pairs,key=lambda p:finite(p.get("volume_24h_usdt")) or 0)
            selected.append((sym,pair["pair"],lane))
            seen.add(sym);n+=1
    return selected

def build(scan,dossiers,fetch,now):
    if dossiers.get("market_universe_size")!=len(scan.get("coins") or {}):
        raise ValueError("UNIVERSE_MISMATCH")
    if dossiers.get("dossiers") is None:raise ValueError("MISSING_DOSSIERS")
    if (now-dt.datetime.fromisoformat(scan["as_of_utc"])).total_seconds()>7200:
        raise ValueError("STALE_SCAN")
    records={};failures={}
    for sym,pair,lane in targets(dossiers,scan):
        url=BN+"/api/v3/depth?"+urllib.parse.urlencode({"symbol":pair,"limit":100})
        try:
            book=fetch(url)
            snapshot=measure(book,dt.datetime.now(dt.timezone.utc))
            snapshot.update(pair=pair,venue="binance",lane=lane)
            records[sym]=snapshot
        except Exception as exc:
            failures[sym]=type(exc).__name__+": "+str(exc)[:160]
    return {"schema":"hunter_liquidity_probe_v1","as_of_utc":now.isoformat(),
            "scan_as_of_utc":scan["as_of_utc"],
            "requested_count":len(targets(dossiers,scan)),
            "successful_count":len(records),"failures":failures,
            "snapshots":records,"capital_authority":"NONE__OFFICIAL_FACTS_AND_PORTFOLIO_GATES_SEPARATE"}

def live_fetch(url):
    req=urllib.request.Request(url,headers={"User-Agent":"hunter-liquidity-probe/1.0","Accept":"application/json"})
    with urllib.request.urlopen(req,timeout=6) as response:
        return json.load(response)

def main():
    scan=read(SCAN);dossiers=read(DOSSIERS)
    report=build(scan,dossiers,live_fetch,dt.datetime.now(dt.timezone.utc))
    OUT.write_text(json.dumps(report,indent=2,ensure_ascii=False)+"\n")
    print(json.dumps({k:report[k] for k in ("as_of_utc","requested_count","successful_count","failures")},ensure_ascii=False))
    # Public API geo-blocks are explicit degradation, not fabricated successes.
    return 0

if __name__=="__main__":
    raise SystemExit(main())
