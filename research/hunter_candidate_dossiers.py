#!/usr/bin/env python3
"""Produce actionable *research questions*, never buy orders, for forward upside.

Only a dated, independently verified analyst fact registry can unlock arithmetic
scenario maps. Protocol TVL, past rallies and volume are NOT token valuations.
"""
import datetime as dt
import json
import pathlib

ROOT=pathlib.Path("research/results")
RESEARCH=ROOT/"hunter-forward-research.json"
SCAN=ROOT/"hunter-cex-universe-run.json"
FACTS=pathlib.Path("research/hunter-verified-facts.json")
OUT=ROOT/"hunter-candidate-dossiers.json"
MAX_DOSSIERS=25

def read(path,default):
    try:return json.loads(path.read_text())
    except (OSError,ValueError):return default

def parse(s):
    return dt.datetime.fromisoformat(s.replace("Z","+00:00"))

def scenario_map(facts,entry,now):
    """Calculate hypothetical forward prices, NEVER assign probabilities."""
    required=("verified_at_utc","official_sources","contract_verified",
              "forward_supply_verified","token_value_capture_verified",
              "credible_catalyst_verified","supply_future",
              "bear_market_cap_usd","base_market_cap_usd","bull_market_cap_usd")
    missing=[k for k in required if not facts.get(k)]
    if missing:return None,["Verified fact missing: "+x for x in missing]
    if not isinstance(facts["official_sources"],list) or len(facts["official_sources"])<1:
        return None,["Official evidence source list invalid"]
    if any(not isinstance(x,str) or not x.startswith("https://") for x in facts["official_sources"]):
        return None,["Official evidence URL invalid"]
    try:
        age=(now-parse(facts["verified_at_utc"])).total_seconds()/3600
        if age<0 or age>336:return None,["Verified facts stale (>14d) or future dated"]
        supply=float(facts["supply_future"])
        caps=[float(facts[x]) for x in ("bear_market_cap_usd","base_market_cap_usd","bull_market_cap_usd")]
        if supply<=0 or entry<=0 or any(x<=0 for x in caps) or caps!=sorted(caps):
            return None,["Scenario inputs nonpositive or market caps not ordered"]
    except (TypeError,ValueError,OverflowError,KeyError):
        return None,["Scenario inputs invalid"]
    prices=[cap/supply for cap in caps]
    return {"status":"HYPOTHETICAL_SCENARIOS_NOT_PREDICTIONS",
            "assumption":"Analyst-provided market-cap scenarios divided by verified forward circulating supply",
            "entry_price":entry,"supply_future":supply,
            "bear_price":round(prices[0],8),
            "base_price":round(prices[1],8),
            "bull_price":round(prices[2],8),
            "bear_return_pct":round((prices[0]/entry-1)*100,2),
            "base_return_pct":round((prices[1]/entry-1)*100,2),
            "bull_return_pct":round((prices[2]/entry-1)*100,2),
            "probabilities":"UNKNOWN__DO_NOT_FABRICATE",
            "btc_relative_status":"REQUIRES_SAME_HORIZON_VERIFIED_BTC_SCENARIOS",
            "capital_authority":"NONE__PORTFOLIO_LIQUIDITY_AND_EXECUTION_GATES_PENDING"},[]

def prioritize(research,scan):
    coins=scan.get("coins") or {}
    reviewed=research.get("research_results") or {}
    triggered=set(research.get("triggered_researched") or [])
    backlog=set(research.get("trigger_backlog") or [])
    rows=[]
    for sym,item in reviewed.items():
        coin=coins.get(sym)
        if not coin:continue
        obs=item.get("observations") or {}
        signals=item.get("research_attention_signals") or []
        market=obs.get("market_structure") or {}
        # Attention triage, not estimated returns or a BUY ranking. Previously
        # rallied and declined assets use identical rules.
        fresh=item.get("researched_at_utc")==research.get("as_of_utc")
        has_market=bool(market)
        has_cap=obs.get("market_cap") is not None
        has_tvl=obs.get("defillama_tvl_usd") is not None
        attention=(3 if sym in triggered else 0)+(2 if sym in backlog else 0)+len(signals)
        evidence_count=int(has_market)+int(has_cap)+int(has_tvl)
        rows.append((sym,item,coin,attention,evidence_count,fresh))
    rows.sort(key=lambda r:(-r[3],-r[4],not r[5],r[0]))
    return rows

def build(research,scan,registry,now):
    if research.get("universe_scan_as_of_utc")!=scan.get("as_of_utc"):
        raise ValueError("Refuse mismatched research and exchange snapshot")
    if not scan.get("binance_complete"):raise ValueError("Incomplete Binance coverage")
    if (now-parse(scan["as_of_utc"])).total_seconds()>7200:
        raise ValueError("Exchange snapshot stale (>2h)")
    cases=[];full=prioritize(research,scan)
    facts_all=registry.get("assets") or {}
    for sym,item,coin,attention,evidence_count,fresh in full[:MAX_DOSSIERS]:
        fact=facts_all.get(sym) or {}
        entry=float(coin["reference_price"])
        scen,missing=scenario_map(fact,entry,now)
        case={"asset":sym,"as_of_utc":now.isoformat(),
              "exchange_price_as_of_utc":scan["as_of_utc"],
              "entry_reference_price":entry,"venue":coin.get("reference_venue"),
              "prior_rally_never_auto_rejects":True,
              "research_attention_signals":item.get("research_attention_signals") or [],
              "nonprice_observations":{k:v for k,v in (item.get("observations") or {}).items()
                  if k not in ("market_structure",)},
              "market_structure":(item.get("observations") or {}).get("market_structure"),
              "research_source_urls":item.get("source_urls") or [],
              "official_sources":fact.get("official_sources") or [],
              "scenario_map":scen,"research_questions":list(dict.fromkeys(
                  (item.get("missing_facts") or [])+missing)),
              "attention_reasons":{"triggered":sym in set(research.get("triggered_researched") or []),
                  "attention_signal_count":len(item.get("research_attention_signals") or []),
                  "evidence_fields_present":evidence_count},
              "status":"SCENARIO_RESEARCH_READY" if scen else "RESEARCH_INCOMPLETE",
              "capital_ready":False,"trade_action":"NONE"}
        cases.append(case)
    return {"schema":"hunter_candidate_dossiers_v1","as_of_utc":now.isoformat(),
            "market_universe_size":len(scan.get("coins") or {}),
            "deep_research_cached_count":len(research.get("research_results") or {}),
            "dossier_count":len(cases),"unresearched_market_count":max(0,
                len(scan.get("coins") or {})-len(research.get("research_results") or {})),
            "selection_note":"Research attention and evidence availability only, NOT expected return ranking.",
            "scenario_policy":"No numeric price targets without dated official verified supply, token capture and explicit market-cap assumptions.",
            "capital_ready":[],"buy_proposals":[],"dossiers":cases}

def main():
    now=dt.datetime.now(dt.timezone.utc)
    report=build(read(RESEARCH,{}),read(SCAN,{}),read(FACTS,{}),now)
    OUT.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps({k:report[k] for k in ("as_of_utc","market_universe_size",
        "deep_research_cached_count","dossier_count","unresearched_market_count",
        "capital_ready")},ensure_ascii=False))
    return 0

if __name__=="__main__":raise SystemExit(main())
