#!/usr/bin/env python3
"""Offline V2 evidence inventory and close-only sensitivity replay.

Never writes canonical portfolio/results, never fabricates executable books. Recorded
marks and completed kline closes are ASSUMPTION_ONLY, not executable evidence.
"""
import argparse
import datetime as dt
import hashlib
import importlib.util
import sys
import json
import math
import pathlib
import subprocess
from dataclasses import asdict, dataclass

PORTFOLIO = 'research/results/hunter-shadow-v2-portfolio.json'
COST_BPS = (0, 5, 10, 25, 50)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def timestamp(value):
    return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(dt.timezone.utc)


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True)


@dataclass(frozen=True)
class CandidateParameters:
    arm_pct: float = 2.0
    runner_pct: float = 8.0
    arm_basis: str = 'net'
    floor_mode: str = 'capture_ratio'
    capture_low: float = .25
    capture_mid: float = .5
    capture_runner: float = .6
    giveback_pct: float = 1.5
    fee_bps: float = 10.0
    legacy_arm_pct: float = 2.0
    legacy_giveback_pct: float = 2.0
    legacy_min_net_pct: float = .35


def inventory(repo, ref='origin/main'):
    """All locally recoverable position IDs, including positions erased by resets."""
    head = git(repo, 'rev-parse', ref).strip()
    latest = json.loads(git(repo, 'show', f'{head}:{PORTFOLIO}'))
    current = {p['shadow_id'] for k in ('open_positions', 'closed_positions') for p in latest.get(k, [])}
    positions, observations, failures = {}, {}, []
    commits = git(repo, 'log', '--format=%H', '--reverse', head, '--', PORTFOLIO).splitlines()
    for commit in commits:
        try:
            state = json.loads(git(repo, 'show', f'{commit}:{PORTFOLIO}'))
        except (ValueError, subprocess.CalledProcessError) as exc:
            failures.append({'commit': commit, 'reason': type(exc).__name__})
            continue
        for lane in ('open_positions', 'closed_positions', 'archived_closed_positions'):
            for p in state.get(lane, []):
                pid = p.get('shadow_id')
                if not pid:
                    failures.append({'commit': commit, 'reason': 'POSITION_ID_MISSING'})
                    continue
                positions[pid] = p
                at, price = p.get('last_marked_at_utc'), p.get('last_price')
                if at and isinstance(price, (int, float)) and math.isfinite(price) and price > 0:
                    observations.setdefault(pid, {})[at] = {'at': at, 'price': price, 'commit': commit,
                        'scope': 'ASSUMPTION_ONLY', 'source': 'PERSISTED_REFERENCE_MARK',
                        'execution_evidence_status': 'UNVERIFIABLE'}
    rows = []
    for pid, p in sorted(positions.items(), key=lambda pair: (pair[1].get('opened_at_utc', ''), pair[0])):
        obs = sorted(observations.get(pid, {}).values(), key=lambda x: timestamp(x['at']))
        rows.append({'position_id': pid, 'asset': p['asset'], 'position': p,
                     'current_portfolio': pid in current, 'observations': obs,
                     'execution_comparison_status': 'UNVERIFIABLE',
                     'missing': ['HISTORICAL_FULL_QUANTITY_BID_DEPTH', 'SOURCE_TIMESTAMP',
                                 'FULL_LIFECYCLE_EXECUTION_EVIDENCE', 'COMPLETE_RUNNER_SIGNALS']})
    # Fixed chronological split, prior to result evaluation. No winner selection.
    split = len(rows) * 2 // 3
    for i, row in enumerate(rows):
        row['split'] = 'IN_SAMPLE' if i < split else 'HOLDOUT'
    return {'frozen_commit': head, 'positions': rows, 'history_commits_read': len(commits),
            'history_read_failures': failures, 'latest_counts': {
                'open': len(latest.get('open_positions', [])), 'closed': len(latest.get('closed_positions', []))},
            'coverage': {'current_positions': len(current), 'recoverable_position_ids': len(rows),
                         'historical_executable_complete': 0},
            'history_boundary': 'LOCAL_GIT_RECOVERABLE_ONLY; erased or unavailable history remains UNVERIFIABLE'}


def close_observations(bars):
    """Use completed close timestamp only; high/low never controls simulated exits."""
    out = []
    for bar in bars:
        at = dt.datetime.fromtimestamp(float(bar[6]) / 1000, dt.timezone.utc).isoformat()
        out.append({'at': at, 'price': float(bar[4]), 'scope': 'ASSUMPTION_ONLY',
                    'source': 'BINANCE_COMPLETED_KLINE_CLOSE', 'execution_evidence_status': 'UNVERIFIABLE'})
    return sorted(out, key=lambda x: timestamp(x['at']))


def net_mark(tranches, price, fee_bps, sell_cost_bps):
    invested = sum(float(t['notional_usdt']) for t in tranches)
    quantity = sum(float(t['notional_usdt']) / (float(t['price']) *
                   (1 + (float(t.get('buy_slippage_bps', 0)) + fee_bps) / 10000)) for t in tranches)
    return quantity * price * (1 - sell_cost_bps / 10000) * (1 - fee_bps / 10000) - invested


def simulate_reference(position, observations, parameters, sell_cost_bps, engine='candidate'):
    """Close-only candidate sensitivity. RUNNER admission unknown => no fake runner.

    This is deliberately not the executable engine. Every simulated SELL and PnL is
    counterfactual and cannot close official positions. ADD cannot itself cause exit.
    """
    state, floor, peak_net, peak_raw = 'UNARMED', 0., 0., 0.
    previous_count, exit_event = 0, None
    candidate_position = {'position_id': position.get('shadow_id'), 'asset': position.get('asset')}
    trace = []
    for obs in sorted(observations, key=lambda x: timestamp(x['at'])):
        if timestamp(obs['at']) < timestamp(position['opened_at_utc']):
            continue
        active = [t for t in position.get('tranches', []) if timestamp(t['at']) <= timestamp(obs['at'])]
        if not active:
            continue
        invested = sum(float(t['notional_usdt']) for t in active)
        entry = sum(float(t['price']) * float(t['notional_usdt']) for t in active) / invested
        raw = (obs['price'] / entry - 1) * 100
        net = net_mark(active, obs['price'], parameters.fee_bps, sell_cost_bps)
        peak_raw, peak_net = max(peak_raw, raw), max(peak_net, net)
        basis = raw if parameters.arm_basis == 'raw' else net / invested * 100
        added = len(active) > previous_count and previous_count > 0
        previous_count = len(active)
        if isinstance(engine, tuple):
            advance, params_class = engine
            values = asdict(parameters)
            values = {k:v for k,v in values.items() if k in params_class.__dataclass_fields__}
            values['floor_mode'] = 'giveback_pct' if parameters.floor_mode == 'percentage_point_giveback' else parameters.floor_mode
            values['allow_assumption_only'] = True
            quantity = sum(float(t['notional_usdt']) / (float(t['price']) * (1 + (float(t.get('buy_slippage_bps', 0)) + parameters.fee_bps) / 10000)) for t in active)
            execution = {'status':'ASSUMPTION_ONLY', 'evidence_scope':'ASSUMPTION_ONLY', 'execution_verified':False,
                'full_quantity_verified':False, 'source_timestamp':obs['at'], 'execution_price':obs['price'],
                'quantity':quantity, 'total_invested_cash':invested, 'executable_net_pnl_usdt':net,
                'executable_net_return_pct':net / invested * 100}
            candidate_position, decision = advance(candidate_position, execution,
                {'observed_price_return_pct':raw, 'fresh':False}, obs['at'], 'OFFLINE_REFERENCE_' + obs['at'], params_class(**values))
            state = candidate_position['protection_state']
            floor = candidate_position['protected_net_pnl_floor_usdt']
            should_exit = decision['action'] == 'SELL_INTENT'
        elif engine == 'legacy':
            should_exit = peak_raw >= parameters.legacy_arm_pct and (raw <= parameters.legacy_min_net_pct + 2 * parameters.fee_bps / 100 or peak_raw - raw >= parameters.legacy_giveback_pct) and net > 0
        else:
            if state == 'UNARMED' and basis >= parameters.arm_pct:
                state = 'PROFIT_PROTECTION_ARMED'
            if state != 'UNARMED':
                # Capture is in USDT; expanding ADD denominator never lowers old floor.
                peak_return = peak_net / invested * 100
                ratio = parameters.capture_mid if peak_return >= 4 else parameters.capture_low
                proposed = peak_net * ratio if parameters.floor_mode == 'capture_ratio' else peak_net - invested * parameters.giveback_pct / 100
                floor = max(floor, proposed, 0.)
            should_exit = state != 'UNARMED' and net <= floor and net > 0 and not added
        trace.append({'at': obs['at'], 'net_pnl_usdt': net, 'invested_usdt': invested,
                      'state': state, 'protected_floor_usdt': floor, 'add_observation': added})
        if should_exit:
            exit_event = {'at': obs['at'], 'reference_price': obs['price'], 'net_pnl_usdt': net,
                          'capital_released_usdt': invested, 'scope': 'COUNTERFACTUAL_REFERENCE_CLOSE_NOT_EXECUTED'}
            break
    final = trace[-1]['net_pnl_usdt'] if trace else None
    capture = exit_event['net_pnl_usdt'] / peak_net if exit_event and peak_net > 0 else None
    drawdown = None
    if trace:
        running_peak = trace[0]['net_pnl_usdt']
        drawdown = 0.
        for row in trace:
            running_peak = max(running_peak, row['net_pnl_usdt'])
            drawdown = max(drawdown, running_peak - row['net_pnl_usdt'])
    horizons = {}
    for hours in (1, 6, 24):
        point, maximum = None, None
        if exit_event:
            target = timestamp(exit_event['at']) + dt.timedelta(hours=hours)
            within = [o for o in observations if timestamp(exit_event['at']) < timestamp(o['at']) <= target]
            endpoint = [o for o in observations if abs((timestamp(o['at']) - target).total_seconds()) <= 60]
            if endpoint:
                chosen = min(endpoint, key=lambda o: abs((timestamp(o['at']) - target).total_seconds()))
                point = (chosen['price'] / exit_event['reference_price'] - 1) * 100
            # Require window coverage, no partial-window max advertised as full horizon.
            if endpoint and within:
                maximum = max((o['price'] / exit_event['reference_price'] - 1) * 100 for o in within)
        horizons[f'{hours}h'] = {'point_in_time_return_pct': point, 'window_max_return_pct': maximum,
                                'status': 'ASSUMPTION_ONLY' if point is not None else 'UNVERIFIABLE'}
    return {'scope': 'ASSUMPTION_ONLY', 'execution_comparison_status': 'UNVERIFIABLE',
            'observation_count': len(trace), 'exit': exit_event, 'terminal_mark_net_pnl_usdt': final,
            'profit_capture_ratio': capture, 'sampled_pnl_drawdown_usdt': drawdown,
            'post_exit': horizons, 'runner_status': 'UNVERIFIABLE_MISSING_CONTEMPORANEOUS_SIGNALS',
            'trace': trace, 'trace_hash': digest(trace), 'full_lifecycle': False,
            'candidate_incidents': candidate_position.get('protection_incidents', []),
            'candidate_engine': candidate_position.get('protection_engine_version', 'REFERENCE_PROXY_NOT_PRODUCTION_ENGINE')}


def build_report(evidence, parameters=CandidateParameters(), minute_bars=None, candidate_engine=None, candidate_module_hash=None):
    rows = []
    definitions = {}
    def compact(result):
        return {'exit':result['exit'], 'terminal_mark_net_pnl_usdt':result['terminal_mark_net_pnl_usdt'],
                'profit_capture_ratio':result['profit_capture_ratio'],
                'sampled_pnl_drawdown_usdt':result['sampled_pnl_drawdown_usdt'],
                'observation_count':result['observation_count'], 'trace_hash':result['trace_hash'],
                'post_exit_returns_pct':{h:[v['point_in_time_return_pct'],v['window_max_return_pct']] for h,v in result['post_exit'].items()},
                'incident_classes':sorted({i['root_cause_class'] for i in result['candidate_incidents']}),
                'incident_count':len(result['candidate_incidents'])}
    for row in evidence['positions']:
        observations = row['observations']
        if minute_bars and row['asset'] in minute_bars:
            observations = close_observations(minute_bars[row['asset']])
        scenarios = []
        for basis in ('net', 'raw'):
            for mode in ('capture_ratio', 'percentage_point_giveback'):
                params = CandidateParameters(**{**asdict(parameters), 'arm_basis': basis, 'floor_mode': mode})
                for cost in COST_BPS:
                    old = simulate_reference(row['position'], observations, params, cost, 'legacy')
                    new = simulate_reference(row['position'], observations, params, cost, candidate_engine or 'candidate')
                    old.pop('trace'); new.pop('trace')
                    scenario_id = f'{basis}/{mode}/{cost}bps'
                    definitions[scenario_id] = {'parameters':asdict(params),'sell_cost_bps':cost}
                    scenarios.append({'scenario_id':scenario_id, 'old':compact(old), 'new':compact(new)})
        rows.append({**{k: v for k, v in row.items() if k not in ('position', 'observations')},
                     'reference_observation_count': len(observations), 'scenarios': scenarios})
    metrics = ['net_usdt_pnl_after_costs', 'portfolio_max_drawdown', 'profit_capture_ratio', 'sell_count', 'capital_released', 'premature_exit_count',
               'missed_profit_exit_count', 'capital_utilization', 'new_buy_count', 'new_buy_quality',
               'reserve_usage', 'gap_through_count', 'data_degraded_count', 'execution_evidence_missing_count']
    return {'schema': 'hunter-v2-lifecycle-offline-replay-v1',
            'frozen_commit': evidence['frozen_commit'], 'parameter_hash': digest(asdict(parameters)),
            'input_hash': digest({'evidence': evidence, 'minute_bars': minute_bars}),
            'authority': 'NONE_SHADOW_ONLY', 'real_trading_enabled': False,
            'canonical_write_allowed': False, 'candidate_enabled': False,
            'candidate_engine_source_hash': candidate_module_hash,
            'candidate_engine_exercised': candidate_engine is not None,
            'coverage': evidence['coverage'], 'latest_counts': evidence['latest_counts'],
            'history_commits_read': evidence['history_commits_read'],
            'history_read_failures': evidence['history_read_failures'], 'positions': rows, 'scenario_definitions':definitions,
            'scenario_results_scope':'ASSUMPTION_ONLY; exits COUNTERFACTUAL_REFERENCE_CLOSE_NOT_EXECUTED; post_exit values [point_in_time,window_max], null=UNVERIFIABLE; no full-lifecycle comparison',
            'portfolio_replay_status': 'UNVERIFIABLE_MISSING_CONTEMPORANEOUS_REVIEW_AND_EXECUTION_EVIDENCE',
            'metrics': {m: {'status': 'UNVERIFIABLE', 'value': None, 'denominator': 0} for m in metrics},
            'limitations': ['Recorded marks may have source timestamp/freshness unknown.',
                'Historical full quantity orderbooks unavailable; no executable comparison proved.',
                'Missing observations can hide excursions; no OHLC intrabar ordering assumption.',
                'Current/open and recoverable erased positions included; unknown erased positions not invented.',
                'Holdout chronological split frozen before evaluation; sparse short history not independent validation.',
                'Close-only paths are partial scenarios, not full lifecycle profitability; old denotes frozen current legacy protection subset, not a reconstruction of every historical policy version or all other exits.',
                'Full-portfolio candidates, identity/supply and competition not reconstructed; no invented new BUY.',
                'No runner admitted without historical fresh signal evidence.'],
            'decision': 'NOT_ENOUGH_EVIDENCE_TO_REPLACE_CURRENT_V2'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', type=pathlib.Path, default=pathlib.Path('.'))
    parser.add_argument('--ref', default='origin/main')
    parser.add_argument('--minute-bars', type=pathlib.Path)
    parser.add_argument('--candidate-module', type=pathlib.Path, help='Frozen offline candidate module to exercise; no live imports')
    parser.add_argument('--output', type=pathlib.Path, required=True)
    args = parser.parse_args()
    out = args.output.resolve()
    canonical = (args.repo / 'research/results').resolve()
    if out.is_relative_to(canonical):
        parser.error('CANONICAL_RESULTS_WRITE_FORBIDDEN')
    evidence = inventory(args.repo, args.ref)
    minute = {x['asset']: x['bars'] for x in json.loads(args.minute_bars.read_text())} if args.minute_bars else None
    candidate, module_hash = None, None
    if args.candidate_module:
        spec = importlib.util.spec_from_file_location('offline_frozen_protection_candidate', args.candidate_module)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        frozen_source = args.candidate_module.read_bytes()
        exec(compile(frozen_source, str(args.candidate_module), 'exec'), module.__dict__)
        candidate = (module.advance, module.ProtectionParams)
        module_hash = hashlib.sha256(frozen_source).hexdigest()
    report = build_report(evidence, minute_bars=minute, candidate_engine=candidate, candidate_module_hash=module_hash)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'decision': report['decision'], 'coverage': report['coverage'], 'output': str(out)}))


if __name__ == '__main__':
    main()
