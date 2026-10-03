#!/usr/bin/env python3
"""Tactical capital-review gate for short-horizon Binance spot opportunities.

This gate never places orders. It deliberately separates short-horizon trade
evidence from long-horizon fundamental valuation assumptions. Identity,
supply/unlock risk, live liquidity, BTC-relative strength and the 20k USDT
alt-pool constraint remain fail-closed.
"""
import datetime as dt,json,math,pathlib
ROOT=pathlib.Path("research/results")
SCAN=ROOT/"hunter-cex-universe-run.json"
EARLY=ROOT/"hunter-early-signals.json"
LIQ=ROOT/"hunter-liquidity-probe.json"
FACTS=pathlib.Path("research/hunter-verified-facts.json")
SUPPLY=ROOT/"hunter-tactical-supply-risk.json"
IDENTITY=ROOT/"hunter-identity-audit.json"
CAPITAL=pathlib.Path("research/hunter-capital-state.json")
OUT=ROOT/"hunter-tactical-capital-review.json"
MAX_ALT_POOL=20000.0

def read(p,d={}):
    try:return json.loads(p.read_text())
    except (OSError,ValueError):return d

def parse(s): return dt.datetime.fromisoformat(str(s).replace("Z","+00:00"))

def finite(v):
    try:
        x=float(v); return x if math.isfinite(x) else None
    except (TypeError,ValueError,OverflowError): return None

def main():
    now=dt.datetime.now(dt.timezone.utc)
    scan,early,liq,facts=read(SCAN),read(EARLY),read(LIQ),read(FACTS)
    supply=read(SUPPLY); identity=read(IDENTITY); capital=read(CAPITAL)
    supply_by=supply.get("assets") or {}; identity_by=identity.get("assets") or {}
    if identity.get("scan_as_of_utc")!=scan.get("as_of_utc"): raise SystemExit("IDENTITY_GENERATION_MISMATCH")
    pool=finite(capital.get("capital_pool_usdt")); known_open=finite(capital.get("open_cost_usdt")); known_pending=finite(capital.get("pending_reservations_usdt"))
    if pool!=MAX_ALT_POOL or known_open is None or known_pending is None: raise SystemExit("CAPITAL_STATE_INVALID")
    if not scan.get("binance_complete"): raise SystemExit("INCOMPLETE_BINANCE_SCAN")
    if early.get("scan_generation_id")!=scan.get("generation_id"): raise SystemExit("EARLY_GENERATION_MISMATCH")
    if liq.get("scan_as_of_utc")!=scan.get("as_of_utc"): raise SystemExit("LIQUIDITY_GENERATION_MISMATCH")
    early_by={x["base"]:x for x in early.get("early") or []}
    fact_by=facts.get("assets") or {}
    rows=[]
    for sym,sig in early_by.items():
        blockers=[]
        research_gaps=[]
        f=fact_by.get(sym) or {}
        supply_fact=supply_by.get(sym) or {}
        coin=(scan.get("coins") or {}).get(sym) or {}
        snap=(liq.get("snapshots") or {}).get(sym) or {}
        # Consume the current Identity Audit directly. Do not require a second,
        # disconnected per-asset contract_verified flag in verified-facts.
        ident=identity_by.get(sym) or {}
        if not ident.get("capital_identity_pass"):
            ib=ident.get("blockers") or []
            if any("MISMATCH" in str(x) or "CONFLICT" in str(x) for x in ib): blockers.append("ASSET_IDENTITY_MISMATCH")
            else: blockers.append("ASSET_IDENTITY_NOT_CORROBORATED")
        if not f.get("forward_supply_verified"): research_gaps.append("FORWARD_SUPPLY_UNLOCK_RESEARCH_INCOMPLETE")
        if not (f.get("tactical_supply_risk_verified") or supply_fact.get("tactical_supply_risk_verified")): research_gaps.append("TACTICAL_SUPPLY_RESEARCH_INCOMPLETE")
        # Identity freshness belongs to Identity Audit. Per-asset fundamental
        # timestamps are only relevant when the corresponding fact is asserted.
        if f.get("forward_supply_verified") or f.get("tactical_supply_risk_verified"):
            try:
                fact_age=(now-parse(f.get("verified_at_utc"))).total_seconds()/3600
                if fact_age<0 or fact_age>336: research_gaps.append("VERIFIED_FUNDAMENTAL_FACTS_STALE")
            except Exception: research_gaps.append("VERIFIED_FUNDAMENTAL_TIMESTAMP_INVALID")
        if not snap: blockers.append("LIVE_ORDERBOOK_MISSING")
        else:
            try:
                age=(now-parse(snap.get("as_of_utc"))).total_seconds()/3600
                spread=float(snap["spread_bps"])
                depth=min(float(snap["bid_depth_2pct_usdt"]),float(snap["ask_depth_2pct_usdt"]))
                if age<0 or age>1: blockers.append("LIVE_ORDERBOOK_STALE")
                if spread>50: blockers.append("SPREAD_EXCEEDS_50_BPS")
                if depth<30000: blockers.append("DEPTH_BELOW_30K_USDT")
            except Exception: blockers.append("LIVE_ORDERBOOK_INVALID")
        rel1=finite(sig.get("btc_relative_1h_pct")); rel4=finite(sig.get("btc_relative_4h_pct"))
        accel=finite(sig.get("relative_acceleration_pct"))
        if rel1 is None or rel4 is None or accel is None: blockers.append("BTC_RELATIVE_SIGNAL_MISSING")
        elif sum((rel1>=0.8,rel4>=1.5,accel>=0.5))<2: research_gaps.append("BTC_RELATIVE_CONFIRMATION_PENDING")
        proposal=min(4000.0,float(f.get("tactical_max_new_cost_usdt") or 3000.0))
        if known_open+known_pending+proposal>pool:
            blockers.append("ALT_POOL_20000_USDT_CAP")
        execution=(snap.get("execution_scenarios") or {}).get(str(int(proposal))) if snap else None
        independent=int(sig.get("independent_signal_count") or 0)
        score=finite(sig.get("score")) or 0.0
        early_strength=(independent>=2 and ((rel1 or 0)>=0.8 or (rel4 or 0)>=1.5) and score>=8)
        entry_stage="BLOCKED" if blockers else ("EARLY_ENTRY_REVIEW" if early_strength else "WATCH")
        first_tranche_allowed=(not blockers and early_strength)
        rows.append({"asset":sym,"as_of_utc":now.isoformat(),
          "reference_price":coin.get("reference_price"),"signal":sig,
          "proposed_max_cost_usdt":proposal,"execution_scenario":execution,
          "research_gaps":list(dict.fromkeys(research_gaps)),
          "entry_stage":entry_stage,"first_tranche_allowed":first_tranche_allowed,
          "confirmation_role":"ADD_POSITION_ONLY" if first_tranche_allowed else "NONE",
          "blockers":list(dict.fromkeys(blockers)),
          "capital_review_eligible":not blockers,
          "trade_action":"USER_REVIEW_REQUIRED" if not blockers else "NONE"})
    rows.sort(key=lambda x:(not x["capital_review_eligible"],-float(x["signal"].get("score") or 0)))
    report={"schema":"hunter_tactical_capital_review_v1","as_of_utc":now.isoformat(),
      "scan_generation_id":scan.get("generation_id"),
      "policy":"NO_AUTO_TRADE__SHORT_HORIZON_GATE_SEPARATE_FROM_LONG_HORIZON_VALUATION",
      "alt_pool_cap_usdt":MAX_ALT_POOL,
      "capital_state":{"open_cost_usdt":known_open,"pending_reservations_usdt":known_pending,"available_usdt":pool-known_open-known_pending},
      "capital_review_eligible":[x["asset"] for x in rows if x["capital_review_eligible"]],
      "candidates":rows[:40]}
    OUT.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps({"reviewed":len(rows),"eligible":report["capital_review_eligible"],
      "top_blockers":{x["asset"]:x["blockers"] for x in rows[:10]}},ensure_ascii=False))
if __name__=="__main__":main()
