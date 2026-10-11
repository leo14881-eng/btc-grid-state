#!/usr/bin/env python3
"""Infrastructure-only, fail-closed publication of a monitor generation."""
import argparse
import json
import math
import os
import pathlib
import subprocess
import sys
import time

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
# Monitor's complete result read/write set, separate from Research's inputs.
# OUT supplies the evidence cursor, leading-risk retains history, and scheduler
# health supplies generation admission. Summary guard status is also read.
MONITOR_RESULT_PATHS = frozenset(PATHS) | frozenset(RESULTS + name for name in (
    'hunter-shadow-rules.json', 'hunter-cex-universe-run.json',
    'hunter-tactical-capital-review.json', 'hunter-liquidity-probe.json',
    'hunter-tactical-supply-risk.json', 'hunter-shadow-v1-overfilter-guard.json',
    'hunter-shadow-v2-overfilter-guard.json',
))


def timed(stage, fn, *args, **kwargs):
    start = time.monotonic()
    try:
        return fn(*args, **kwargs)
    finally:
        print('HUNTER_MONITOR_PERSIST_TIMING ' + json.dumps(dict(
            stage=stage, seconds=round(time.monotonic()-start, 6))), flush=True)


def git(*args, check=True):
    # Only the Git operation name is logged, never URLs or command arguments.
    return timed('git_' + args[0], subprocess.run, ['git', *args], check=check,
                 capture_output=True, text=True)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def snapshot(ref=None):
    return {name: (git('show', f'{ref}:{RESULTS}{name}').stdout if ref
                   else pathlib.Path(RESULTS + name).read_text()) for name in NAMES}


def validate(raw, generation):
    docs = timed('parse_snapshot', lambda: {name: json.loads(raw[name]) for name in NAMES})
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
        counts.append(len(opened))
    return counts


def tested_input(path):
    # Inputs to prepared code/tests must not change before locked admission.
    return (path == RESULTS + 'hunter-shadow-rules.json' or
            path in ('research/__init__.py', 'scripts/__init__.py', 'tests/__init__.py') or
            path.startswith('research/hunter_') or
            path.startswith('tests/test_hunter_') or
            path.startswith('scripts/hunter_') or
            path.startswith('deploy/systemd/hunter-position-monitor.') or
            path == '.github/workflows/hunter-position-monitor.yml' or
            path == '.github/hunter-runtime.json')


def protected(path):
    # Reject changes to anything this transaction reads or publishes. Independent
    # diagnostics and Research-only outputs can rebase without stale decisions.
    # Imported modules' unused main() entrypoints do not expand Monitor's read set.
    return tested_input(path) or path in MONITOR_RESULT_PATHS


def rebind_after_prepare(base, isolated=False):
    """Under the host lock, discard the prepared snapshot and read latest state.

    Tests ran outside the lock. If their protected inputs changed, fail closed
    instead of running untested code or committing from the prepared snapshot.
    Only this disposable checkout may be reset. No production state is written.
    """
    require(isolated and os.environ.get('HUNTER_MONITOR_ISOLATED_CHECKOUT') == '1',
            'DISPOSABLE_MONITOR_CHECKOUT_REQUIRED')
    require(git('rev-parse', 'HEAD').stdout.strip() == base, 'MONITOR_PREPARE_BASE_MISMATCH')
    require(not git('status', '--porcelain', '--untracked-files=no').stdout.strip(),
            'UNEXPECTED_MONITOR_PREPARE_MUTATION')
    git('fetch', 'origin', 'main')
    latest = git('rev-parse', 'origin/main').stdout.strip()
    changes = git('diff', '--name-only', base, latest).stdout.splitlines()
    conflicts = [p for p in changes if tested_input(p)]
    require(not conflicts, 'MONITOR_PREPARATION_INVALIDATED ' + ' '.join(conflicts))
    git('reset', '--hard', latest)
    print('HUNTER_MONITOR_REBOUND', latest, flush=True)
    return latest


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
    expected = timed('read_worktree_snapshot', snapshot)
    validate(expected, generation)
    changes = git('status', '--porcelain', '--untracked-files=no').stdout.splitlines()
    require(all(line[3:] in PATHS for line in changes), 'UNEXPECTED_TRACKED_MUTATION')
    cas(base)
    git('add', '-f', '--', *PATHS)
    if git('diff', '--cached', '--quiet', check=False).returncode == 0:
        verified_commit = timed('authoritative_readback', readback, expected, generation)
        from scripts.hunter_monitor_runtime import publish_verified
        timed('primary_proof_publication', publish_verified, expected, generation, verified_commit)
        return
    git('commit', '-m', 'state: five-minute Hunter position monitor')
    for attempt in range(1, 6):
        cas(base)
        git('rebase', 'origin/main')
        pushed = git('push', 'origin', 'HEAD:main', check=False)
        if pushed.returncode == 0:
            commit = git('rev-parse', 'HEAD').stdout.strip()
            verified_commit = timed('authoritative_readback', readback, expected, generation, commit)
            from scripts.hunter_monitor_runtime import publish_verified
            timed('primary_proof_publication', publish_verified, expected, generation, verified_commit)
            print('SHADOW_STATE_PUSH_VERIFIED', f'attempt={attempt}')
            return
        print('SHADOW_STATE_PUSH_RACE', f'attempt={attempt}', flush=True)
    raise RuntimeError('PUSH_RACE_RETRY_EXHAUSTED')


def main():
    p = argparse.ArgumentParser()
    p.add_argument('command', choices=['validate', 'persist', 'rebind'])
    p.add_argument('--generation')
    p.add_argument('--base')
    p.add_argument('--isolated-checkout', action='store_true')
    a = p.parse_args()
    if a.command == 'rebind':
        require(bool(a.base), 'BASE_SHA_REQUIRED')
        rebind_after_prepare(a.base, a.isolated_checkout)
        return
    require(bool(a.generation), 'GENERATION_REQUIRED')
    if a.command == 'validate':
        validate(snapshot(), a.generation)
        print('SERVER_PRE_PERSIST_VALIDATION_OK')
    else:
        require(bool(a.base), 'BASE_SHA_REQUIRED')
        persist(a.base, a.generation)


if __name__ == '__main__':
    main()

