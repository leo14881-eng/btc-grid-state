#!/usr/bin/env python3
"""Produce actionable *research questions*, never buy orders, for forward upside.

Only a dated, independently verified analyst fact registry can unlock arithmetic
scenario maps. Protocol TVL, past rallies and volume are NOT token valuations.
"""
import datetime as dt
import json
import pathlib
import math

ROOT=pathlib.Path("research/results")
RESEARCH=ROOT/"hunter-forward-research.json"
SCAN=ROOT/"hunter-cex-universe-run.json"
FACTS=pathlib.Path("research/hunter-verified-facts.json")
IDENTITY=ROOT/"hunter-identity-audit.json"
LIQUIDITY=ROOT/"hunter-liquidity-probe.json"
OUT=ROOT/"hunter-candidate-dossiers.json"
MAX_DOSSIERS=40
EARLY_QUOTA=16
CONTINUATION_QUOTA=12

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
        if not all(math.isfinite(x) for x in [supply,entry]+caps) or supply<=0 or entry<=0 or any(x<=0 for x in caps) or caps!=sorted(caps):
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

def classify_cohort(item):
    """Parallel opportunity lanes; a past rally never disqualifies an asset."""
    m=(item.get("observations") or {}).get("market_structure") or {}
    change=m.get("return_vs_7_completed_days_pct")
    volume=m.get("volume_7d_ratio")
    if isinstance(change,(float,int)) and isinstance(volume,(float,int)):
        if -10<=change<=20 and volume>=1.3:
            return "EARLY_FLOW_ATTENTION"
        if change>20 and volume>=1.3:
            return "CONTINUATION_FORWARD_UPSIDE_ATTENTION"
    return "ROTATING_FUNDAMENTALS_OR_UNCONFIRMED"


def balanced_candidates(full):
    """Reserve separate lanes so recent winners cannot crowd out early setups."""
    early=[r for r in full if classify_cohort(r[1])=="EARLY_FLOW_ATTENTION"]
    cont=[r for r in full if classify_cohort(r[1])=="CONTINUATION_FORWARD_UPSIDE_ATTENTION"]
    # Within the early lane use independently observed flow and evidence, not
    # alphabetic ticker order or 24h price appreciation. This is research
    # triage only, never a predicted-return or buy ranking.
    def early_key(row):
        obs=row[1].get("observations") or {}
        market=obs.get("market_structure") or {}
        vol=market.get("volume_7d_ratio") or 0
        cap=obs.get("market_cap")
        # 1.5x-6x is the non-extreme flow band; extreme spikes are still
        # eligible for continuation/overflow research, never banned.
        controlled=1.5<=vol<=6
        return (-int(controlled),-int(cap is not None),-min(vol,6),
                -int(row[5]),row[0])
    early.sort(key=early_key)
    selected=[];seen=set()
    for group,quota in ((early,EARLY_QUOTA),(cont,CONTINUATION_QUOTA),(full,MAX_DOSSIERS)):
        for row in group:
            if len(selected)>=MAX_DOSSIERS or quota<=0:break
            if row[0] not in seen:
                selected.append(row);seen.add(row[0]);quota-=1
    return selected,{"early_available":len(early),"continuation_available":len(cont),
        "early_selected":sum(classify_cohort(r[1])=="EARLY_FLOW_ATTENTION" for r in selected),
        "continuation_selected":sum(classify_cohort(r[1])=="CONTINUATION_FORWARD_UPSIDE_ATTENTION" for r in selected)}


def capital_gate(fact,scenario,entry,now):
    """No permanent false gate: independently documented facts can unlock review.

    This is a proposal eligibility check, never an automatic exchange order.
    """
    blockers=[]
    if scenario is None:
        return False,["EVIDENCE_GATED_SCENARIO_UNAVAILABLE"]
    required=("liquidity_verified_at_utc","portfolio_verified_at_utc",
              "drawdown_budget_verified","counterparty_verified",
              "btc_same_horizon_base_return_pct","liquidity_max_spread_bps",
              "liquidity_orderbook_depth_2pct_usdt","portfolio_open_cost_usdt",
              "portfolio_pending_reservations_usdt","max_proposed_new_cost_usdt")
    for key in required:
        if key not in fact or fact[key] is None:
            blockers.append("MISSING_"+key)
    if blockers:return False,blockers
    try:
        for key in ("liquidity_verified_at_utc","portfolio_verified_at_utc"):
            age=(now-parse(fact[key])).total_seconds()/3600
            if age<0 or age>1: blockers.append("STALE_"+key)
        if not fact["drawdown_budget_verified"]:blockers.append("DRAWDOWN_BUDGET_UNVERIFIED")
        if not fact["counterparty_verified"]:blockers.append("COUNTERPARTY_UNVERIFIED")
        spread=float(fact["liquidity_max_spread_bps"])
        depth=float(fact["liquidity_orderbook_depth_2pct_usdt"])
        open_cost=float(fact["portfolio_open_cost_usdt"])
        pending=float(fact["portfolio_pending_reservations_usdt"])
        proposal=float(fact["max_proposed_new_cost_usdt"])
        btc_base=float(fact["btc_same_horizon_base_return_pct"])
        if spread<0 or spread>50:blockers.append("SPREAD_EXCEEDS_50_BPS")
        if depth<max(10000,proposal*10):blockers.append("DEPTH_INSUFFICIENT")
        if not all(math.isfinite(x) for x in (spread,depth,open_cost,pending,proposal,btc_base)):
            blockers.append("NONFINITE_CAPITAL_GATE_INPUT")
        if min(open_cost,pending,proposal)<0 or proposal<=0 or open_cost+pending+proposal>20000:
            blockers.append("ALT_POOL_20000_USDT_CAP")
        if scenario["base_return_pct"]<=btc_base:
            blockers.append("BASE_CASE_NOT_ABOVE_SAME_HORIZON_BTC")
        downside=abs(min(0,scenario["bear_return_pct"]))
        if downside<=0 or scenario["bull_return_pct"]/downside<2:
            blockers.append("UPSIDE_DOWNSIDE_BELOW_2")
    except (ValueError,TypeError,KeyError,OverflowError):
        blockers.append("INVALID_CAPITAL_GATE_INPUT")
    return not blockers,blockers


def fresh_execution_evidence(liquidity,scan,sym,now):
    """Public partial orderbook evidence only; never a fill guarantee."""
    if liquidity.get("scan_as_of_utc")!=scan.get("as_of_utc"):
        return {},["LIVE_ORDERBOOK_SCAN_MISMATCH"]
    snapshot=(liquidity.get("snapshots") or {}).get(sym) or {}
    pair=snapshot.get("pair")
    pairs={p.get("pair") for p in (scan.get("coins") or {}).get(sym,{}).get("pairs") or []
           if p.get("venue")=="binance"}
    if pair not in pairs or snapshot.get("venue")!="binance":
        return {},["LIVE_ORDERBOOK_VENUE_OR_PAIR_MISMATCH"]
    try:
        age=(now-parse(snapshot["as_of_utc"])).total_seconds()/3600
        if age<0 or age>1:
            return {},["LIVE_ORDERBOOK_STALE"]
        spread=float(snapshot["spread_bps"])
        bid=float(snapshot["bid_depth_2pct_usdt"])
        ask=float(snapshot["ask_depth_2pct_usdt"])
        if not all(math.isfinite(x) for x in (spread,bid,ask)) or min(spread,bid,ask)<0:
            return {},["LIVE_ORDERBOOK_INVALID"]
    except (ValueError,TypeError,KeyError,OverflowError):
        return {},["LIVE_ORDERBOOK_INVALID"]
    return {"liquidity_verified_at_utc":snapshot["as_of_utc"],
            "liquidity_max_spread_bps":spread,
            "liquidity_orderbook_depth_2pct_usdt":min(bid,ask)},[]


def build(research,scan,registry,now,identity=None,liquidity=None):
    if research.get("universe_scan_as_of_utc")!=scan.get("as_of_utc"):
        raise ValueError("Refuse mismatched research and exchange snapshot")
    if not scan.get("binance_complete"):raise ValueError("Incomplete Binance coverage")
    if (now-parse(scan["as_of_utc"])).total_seconds()>7200:
        raise ValueError("Exchange snapshot stale (>2h)")
    identity=identity or {}
    liquidity=liquidity or {}
    identity_fresh=(identity.get("scan_as_of_utc")==scan.get("as_of_utc"))
    identity_assets=identity.get("assets") or {}
    cases=[];full=prioritize(research,scan)
    selected,cohort_stats=balanced_candidates(full)
    facts_all=registry.get("assets") or {}
    for sym,item,coin,attention,evidence_count,fresh in selected:
        fact=facts_all.get(sym) or {}
        entry=float(coin["reference_price"])
        scen,missing=scenario_map(fact,entry,now)
        execution,evidence_blockers=fresh_execution_evidence(liquidity,scan,sym,now)
        capital_facts=dict(fact)
        # Prevent a manually entered liquidity claim from bypassing fresh API data.
        for key in ("liquidity_verified_at_utc","liquidity_max_spread_bps",
                    "liquidity_orderbook_depth_2pct_usdt"):
            capital_facts.pop(key,None)
        capital_facts.update(execution)
        ready,capital_blockers=capital_gate(capital_facts,scen,entry,now)
        capital_blockers.extend(evidence_blockers)
        ident=identity_assets.get(sym) or {}
        identity_pass=identity_fresh and ident.get("capital_identity_pass") is True
        if not identity_pass:
            ready=False
            capital_blockers.append("CONTRACT_IDENTITY_NOT_CORROBORATED_OR_STALE")
        case={"asset":sym,"as_of_utc":now.isoformat(),
              "exchange_price_as_of_utc":scan["as_of_utc"],
              "entry_reference_price":entry,"venue":coin.get("reference_venue"),
              "prior_rally_never_auto_rejects":True,
              "opportunity_cohort":classify_cohort(item),
              "research_attention_signals":item.get("research_attention_signals") or [],
              "nonprice_observations":{k:v for k,v in (item.get("observations") or {}).items()
                  if k not in ("market_structure",)},
              "market_structure":(item.get("observations") or {}).get("market_structure"),
              "research_source_urls":item.get("source_urls") or [],
              "official_sources":fact.get("official_sources") or [],
              "identity_status":ident.get("identity_status","AUDIT_MISSING"),
              "identity_blockers":ident.get("blockers") or ["IDENTITY_AUDIT_MISSING"],
              "live_orderbook_evidence":execution,
              "scenario_map":scen,"capital_gate_blockers":capital_blockers,
              "research_questions":list(dict.fromkeys(
                  (item.get("missing_facts") or [])+missing)),
              "attention_reasons":{"triggered":sym in set(research.get("triggered_researched") or []),
                  "attention_signal_count":len(item.get("research_attention_signals") or []),
                  "evidence_fields_present":evidence_count},
              "status":"CAPITAL_REVIEW_ELIGIBLE" if ready else
                  ("SCENARIO_RESEARCH_READY" if scen else "RESEARCH_INCOMPLETE"),
              "capital_ready":ready,"trade_action":"USER_REVIEW_REQUIRED" if ready else "NONE"}
        cases.append(case)
    return {"schema":"hunter_candidate_dossiers_v1","as_of_utc":now.isoformat(),
            "scan_as_of_utc":scan["as_of_utc"],
            "market_universe_size":len(scan.get("coins") or {}),
            "deep_research_cached_count":len(research.get("research_results") or {}),
            "dossier_count":len(cases),"cohort_coverage":cohort_stats,
            "early_entry_watchlist":[x["asset"] for x in cases if x["opportunity_cohort"]=="EARLY_FLOW_ATTENTION"],
            "continuation_watchlist":[x["asset"] for x in cases if x["opportunity_cohort"]=="CONTINUATION_FORWARD_UPSIDE_ATTENTION"],
            "unresearched_market_count":max(0,
                len(scan.get("coins") or {})-len(research.get("research_results") or {})),
            "selection_note":"Research attention and evidence availability only, NOT expected return ranking.",
            "scenario_policy":"No numeric price targets without dated official verified supply, token capture and explicit market-cap assumptions.",
            "capital_ready":[x["asset"] for x in cases if x["capital_ready"]],
            "buy_proposals":[{"asset":x["asset"],"status":"USER_REVIEW_REQUIRED_NOT_AN_ORDER",
                "scenario_map":x["scenario_map"]} for x in cases if x["capital_ready"]],
            "dossiers":cases}

def main():
    now=dt.datetime.now(dt.timezone.utc)
    report=build(read(RESEARCH,{}),read(SCAN,{}),read(FACTS,{}),now,
                 read(IDENTITY,{}),read(LIQUIDITY,{}))
    OUT.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps({k:report[k] for k in ("as_of_utc","market_universe_size",
        "deep_research_cached_count","dossier_count","unresearched_market_count",
        "capital_ready")},ensure_ascii=False))
    return 0

if __name__=="__main__":raise SystemExit(main())
