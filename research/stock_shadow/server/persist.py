"""Atomic stock-only publication with authoritative Git fencing and readback.

Never reset/rebase a working checkout, patch a partial ledger onto newer state, or
claim success before the exact published snapshot has been read from origin/main.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from research.stock_shadow.server.runtime import (CONFIG, check_fence, from_environment,
                                                 git, require)
from research.stock_shadow.state_safety import validate_inputs
from research.stock_shadow.stock_shadow_v1 import validate_ledger, net_pnl

ROOT = 'research/results/stock-shadow/'
PORTFOLIO = ROOT + 'portfolio-v1.json'
TRADES = ROOT + 'trades-v1.json'
COMMON = {'portfolio-v1.json', 'trades-v1.json', 'summary-v1.json',
          'review-v1.json', 'calendar-session-cache-v1.json'}
OUTPUTS = {
    'main': COMMON | {'market-daily-cache-v1.json', 'fundamentals-observer-v1.json',
                      'main-run-health-v1.json'},
    'monitor': COMMON | {'position-monitor-v1.json', 'monitor-run-health-v1.json'},
    'replay': {'replay-v1.json'},
}


class PublicationRejected(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


class PublicationUnverified(RuntimeError):
    """A candidate may be remote; never rerun or restore state on this signal."""
    def __init__(self, commit, generation, cause):
        self.commit, self.generation = commit, generation
        super().__init__(f'STOCK_PUBLICATION_UNVERIFIED commit={commit} generation={generation} '
                         f'cause={type(cause).__name__}')


def verify_candidate(commit, outputs, a):
    try:
        readback(commit, outputs, a)
    except Exception as exc:
        raise PublicationUnverified(commit, a['generation'], exc) from exc


def json_bytes(value):
    return (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + '\n').encode()


def protected(path):
    return (path == CONFIG or path.startswith(ROOT) or
            path.startswith('research/stock_shadow/') or
            path.startswith('tests/test_stock') or
            path.startswith('.github/workflows/stock-shadow') or
            path.startswith('deploy/stock-shadow/'))


def changed_paths():
    tracked = git('diff', '--name-only', '-z', 'HEAD').stdout.split('\0')
    untracked = git('ls-files', '--others', '--exclude-standard', '-z',
                    '--exclude=**/__pycache__/**', '--exclude=.pytest_cache/**').stdout.split('\0')
    return sorted(set(tracked + untracked) - {''})


def snapshot(mode, a):
    require(mode in OUTPUTS or mode in ('health', 'preflight'), 'INVALID_PERSISTENCE_MODE')
    require(mode == a['job'] or mode in ('health', 'preflight') and a['job'] in ('main', 'monitor'),
            'MODE_JOB_MISMATCH')
    preflight = a['job'] + '-preflight-health-v1.json'
    names = (OUTPUTS[a['job']] if mode not in ('health', 'preflight') else
             {preflight if mode == 'preflight' else a['job'] + '-run-health-v1.json'})
    paths = changed_paths()
    # A failed process can leave partial stock outputs. They may be inspected but
    # are never published by health mode. Out-of-scope mutations still reject.
    allowed = {ROOT + name for name in OUTPUTS[a['job']]}
    if mode == 'preflight':
        allowed = {ROOT + preflight}  # No test/engine artifacts can ride along.
    require(all(path in allowed for path in paths),
            'UNEXPECTED_MUTATION')
    chosen = [path for path in paths if path[len(ROOT):] in names]
    result = {}
    for path in chosen:
        file = Path(path)
        require(file.is_file() and not file.is_symlink(), 'DELETED_OR_SYMLINK_OUTPUT')
        raw = file.read_bytes()
        json.loads(raw, parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
        result[path] = raw
    return result


def read_json(path, ref=None):
    return json.loads(git('show', f'{ref}:{path}').stdout if ref else Path(path).read_text())


def reporting_audit(state):
    """Expose stale derived ADD reporting without rewriting strategy/ledger data."""
    stored = recomputed = 0.0
    mismatches = []
    unpriced = []
    unavailable = []
    for symbol, position in state['positions'].items():
        if position.get('quote_status') != 'VALID':
            unavailable.append(symbol)
        price = position.get('last_price')
        reported = position.get('net_pnl_usdt')
        if not isinstance(price, (int, float)) or isinstance(price, bool) or price <= 0:
            unpriced.append(symbol)
            continue
        actual = round(net_pnl(position, price), 6)
        recomputed += actual
        if isinstance(reported, (int, float)) and not isinstance(reported, bool):
            stored += reported
            if abs(reported - actual) > 0.000002:
                mismatches.append(symbol)
        else:
            mismatches.append(symbol)
    return {'status': 'MATCH' if not mismatches and not unpriced else 'REPORTING_MISMATCH',
            'mismatched_symbols': mismatches, 'unpriced_symbols': unpriced,
            'valuation_coverage_status': 'PARTIAL' if unavailable or unpriced else 'COMPLETE',
            'unvalued_symbols': sorted(set(unavailable + unpriced)),
            'pnl_basis': 'LAST_KNOWN_PRICES_ARITHMETIC_ONLY',
            'stored_open_net_pnl': round(stored, 6),
            'recomputed_open_net_pnl': round(recomputed, 6),
            'strategy_effect': False, 'ledger_rewritten': False}


def validate_outputs(outputs, mode, a):
    if mode in ('health', 'preflight'):
        health_path = ROOT + a['job'] + ('-preflight-health-v1.json' if mode == 'preflight' else '-run-health-v1.json')
        require(health_path in outputs, 'MISSING_FAILURE_HEALTH')
        h = json.loads(outputs[health_path])
        require(h.get('status') == 'FAILED', 'NOT_FAILURE_HEALTH')
        if mode == 'preflight':
            require(h.get('schema') == 'stock_server_preflight_health_v1'
                    and h.get('stage') == 'PREFLIGHT' and h.get('engine_started') is False
                    and h.get('job') == a['job'] and h.get('owner') == a['owner']
                    and h.get('epoch') == a['epoch'] and h.get('generation') == a['generation'],
                    'INVALID_PREFLIGHT_HEALTH')
    elif a['job'] in ('main', 'monitor'):
        h = read_json(ROOT + a['job'] + '-run-health-v1.json')
        require(h.get('status') == 'SUCCESS', 'RUN_NOT_SUCCESSFUL')
    else:
        h = None
    if h is not None:
        require(h.get('source_commit') == a['source_commit'] and h.get('run_id') == a['run_id'],
                'HEALTH_RUN_BINDING_MISMATCH')
        require(h.get('simulation_only') is True and h.get('real_orders') is False,
                'HEALTH_SHADOW_INVARIANT')
    if mode in ('health', 'preflight'):
        return None
    if a['job'] == 'replay':
        replay = read_json(ROOT + 'replay-v1.json')
        require(isinstance(replay, dict), 'REPLAY_DATA_NOT_ACCEPTED')
        health = replay.get('data_health') or {}
        require(isinstance(health, dict), 'REPLAY_DATA_NOT_ACCEPTED')
        required = health.get('required_symbols') or []
        counts = health.get('regular_session_bar_counts') or {}
        rows = replay.get('results')
        require(replay.get('mode') == 'REPLAY_OBSERVATION_ONLY' and
                replay.get('strategy_effect') is False, 'REPLAY_OBSERVATION_INVARIANT')
        require(replay.get('acceptance_status') == 'ACCEPTED' and
                replay.get('errors') == [] and health.get('status') == 'COMPLETE' and
                health.get('session_complete') is True and health.get('missing_symbols') == [] and
                isinstance(required, list) and all(isinstance(symbol, str) and symbol for symbol in required) and
                isinstance(counts, dict) and {'SPY', 'QQQ'} <= set(required) and
                len(required) == len(set(required)) and
                all(type(counts.get(symbol)) is int and counts[symbol] > 0 for symbol in required),
                'REPLAY_DATA_NOT_ACCEPTED')
        require(isinstance(rows, list) and type(replay.get('big_movers_total')) is int and
                replay.get('big_movers_total') == len(rows) and
                all(isinstance(row, dict) and row.get('symbol') in required and
                    row.get('high_at') and row.get('lookahead_check') == 'PASS' for row in rows) and
                len({row['symbol'] for row in rows}) == len(rows) and
                set(required) == {'SPY', 'QQQ'} | {row['symbol'] for row in rows} and
                health.get('cohort_status') == ('QUALIFYING_MOVERS' if rows else 'NO_QUALIFYING_MOVERS') and
                replay.get('future_leakage_detected') is False and
                replay.get('future_mutation_invariance') is True and
                replay.get('lookahead_violations') == [], 'REPLAY_LOOKAHEAD_NOT_ACCEPTED')
    state, events = read_json(PORTFOLIO), read_json(TRADES)
    validate_inputs(state, events)
    validate_ledger(state, events)
    require(state.get('simulation_only') is True, 'PORTFOLIO_NOT_SHADOW_ONLY')
    before, prior_events = read_json(PORTFOLIO, a['source_commit']), read_json(TRADES, a['source_commit'])
    require(events[:len(prior_events)] == prior_events, 'HISTORICAL_EVENTS_CHANGED')
    require(state['closed'][:len(before['closed'])] == before['closed'], 'HISTORICAL_CLOSED_CHANGED')
    new_closed = state['closed'][len(before['closed']):]
    for symbol, previous in before['positions'].items():
        current = state['positions'].get(symbol)
        if current is None:
            matches = [p for p in new_closed if p.get('symbol') == symbol and
                       p.get('opened_at') == previous.get('opened_at')]
            require(len(matches) == 1, 'POSITION_DISAPPEARED_WITHOUT_CLOSE')
            current = matches[0]
        require(current.get('opened_at') == previous.get('opened_at') and
                current['tranches'][:len(previous['tranches'])] == previous['tranches'],
                'HISTORICAL_POSITION_CHANGED')
    if PORTFOLIO in outputs or TRADES in outputs:
        require(state.get('source_commit') == a['source_commit'] and state.get('run_id') == a['run_id'],
                'PORTFOLIO_RUN_BINDING_MISMATCH')
        # Always capture the pair together, including the unchanged member.
        outputs[PORTFOLIO] = Path(PORTFOLIO).read_bytes()
        outputs[TRADES] = Path(TRADES).read_bytes()
    return reporting_audit(state)


def cas(a):
    git('fetch', '--quiet', 'origin', 'main')
    target = git('rev-parse', 'origin/main').stdout.strip()
    try:
        check_fence(a, target)
    except (RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        raise PublicationRejected(44, 'STOCK_WRITER_FENCED') from exc
    require(git('merge-base', '--is-ancestor', a['source_commit'], target,
                check=False).returncode == 0, 'SOURCE_NO_LONGER_ANCESTOR')
    changes = git('diff', '--name-only', a['source_commit'], target).stdout.splitlines()
    conflicts = [path for path in changes if protected(path)]
    if conflicts:
        code = 43 if any(path.startswith(ROOT) for path in conflicts) else 42
        raise PublicationRejected(code, 'STOCK_INPUT_CAS_REJECTED ' + ' '.join(conflicts))
    return target


def candidate_commit(parent, outputs, a, mode):
    # An independent Git index assembles one atomic tree on the exact remote
    # parent without resetting source files or bringing unrelated local changes.
    env = dict(os.environ)
    env.update(GIT_AUTHOR_NAME='stock-shadow-bot', GIT_AUTHOR_EMAIL='stock-shadow-bot@users.noreply.github.com',
               GIT_COMMITTER_NAME='stock-shadow-bot', GIT_COMMITTER_EMAIL='stock-shadow-bot@users.noreply.github.com')
    with tempfile.TemporaryDirectory(prefix='stock-index-') as temp:
        env['GIT_INDEX_FILE'] = str(Path(temp) / 'index')
        git('read-tree', parent, env=env)
        for path, raw in outputs.items():
            blob = subprocess.run(['git', 'hash-object', '-w', '--stdin'], input=raw,
                                  capture_output=True, check=True).stdout.decode().strip()
            git('update-index', '--add', '--cacheinfo', '100644', blob, path, env=env)
        tree = git('write-tree', env=env).stdout.strip()
        return git('commit-tree', tree, '-p', parent, '-m',
                   f'data: stock {mode} generation {a["generation"][:16]}', env=env).stdout.strip()


def readback(commit, outputs, a):
    git('fetch', '--quiet', 'origin', 'main')
    require(git('merge-base', '--is-ancestor', commit, 'origin/main', check=False).returncode == 0,
            'PUBLISHED_COMMIT_NOT_AUTHORITATIVE')
    check_fence(a, 'origin/main')
    for path, raw in outputs.items():
        current = subprocess.run(['git', 'show', 'origin/main:' + path], check=True,
                                 capture_output=True).stdout
        require(current == raw, 'AUTHORITATIVE_SNAPSHOT_MISMATCH ' + path)
    print('STOCK_POST_PUSH_READBACK_OK', commit, a['generation'], flush=True)


def persist(mode, a=None):
    a = a or from_environment()
    outputs = snapshot(mode, a)
    # Test authority and input freshness even for a no-change or failed run.
    cas(a)
    audit = validate_outputs(outputs, mode, a)
    manifest = {
        'schema': 'stock_shadow_generation_v1', **a,
        'status': 'RUN_FAILED' if mode in ('health', 'preflight') else 'RUN_COMPLETED',
        'mode': mode, 'created_at': datetime.now(timezone.utc).isoformat(),
        'shadow_only': True, 'real_orders': False,
        'files': {path: hashlib.sha256(raw).hexdigest() for path, raw in sorted(outputs.items())},
        'reporting_audit': audit,
    }
    outputs[ROOT + 'runtime-' + a['job'] + '-v1.json'] = json_bytes(manifest)
    for attempt in range(1, 6):
        target = cas(a)
        commit = candidate_commit(target, outputs, a, mode)
        # Exact expected-main lease plus candidate's sole parent=target prevents
        # a paused/new-epoch writer or a branch rewind racing the push.
        pushed = git('push', '--porcelain',
                     '--force-with-lease=refs/heads/main:' + target,
                     'origin', commit + ':refs/heads/main', check=False)
        if pushed.returncode == 0:
            verify_candidate(commit, outputs, a)
            return {'commit': commit, 'generation': a['generation'],
                    'verified': True, 'mode': mode, 'reporting_audit': audit}
        # A lost acknowledgement is not permission to replay the output. Check
        # whether the candidate is already authoritative before creating another.
        try:
            git('fetch', '--quiet', 'origin', 'main')
        except Exception as exc:
            raise PublicationUnverified(commit, a['generation'], exc) from exc
        if git('merge-base', '--is-ancestor', commit, 'origin/main', check=False).returncode == 0:
            verify_candidate(commit, outputs, a)
            return {'commit': commit, 'generation': a['generation'],
                    'verified': True, 'mode': mode, 'reporting_audit': audit}
        print('STOCK_PUSH_RACE_RETRY', attempt, flush=True)
    raise RuntimeError('STOCK_PUSH_RETRY_EXHAUSTED')


def main():
    p = argparse.ArgumentParser()
    p.add_argument('mode', choices=['main', 'monitor', 'replay', 'health', 'preflight'])
    args = p.parse_args()
    try:
        print(json.dumps(persist(args.mode), sort_keys=True))
    except PublicationRejected as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(exc.code)


if __name__ == '__main__':
    main()
