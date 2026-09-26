#!/usr/bin/env python3
"""Append-only, point-in-time Hunter prospective discovery/outcome audit.

Never uses future returns for selection, promotion or backdating. No orders.
"""
import datetime as dt
import json
import pathlib

ROOT=pathlib.Path("research/results")
SCAN=ROOT/"hunter-cex-universe-run.json"
RESEARCH=ROOT/"hunter-forward-research.json"
OUT=ROOT/"hunter-forward-audit.json"
SUMMARY=ROOT/"hunter-forward-audit-summary.json"
HORIZONS={"24h":24,"7d":168,"30d":720,"90d":2160}

def load(path,default):
    try:return json.loads(path.read_text())
    except (OSError,ValueError):return default

def parse(s):
    return dt.datetime.fromisoformat(s.replace("Z","+00:00"))

def venue_price(coins,sym,venue):
    for p in (coins.get(sym) or {}).get("pairs",[]):
        if p.get("venue")==venue and p.get("price",0)>0:return p["price"]
    return None

def freeze_event(sym,coin,research,coins,at):
    venue=coin.get("reference_venue")
    price=venue_price(coins,sym,venue)
    btc=venue_price(coins,"BTC",venue)
    if not price or not btc:return None
    obs=research.get("observations") or {}
    return {"asset":sym,"first_detected_at_utc":at,
            "venue":venue,"reference_price":price,"btc_reference_price":btc,
            "research_attention_signals":list(research.get("research_attention_signals") or []),
            "stage_price_only":research.get("stage_price_only"),
            "evidence_snapshot":{"market_cap":obs.get("market_cap"),
                "fully_diluted_valuation":obs.get("fully_diluted_valuation"),
                "defillama_tvl_usd":obs.get("defillama_tvl_usd"),
                "market_structure":obs.get("market_structure"),
                "missing_facts":list(research.get("missing_facts") or []),
                "sources":list(research.get("source_urls") or [])},
            "research_status":"RESEARCH_ONLY_NOT_A_BUY_SIGNAL",
            "outcomes":{}}

def settle(event,coins,now):
    start=parse(event["first_detected_at_utc"])
    elapsed=(now-start).total_seconds()/3600
    sym=event["asset"];venue=event["venue"]
    price=venue_price(coins,sym,venue)
    btc=venue_price(coins,"BTC",venue)
    for label,hours in HORIZONS.items():
        if label in event["outcomes"] or elapsed<hours:continue
        if not price or not btc:continue
        r=price/event["reference_price"]-1
        b=btc/event["btc_reference_price"]-1
        event["outcomes"][label]={"observed_at_utc":now.isoformat(),
            "actual_elapsed_hours":round(elapsed,2),
            "late_observation":elapsed>hours+2,
            "asset_return_pct":round(r*100,4),
            "btc_return_pct":round(b*100,4),
            "btc_relative_return_pct":round((r-b)*100,4),
            "price":price,"btc_price":btc}
    return event

def build(scan,research,previous,now):
    coins=scan.get("coins") or {}
    if not scan.get("binance_complete") or not coins or "BTC" not in coins:
        raise ValueError("Incomplete Binance/BTC baseline; no audit update")
    if research.get("universe_scan_as_of_utc")!=scan.get("as_of_utc"):
        raise ValueError("Research/market timestamp mismatch; no future or stale join")
    first=previous.get("first_seen") or {}
    events=previous.get("research_events") or []
    now_iso=now.isoformat()
    for sym,coin in coins.items():
        if sym not in first:
            venue=coin.get("reference_venue")
            p=venue_price(coins,sym,venue)
            b=venue_price(coins,"BTC",venue)
            if p and b:first[sym]={"first_seen_at_utc":scan["as_of_utc"],
                "venue":venue,"price":p,"btc_price":b,"outcomes":{}}
    for sym,snap in first.items():
        start=parse(snap["first_seen_at_utc"])
        elapsed=(now-start).total_seconds()/3600
        price=venue_price(coins,sym,snap["venue"])
        btc=venue_price(coins,"BTC",snap["venue"])
        for label,hours in HORIZONS.items():
            if label in snap["outcomes"] or elapsed<hours or not price or not btc:continue
            r=price/snap["price"]-1;b=btc/snap["btc_price"]-1
            snap["outcomes"][label]={"observed_at_utc":now_iso,
                "actual_elapsed_hours":round(elapsed,2),
                "late_observation":elapsed>hours+2,
                "asset_return_pct":round(r*100,4),
                "btc_return_pct":round(b*100,4),
                "btc_relative_return_pct":round((r-b)*100,4)}
    latest={}
    for e in events:latest[e["asset"]]=e
    added=[]
    # Only assets actually researched in this cycle can receive a new frozen
    # research snapshot; never backfill research signals to their first listing.
    for sym in research.get("rotation_batch") or []:
        res=(research.get("research_results") or {}).get(sym)
        coin=coins.get(sym)
        if not res or not coin or res.get("researched_at_utc")!=research.get("as_of_utc"):
            continue
        old=latest.get(sym)
        if old:
            hours=(now-parse(old["first_detected_at_utc"])).total_seconds()/3600
            changed=set(old.get("research_attention_signals") or [])!=set(res.get("research_attention_signals") or [])
            price=venue_price(coins,sym,old["venue"])
            repricing=bool(price and abs(price/old["reference_price"]-1)>=.2)
            if hours<168 and not ((changed or repricing) and hours>=6):
                continue
        event=freeze_event(sym,coin,res,coins,scan["as_of_utc"])
        if event:events.append(event);latest[sym]=event;added.append(sym)
    for event in events:settle(event,coins,now)
    summary={"schema":"hunter_forward_audit_v1","as_of_utc":now_iso,
        "market_snapshot_as_of_utc":scan["as_of_utc"],
        "baseline_first_seen_count":len(first),"frozen_research_event_count":len(events),
        "new_frozen_research_events":added,
        "outcomes":{label:{"baseline_n":sum(label in x["outcomes"] for x in first.values()),
            "research_event_n":sum(label in x["outcomes"] for x in events),
            "on_time_research_n":sum(label in x["outcomes"] and
                not x["outcomes"][label]["late_observation"] for x in events)}
            for label in HORIZONS},
        "interpretation":"Descriptive forward outcomes, not proof of causal alpha; no selection on future outcomes.",
        "capital_authority":"NONE"}
    return {"schema":"hunter_forward_audit_v1","as_of_utc":now_iso,
            "first_seen":first,"research_events":events},summary

def main():
    scan=load(SCAN,{})
    research=load(RESEARCH,{})
    now=dt.datetime.now(dt.timezone.utc)
    data,summary=build(scan,research,load(OUT,{}),now)
    OUT.write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n")
    SUMMARY.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps(summary,ensure_ascii=False))
    return 0

if __name__=="__main__":raise SystemExit(main())
