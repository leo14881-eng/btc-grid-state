#!/usr/bin/env python3
"""Hunter V1 broad-net shadow lane.

V1 samples every EARLY candidate (>=2 independent early signals) directly and deterministically from
hunter-early-signals.json. It deliberately bypasses V2 capital-review gates.
V2 remains the strict executable/capital lane.
"""
import json,pathlib
try:
    from research import hunter_shadow_trader_v2 as engine
except ImportError:
    import hunter_shadow_trader_v2 as engine

def configure_v1():
    root=pathlib.Path("research/results")
    engine.STATE=root/"hunter-shadow-portfolio.json"
    engine.SUMMARY=root/"hunter-shadow-summary.json"
    engine.GUARD=root/"hunter-shadow-v1-overfilter-guard.json"
    engine.CAPITAL_POOL_USDT=engine.LANES["V1"]["capital_pool_usdt"]
    engine.ENTRY_MODE=engine.LANES["V1"]["entry_mode"]
    # V1 admission is defined by EARLY membership itself (>=2 independent signals).
    # Do not re-apply the legacy score>=6 gate; V2 keeps its strict score/evidence gates.
    engine.DISCOVERY_MIN_SCORE=engine.LANES["V1"]["discovery_min_score"]
    engine.STRATEGY_ID=engine.LANES["V1"]["strategy"]
    engine.ID_PREFIX="SHV1"
    engine.EVENT_PREFIX="SHADOW_V1"

def load_early_into_review():
    root=pathlib.Path("research/results")
    early=json.loads((root/"hunter-early-signals.json").read_text())
    if early.get("policy_version")!=engine.VERSION:raise RuntimeError("V1_EARLY_POLICY_MISMATCH")
    universe=json.loads((root/"hunter-cex-universe-run.json").read_text())
    excluded=set((((universe.get("venue_status") or {}).get("binance") or {}).get("excluded_bstocks") or []))
    if not excluded:
        raise RuntimeError("CRYPTO_SCOPE_BSTOCK_CLASSIFICATION_MISSING")
    review_path=root/"hunter-tactical-capital-review.json"
    review=json.loads(review_path.read_text())
    by={c.get("asset"):c for c in review.get("candidates") or [] if c.get("asset")}
    candidates=[]
    held={p.get("asset") for p in (engine.load(root/"hunter-shadow-portfolio.json").get("open_positions") or [])}
    signals={x.get("base"):x for x in early.get("early") or []}
    signals.update({x.get("base"):x for x in early.get("all_signals") or [] if x.get("base") in held})
    for s in signals.values():
        a=s.get("base")
        if not a or a in excluded: continue
        c=dict(by.get(a) or {})
        c["asset"]=a
        c["signal"]=s
        c["signal_evidence"]=engine.stamp(a,early.get("scan_generation_id"),early.get("as_of_utc"))
        # V1 samples research signals; missing V2 evidence is retained as metadata,
        # never converted into a V1 admission blocker.
        c.setdefault("blockers",[])
        candidates.append(c)
    review["candidates"]=candidates
    review["v1_source"]="hunter-early-signals.json"
    review["v1_early_count"]=len(candidates)
    review["v1_excluded_bstocks"]=sorted(excluded)
    return review_path,review

def main():
    configure_v1()
    path,review=load_early_into_review()
    original=engine.load
    def v1_load(p,default=None):
        if p==engine.REVIEW:return review
        return original(p,default)
    engine.load=v1_load
    engine.main()

if __name__=="__main__":
    main()
