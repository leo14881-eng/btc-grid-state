#!/usr/bin/env python3
"""Hunter V1 broad-net shadow lane.

V1 and V2 intentionally execute the exact same decision engine. V1 changes only
portfolio constraints: unlimited simulated capital and therefore no cross-asset
capital competition. It never places exchange orders.
"""
import pathlib
import hunter_shadow_trader_v2 as engine

ROOT=pathlib.Path("research/results")
engine.STATE=ROOT/"hunter-shadow-portfolio.json"
engine.SUMMARY=ROOT/"hunter-shadow-summary.json"
engine.GUARD=ROOT/"hunter-shadow-v1-overfilter-guard.json"
engine.CAPITAL_POOL_USDT=None
engine.STRATEGY_ID="SHARED_DECISION_ENGINE_V1_BROAD_NET"
engine.ID_PREFIX="SHV1"
engine.EVENT_PREFIX="SHADOW_V1"

if __name__=="__main__":
    engine.main()
