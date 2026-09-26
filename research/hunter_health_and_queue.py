#!/usr/bin/env python3
"""Machine-readable self-check and prioritized evidence queue, no trade authority."""
import datetime as dt
import json
import pathlib

ROOT=pathlib.Path("research/results")
PATHS={"scan":"hunter-cex-universe-run.json",
       "research":"hunter-forward-research.json",
       "identity":"hunter-identity-audit.json",
       "dossiers":"hunter-candidate-dossiers.json",
       "liquidity":"hunter-liquidity-probe.json",
       "audit":"hunter-forward-audit-summary.json"}
OUT=ROOT/"hunter-health-and-queue.json"

def parse(s):
    return dt.datetime.fromisoformat(s.replace("Z","+00:00"))

def build(data,now):
    scan=data["scan"];research=data["research"];identity=data["identity"]
    dossiers=data["dossiers"];liquidity=data["liquidity"];audit=data["audit"]
    stamp=scan.get("as_of_utc")
    mismatches={}
    for name,source_key in (("research","universe_scan_as_of_utc"),
                             ("identity","scan_as_of_utc"),
                             ("liquidity","scan_as_of_utc"),
                             ("audit","market_snapshot_as_of_utc")):
        if data[name].get(source_key)!=stamp:
            mismatches[name]={"expected":stamp,"actual":data[name].get(source_key)}
    if dossiers.get("market_universe_size")!=len(scan.get("coins") or {}):
        mismatches["dossiers_universe"]={"expected":len(scan.get("coins") or {}),
                                        "actual":dossiers.get("market_universe_size")}
    scan_age=(now-parse(stamp)).total_seconds()/3600
    if scan_age<0 or scan_age>2:
        mismatches["scan_freshness"]={"age_hours":round(scan_age,2)}
    queue=[];probe=liquidity.get("snapshots") or {}
    for d in dossiers.get("dossiers") or []:
        sym=d["asset"];book=probe.get(sym)
        blockers=list(d.get("capital_gate_blockers") or [])
        if d.get("identity_status")!="THIRD_PARTY_CORROBORATED":
            blockers.append("IDENTITY_NEEDS_PRIMARY_AND_INDEPENDENT_CONTRACT")
        if not d.get("official_sources"):
            blockers.append("OFFICIAL_TOKENOMICS_AND_VALUE_CAPTURE_MISSING")
        if not d.get("scenario_map"):
            blockers.append("FORWARD_SUPPLY_AND_VALUATION_SCENARIO_MISSING")
        if not book:
            blockers.append("LIVE_ORDERBOOK_NOT_IN_TOP_16")
        else:
            age=(now-parse(book["as_of_utc"])).total_seconds()/3600
            if age<0 or age>1:
                blockers.append("ORDERBOOK_STALE")
            if book["spread_bps"]>50:
                blockers.append("SPREAD_GT_50_BPS")
            if min(book["bid_depth_2pct_usdt"],book["ask_depth_2pct_usdt"])<10000:
                blockers.append("TWO_SIDED_VISIBLE_DEPTH_LT_10000_USDT")
        queue.append({"asset":sym,"cohort":d.get("opportunity_cohort"),
                      "status":d.get("status"),"blockers":list(dict.fromkeys(blockers)),
                      "has_current_orderbook":bool(book) and "ORDERBOOK_STALE" not in blockers,
                      "needs_human_primary_source_review":not d.get("official_sources"),
                      "capital_ready":d.get("capital_ready") is True})
    # Balanced, bounded evidence queue; no expected-return ranking.
    early=[x for x in queue if x["cohort"]=="EARLY_FLOW_ATTENTION"]
    cont=[x for x in queue if x["cohort"]=="CONTINUATION_FORWARD_UPSIDE_ATTENTION"]
    other=[x for x in queue if x not in early and x not in cont]
    priority=early[:8]+cont[:8]+other[:8]
    health={"snapshot_consistent":not mismatches,
            "snapshot_mismatches":mismatches,
            "binance_complete":scan.get("binance_complete") is True,
            "bybit_complete":scan.get("bybit_complete") is True,
            "bybit_error":(scan.get("errors") or {}).get("bybit"),
            "universe_size":len(scan.get("coins") or {}),
            "researched_cached":research.get("deep_research_total_cached"),
            "unresearched":dossiers.get("unresearched_market_count"),
            "dossiers":len(queue),
            "identity_corroborated":(identity.get("counts") or {}).get("THIRD_PARTY_CORROBORATED",0),
            "liquidity_requested":liquidity.get("requested_count"),
            "liquidity_successful":liquidity.get("successful_count"),
            "liquidity_failures":liquidity.get("failures") or {},
            "forward_24h_sample_n":(audit.get("outcomes") or {}).get("24h",{}).get("research_event_n",0),
            "capital_ready":sum(x["capital_ready"] for x in queue)}
    if not health["binance_complete"] or mismatches:
        health["status"]="PIPELINE_INCONSISTENT"
    elif health["liquidity_successful"]==0:
        health["status"]="LIQUIDITY_DEGRADED"
    elif health["capital_ready"]==0:
        health["status"]="RESEARCH_RUNNING_CAPITAL_GATES_UNRESOLVED"
    else:health["status"]="REVIEW_ELIGIBLE_CANDIDATES_PRESENT"
    return {"schema":"hunter_health_queue_v1","as_of_utc":now.isoformat(),
            "scan_as_of_utc":stamp,"health":health,
            "research_priority_queue":priority,
            "all_candidate_blockers":queue,
            "capital_authority":"NONE__NEVER_AUTO_EXECUTE"}

def main():
    data={k:json.loads((ROOT/v).read_text()) for k,v in PATHS.items()}
    report=build(data,dt.datetime.now(dt.timezone.utc))
    OUT.write_text(json.dumps(report,indent=2,ensure_ascii=False)+"\n")
    print(json.dumps(report["health"],ensure_ascii=False))
    # Mismatched snapshots are fatal, never report an inconsistent scan as success.
    return 0 if report["health"]["snapshot_consistent"] else 2

if __name__=="__main__":raise SystemExit(main())
