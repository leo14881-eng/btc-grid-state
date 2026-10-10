#!/usr/bin/env python3
"""Infrastructure-only, fail-closed publication of a monitor generation."""
import argparse
import json
import math
import pathlib
import subprocess
import sys

# The installed host launcher invokes this file directly, not with -m.
if not __package__:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

RESULTS = 'research/results/'
NAMES = (
    'hunter-shadow-portfolio.json', 'hunter-shadow-v2-portfolio.json',
    'hunter-shadow-summary.json', 'hunter-shadow-v2-summary.json',
    'hunter-position-monitor.json', 'hunter-leading-risk.json',
    'hunter-scheduler-health.json',
)
PATHS = tuple(RESULTS + name for name in NAMES)


def git(*args, check=True):
    return subprocess.run(['git', *args], check=check, capture_output=True,
                          text=True)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def snapshot(ref=None):
    return {name: (git('show', f'{ref}:{RESULTS}{name}').stdout if ref
                   else pathlib.Path(RESULTS + name).read_text()) for name in NAMES}


def validate(raw, generation):
    docs = {name: json.loads(raw[name]) for name in NAMES}
    h = docs['hunter-scheduler-health.json']
    require(h.get('schema') == 'hunter_scheduler_health_v1', 'INVALID_HEALTH_SCHEMA')
    require(h.get('current_generation_id') == generation and
            h.get('last_successful_monitor_generation_id') == generation,
            'GENERATION_MISMATCH')
    require(h.get('shadow_only') is True and type(h.get('real_order_count')) is int
            and h['real_order_count'] == 0, 'REAL_ORDER_INVARIANT')
    lr = docs['hunter-leading-risk.json']
    require(lr.get('capital_authority') == 'NONE_SHADOW_ONLY' and
            lr.get('shadow_only') is True and lr.get('real_position_mutation') is False,
            'LEADING_RISK_AUTHORITY')
    require((lr.get('current') or {}).get('level') in
            ('NORMAL', 'WATCH', 'PRE_CRASH_1', 'PRE_CRASH_2', 'PRE_CRASH_3'),
            'INVALID_LEADING_RISK_LEVEL')
    monitor = docs['hunter-position-monitor.json']
    rows = monitor.get('results')
    require(isinstance(rows, list), 'MONITOR_LANES_MISSING')
    lanes = {row.get('lane'): row for row in rows}
    require(len(lanes) == len(rows), 'DUPLICATE_MONITOR_LANE')
    counts = []
    for prefix, lane in [('hunter-shadow', 'SHADOW_V1'),
                         ('hunter-shadow-v2', 'SHADOW_V2')]:
        p = docs[prefix + '-portfolio.json']
        s = docs[prefix + '-summary.json']
        require(p.get('schema') == 'hunter_shadow_v2_portfolio_v2', 'INVALID_PORTFOLIO_SCHEMA')
        require(p.get('mode') == s.get('mode') == 'SIMULATION_ONLY_NO_REAL_ORDERS',
                'PORTFOLIO_MODE_MISMATCH')
        require(s.get('capital_authority') == 'NONE_SHADOW_ONLY', 'SUMMARY_AUTHORITY')
        opened, closed = p.get('open_positions'), p.get('closed_positions')
        require(isinstance(opened, list) and isinstance(closed, list), 'INVALID_POSITION_LIST')
        assets = [row.get('asset') for row in opened]
        require(all(isinstance(a, str) and a for a in assets) and
                len(assets) == len(set(assets)), 'DUPLICATE_OR_MISSING_OPEN_ASSET')
        require(type(s.get('open_positions')) is int and
                s['open_positions'] == len(opened), 'SUMMARY_OPEN_MISMATCH')
        require(type(s.get('closed_positions')) is int and
                s['closed_positions'] == len(closed), 'SUMMARY_CLOSED_MISMATCH')
        require(bool(p.get('updated_at_utc')) and
                s.get('as_of_utc') == p['updated_at_utc'], 'SUMMARY_REVISION_MISMATCH')
        require(lane in lanes and type(lanes[lane].get('open')) is int and
                lanes[lane]['open'] == len(opened), 'MONITOR_OPEN_MISMATCH')
        if lane == 'SHADOW_V2':
            policy = s.get('policy') or {}
            require(policy.get('capital_pool_usdt') == 20000 and
                    (policy.get('capital_management') or {}).get('initial_capital_usdt') == 20000,
                    'V2_INITIAL_CAPITAL_MISMATCH')
            # Validate actual open exposure, not just the configured principal.
            # Realized profit must never enlarge the user's fixed 20K ceiling.
            exposure = 0.0
            for position in opened:
                tranches = position.get('tranches')
                require(isinstance(tranches, list) and bool(tranches),
                        'V2_EXPOSURE_UNVERIFIABLE')
                for tranche in tranches:
                    amount = tranche.get('notional_usdt')
                    require(type(amount) in (int, float) and math.isfinite(amount)
                            and amount > 0, 'V2_INVALID_TRANCHE_NOTIONAL')
                    exposure += amount
            require(exposure <= 20000.000001, 'V2_EXPOSURE_HARD_CAP_EXCEEDED')
            # New receipts are optional only for legacy publishers. Once present,
            # each position and the public projection must belong to this cycle.
            if 'position_monitor_states' in s:
                from research.hunter_lifecycle_v2 import projection
                current = (monitor.get('evidence_refresh') or {}).get('generation_id')
                require(bool(current) and current == p.get('last_cycle_generation_id'),
                        'V2_MONITOR_AUDIT_GENERATION_MISMATCH')
                for position in opened:
                    decision = position.get('last_monitor_decision') or {}
                    require(decision.get('schema') == 'hunter_v2_monitor_decision_v1'
                            and decision.get('generation_id') == current
                            and decision.get('capital_authority') == 'NONE_SHADOW_ONLY'
                            and decision.get('real_trading_enabled') is False
                            and bool(decision.get('reasons')) and bool(decision.get('checked_at')),
                            'V2_MONITOR_DECISION_MISSING_OR_MIXED')
                require(s['position_monitor_states'] == [projection(position) for position in opened],
                        'V2_MONITOR_PROJECTION_MISMATCH')
        counts.append(len(opened))
    return counts


def protected(path):
    # Transitive Hunter inputs are protected too, not just directly edited modules.
    return (path in PATHS or path == RESULTS + 'hunter-shadow-rules.json' or
            path.startswith('research/hunter_') or
            path.startswith('tests/test_hunter_') or
            path.startswith('scripts/hunter_monitor_') or
            path.startswith('deploy/systemd/hunter-position-monitor.') or
            path == '.github/workflows/hunter-position-monitor.yml' or
            path == '.github/hunter-runtime.json')


def cas(base):
    git('fetch', 'origin', 'main')
    changed = git('diff', '--name-only', base, 'origin/main').stdout.splitlines()
    conflicts = [path for path in changed if protected(path)]
    require(not conflicts, 'SHADOW_STATE_CAS_REJECTED_STALE_WRITER ' + ' '.join(conflicts))


def readback(expected, generation, commit=None):
    git('fetch', 'origin', 'main')
    if commit:
        require(git('merge-base', '--is-ancestor', commit, 'origin/main',
                    check=False).returncode == 0, 'PUBLISHED_COMMIT_NOT_AUTHORITATIVE')
    remote = snapshot('origin/main')
    counts = validate(remote, generation)
    require(remote == expected, 'AUTHORITATIVE_SNAPSHOT_MISMATCH')
    print('SERVER_POST_PUSH_READBACK_OK', generation, *counts)
    return commit or git('rev-parse', 'origin/main').stdout.strip()


def persist(base, generation):
    expected = snapshot()
    validate(expected, generation)
    changes = git('status', '--porcelain', '--untracked-files=no').stdout.splitlines()
    require(all(line[3:] in PATHS for line in changes), 'UNEXPECTED_TRACKED_MUTATION')
    cas(base)
    git('add', '-f', '--', *PATHS)
    if git('diff', '--cached', '--quiet', check=False).returncode == 0:
        verified_commit = readback(expected, generation)
        from scripts.hunter_monitor_runtime import publish_verified
        publish_verified(expected, generation, verified_commit)
        return
    git('commit', '-m', 'state: five-minute Hunter position monitor')
    for attempt in range(1, 6):
        cas(base)
        git('rebase', 'origin/main')
        pushed = git('push', 'origin', 'HEAD:main', check=False)
        if pushed.returncode == 0:
            commit = git('rev-parse', 'HEAD').stdout.strip()
            verified_commit = readback(expected, generation, commit)
            from scripts.hunter_monitor_runtime import publish_verified
            publish_verified(expected, generation, verified_commit)
            print('SHADOW_STATE_PUSH_VERIFIED', f'attempt={attempt}')
            return
        print('SHADOW_STATE_PUSH_RACE', f'attempt={attempt}', flush=True)
    raise RuntimeError('PUSH_RACE_RETRY_EXHAUSTED')


def main():
    p = argparse.ArgumentParser()
    p.add_argument('command', choices=['validate', 'persist'])
    p.add_argument('--generation', required=True)
    p.add_argument('--base')
    a = p.parse_args()
    if a.command == 'validate':
        validate(snapshot(), a.generation)
        print('SERVER_PRE_PERSIST_VALIDATION_OK')
    else:
        require(bool(a.base), 'BASE_SHA_REQUIRED')
        persist(a.base, a.generation)


if __name__ == '__main__':
    main()
