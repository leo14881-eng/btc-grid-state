#!/usr/bin/env python3
"""Hunter V1 broad-net shadow lane.

V1 and V2 execute the same decision engine. V1 changes only portfolio constraints:
unlimited simulated capital and no cross-asset capital competition.
"""
import pathlib
try:
    from research import hunter_shadow_trader_v2 as engine
except ImportError:
    import hunter_shadow_trader_v2 as engine

def configure_v1():
    root=pathlib.Path("research/results")
    engine.STATE=root/"hunter-shadow-portfolio.json"
    engine.SUMMARY=root/"hunter-shadow-summary.json"
    engine.GUARD=root/"hunter-shadow-v1-overfilter-guard.json"
    engine.CAPITAL_POOL_USDT=None
    engine.STRATEGY_ID="SHARED_DECISION_ENGINE_V1_BROAD_NET"
    engine.ID_PREFIX="SHV1"
    engine.EVENT_PREFIX="SHADOW_V1"

def main():
    configure_v1()
    engine.main()

if __name__=="__main__":
    main()
