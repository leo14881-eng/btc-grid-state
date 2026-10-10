#!/usr/bin/env python3
"""Offline adapter replay of published V2 evidence. Prints JSON; never persists state."""
import argparse
import copy
import datetime as dt
import hashlib
import json
import pathlib
import subprocess
import sys
from unittest.mock import patch

sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
from research import hunter_shadow_trader_v2 as engine
from research.hunter_lifecycle_v2 import projection


def git(*args):return subprocess.check_output(['git',*args],encoding='utf-8')
def document(ref,name):return json.loads(git('show',ref+':research/results/'+name))
def digest(obj):return hashlib.sha256(json.dumps(obj,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def run(ref):
    ref=git('rev-parse',ref).strip()
    path='research/results/hunter-shadow-v2-portfolio.json'
    commits=git('log','--first-parent','-2','--format=%H',ref,'--',path).splitlines()
    if len(commits)!=2:raise RuntimeError('PREVIOUS_PUBLISHED_PORTFOLIO_REQUIRED')
    previous=document(commits[1],'hunter-shadow-v2-portfolio.json')
    actual=document(ref,'hunter-shadow-v2-portfolio.json')
    monitor=document(ref,'hunter-position-monitor.json')
    original=copy.deepcopy(previous)
    now=dt.datetime.fromisoformat(actual['updated_at_utc'])
    generation=actual['last_cycle_generation_id']
    if generation!=monitor['evidence_refresh']['generation_id']:raise RuntimeError('MIXED_PUBLISHED_MONITOR_GENERATION')
    actual_by={p['shadow_id']:p for p in actual['open_positions']}
    if set(actual_by)!={p['shadow_id'] for p in previous['open_positions']}:
        raise RuntimeError('CURRENT_ALL_OPEN_REPLAY_REQUIRES_UNCHANGED_POSITION_SET')
    review=document(ref,'hunter-tactical-capital-review.json');review['candidates']=[]
    liquidity={'snapshots':{}};market={}
    mapping={'score':'score','independent_signal_count':'independent','btc_relative_1h_pct':'btc_rel_1h',
             'btc_relative_4h_pct':'btc_rel_4h','relative_acceleration_pct':'rel_accel',
             'return_1h_pct':'return_1h','return_4h_pct':'return_4h'}
    for sid,p in actual_by.items():
        records=[d for d in actual['decisions'] if d.get('shadow_id')==sid and d.get('at')==p['last_marked_at_utc']]
        ev=next((d['evidence'] for d in records if (d.get('evidence') or {}).get('signal_evidence')),None)
        if ev is None:raise RuntimeError('ACTUAL_DECISION_EVIDENCE_MISSING:'+p['asset'])
        c=dict(asset=p['asset'],signal={k:ev[v] for k,v in mapping.items() if v in ev},
               signal_evidence=ev['signal_evidence'],blockers=ev.get('blockers',[]),
               execution_scenario={k:ev.get(k) for k in ('buy_slippage_bps','estimated_rr')})
        refresh=monitor['evidence_refresh']
        c['v2_lifecycle_evidence']=(refresh.get('v2_lifecycle_observations',{}).get(p['asset'],{}).get('lifecycle_evidence'))
        review['candidates'].append(c)
        raw=(refresh.get('observed_exit_raw_books') or {}).get(p['asset'])
        if not raw:raise RuntimeError('ACTUAL_EXIT_BOOK_MISSING:'+p['asset'])
        liquidity['snapshots'][p['asset']]={**{k:ev.get(k) for k in ('spread_bps','bid_depth_2pct_usdt','ask_depth_2pct_usdt')},
             'as_of_utc':ev.get('book_observed_at_utc'),'raw_book_evidence':raw}
        market[p['asset']]={'reference_price':p['last_price']}
    scan=dict(generation_id=generation,as_of_utc=monitor['as_of_utc'],coins=market)
    supply=document(ref,'hunter-tactical-supply-risk.json')
    previous['systemic_risk']=copy.deepcopy(actual.get('systemic_risk'))
    with patch.object(engine,'ENTRY_MODE','EXECUTABLE'),patch.object(engine,'EVENT_PREFIX','SHADOW_V2'),patch('urllib.request.urlopen',side_effect=AssertionError('OFFLINE_REPLAY_NETWORK_FORBIDDEN')):
        engine.manage_existing_positions(previous,scan,review,liquidity,supply,now,capital_proposals=[])
        after=copy.deepcopy(previous)
        engine.manage_existing_positions(previous,scan,review,liquidity,supply,now,capital_proposals=[])
    assert previous==after,'DUPLICATE_GENERATION_MUTATION'
    assert previous['events']==original['events'],'HISTORICAL_EVENTS_CHANGED'
    assert previous['closed_positions']==original['closed_positions'],'HISTORICAL_CLOSED_LEDGER_CHANGED'
    assert [(p['shadow_id'],p['tranches']) for p in previous['open_positions']]==[(p['shadow_id'],p['tranches']) for p in original['open_positions']]
    return dict(schema='hunter_v2_all_open_offline_replay_v1',main_sha=ref,previous_portfolio_commit=commits[1],
        generation_id=generation,evidence_time=actual['updated_at_utc'],scope='ACTUAL_RECORDED_SCALARS_AND_BOOKS_ADAPTED_NOT_FULL_ORIGINAL_INPUT_ENVELOPE',
        raw_micro_receipts_available=all(bool(c.get('v2_lifecycle_evidence')) for c in review['candidates']),
        count=len(actual_by),new_sell_count=0,duplicate_replay_unchanged=True,historical_events_unchanged=True,
        historical_closed_ledger_unchanged=True,source_portfolio_sha256=digest(actual),
        authoritative_states=[dict(asset=p['asset'],health_state=p.get('health_state'),degraded_cycles=p.get('degraded_cycles'),
            recovery_state=p.get('recovery_state'),protection_state=p.get('protection_lifecycle',{}).get('state')) for p in actual['open_positions']],
        proposed_states=[projection(p) for p in previous['open_positions']],
        replay_issues={p['asset']:p['last_monitor_decision']['thesis_review']['checks'] for p in previous['open_positions']},
        safety=dict(capital_authority='NONE_SHADOW_ONLY',real_trading_enabled=False,real_order_count=0,
                    capital_pool_usdt=engine.CAPITAL_POOL_USDT,**engine.reserve_snapshot(previous)))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--ref',required=True);args=parser.parse_args()
    print(json.dumps(run(args.ref),ensure_ascii=False,indent=2))
