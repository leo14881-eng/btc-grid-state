"""Offline stock writer migration acceptance. All remotes are local bare repos."""
import copy
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest
import test_stock_shadow as stock_tests

from research.stock_shadow.server import persist as publisher
from research.stock_shadow.server import runtime
from research.stock_shadow.server import runner
from test_stock_shadow import _p0_git_pair, _p0_persist_environment, _p0_run_persister


ROOT = Path(__file__).resolve().parents[1]
RESULTS = 'research/results/stock-shadow/'
CONFIG = '.github/stock-runtime.json'
PERSIST = ROOT / 'research/stock_shadow/persist_results.sh'
PROVIDER_KEYS = ('APCA_API_KEY_ID', 'APCA_API_SECRET_KEY', 'STOCKFIT_API_KEY', 'FMP_API_KEY')


def calendar_fixture():
    return {'date': '2026-10-06', 'open': '09:30', 'close': '16:00',
            'verified_at': '2026-10-06T07:26:00+00:00',
            'source': 'ALPACA_EXCHANGE_CALENDAR'}


def test_original_market_hours_gate_with_inherited_keys_reproduces_cache_refresh(tmp_path, monkeypatch):
    """A passing historical gate test can dirty today's cache before replay starts."""
    for key in PROVIDER_KEYS:
        monkeypatch.setenv(key, 'offline-dummy-' + key)
    monitor = stock_tests._load_position_monitor()
    cache = tmp_path / 'calendar-session-cache-v1.json'
    original = calendar_fixture()
    cache.write_text(json.dumps(original) + '\n')
    refreshed_at = '2026-10-06T19:48:00+00:00'
    monkeypatch.setattr(monitor, 'CALENDAR_CACHE', cache)
    monkeypatch.setattr(monitor, 'now', lambda: refreshed_at)
    monkeypatch.setattr(stock_tests, '_load_position_monitor', lambda: monitor)
    requests = []

    def fake_urlopen(request, **kwargs):
        requests.append(request.full_url)
        if request.full_url.endswith('start=2026-10-04&end=2026-10-04'):
            return io.BytesIO(b'[]')
        assert request.full_url.endswith('start=2026-10-06&end=2026-10-06')
        return io.BytesIO(b'[{"date":"2026-10-06","open":"09:30","close":"16:00"}]')

    monkeypatch.setattr(monitor.urllib.request, 'urlopen', fake_urlopen)
    stock_tests.test_position_monitor_market_hours_gate()
    assert json.loads(cache.read_text()) == {
        'date': '2026-10-04', 'closed': True, 'verified_at': refreshed_at,
        'source': 'ALPACA_EXCHANGE_CALENDAR',
    }

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 10, 6, 19, 48, tzinfo=timezone.utc)
            return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)

    # Replay uses this unchanged resolver; it repairs the cache dirtied by tests.
    monkeypatch.setattr(stock_tests.ss, 'datetime', FrozenDateTime)
    monkeypatch.setattr(stock_tests.ss, 'CALENDAR_CACHE', cache)
    monkeypatch.setattr(stock_tests.ss, 'API_USAGE', copy.deepcopy(stock_tests.ss.API_USAGE))
    session = stock_tests.ss._alpaca_exchange_session(FrozenDateTime.now(timezone.utc))
    assert session['date'] == original['date']
    refreshed = json.loads(cache.read_text())
    assert {key for key in original if original[key] != refreshed[key]} == {'verified_at'}
    assert refreshed == {**original, 'verified_at': refreshed_at}
    assert len(requests) == 2


def test_original_market_hours_gate_without_provider_keys_is_offline_and_read_only(tmp_path, monkeypatch):
    for key in PROVIDER_KEYS:
        monkeypatch.delenv(key, raising=False)
    monitor = stock_tests._load_position_monitor()
    cache = tmp_path / 'calendar-session-cache-v1.json'
    cache.write_text(json.dumps(calendar_fixture()) + '\n')
    before = cache.read_bytes()
    monkeypatch.setattr(monitor, 'CALENDAR_CACHE', cache)
    monkeypatch.setattr(stock_tests, '_load_position_monitor', lambda: monitor)
    monkeypatch.setattr(monitor.urllib.request, 'urlopen',
                        lambda *args, **kwargs: pytest.fail('gate test attempted provider access'))
    stock_tests.test_position_monitor_market_hours_gate()
    assert cache.read_bytes() == before
    assert monitor.CALENDAR_USAGE['http_requests'] == 0


def test_reporting_audit_detects_post_add_stale_pnl_without_rewriting_book():
    book = {
        'positions': {
            'POST_ADD': {
                'tranches': [{'price': 100.0, 'notional': 1000.0},
                             {'price': 100.0, 'notional': 1000.0}],
                'last_price': 100.0,
                'net_pnl_usdt': -4.0,
            },
        },
        'closed': [],
        'simulation_only': True,
    }
    before = copy.deepcopy(book)
    audit = publisher.reporting_audit(book)
    assert audit['status'] == 'REPORTING_MISMATCH'
    assert audit['mismatched_symbols'] == ['POST_ADD']
    assert len(audit['mismatched_symbols']) == 1
    assert audit['unpriced_symbols'] == []
    assert audit['stored_open_net_pnl'] == -4.0
    assert audit['recomputed_open_net_pnl'] == -8.0
    assert audit['strategy_effect'] is False and audit['ledger_rewritten'] is False
    assert book == before


def write_json(work, path, value):
    target = work / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value, sort_keys=True) + '\n')


def publish_other(git, writer, path, value):
    write_json(writer, path, value)
    git(writer, 'add', '--', path)
    git(writer, 'commit', '-m', 'other writer update')
    git(writer, 'push', 'origin', 'main')


def raw_persist(work, mode, env):
    return subprocess.run(['bash', str(PERSIST), mode], cwd=work, env=env,
                          capture_output=True, text=True)


def accepted_replay_fixture():
    """A genuinely publishable synthetic report, with no provider access."""
    return {
        'mode': 'REPLAY_OBSERVATION_ONLY', 'strategy_effect': False,
        'strategy_version': 'HYBRID_ENTRY_V1_POSITION_STATE_V3',
        'acceptance_status': 'ACCEPTED', 'errors': [], 'discovery_errors': [],
        'future_data_prohibited': True, 'future_mutation_invariance': True,
        'future_leakage_detected': False, 'lookahead_violations': [],
        'target_session': '2026-10-06', 'big_movers_total': 1,
        'data_health': {
            'status': 'COMPLETE', 'session_complete': True,
            'required_symbols': ['MOVER', 'SPY', 'QQQ'], 'missing_symbols': [],
            'regular_session_bar_counts': {'MOVER': 78, 'SPY': 78, 'QQQ': 78},
            'cohort_status': 'QUALIFYING_MOVERS',
        },
        'query_window': {
            'start': '2026-10-06T00:00:00Z', 'end': '2026-10-06T20:08:00Z',
            'as_of_utc': '2026-10-06T20:28:00Z', 'market_data_delay_minutes': 20,
        },
        'results': [{'symbol': 'MOVER', 'high_at': '2026-10-06T19:55:00Z',
                     'lookahead_check': 'PASS'}],
    }


def prepare_outputs(work, job='main', mode=None, env=None):
    env = env or _p0_persist_environment(work, job)
    if job in ('main', 'monitor'):
        write_json(work, RESULTS + job + '-run-health-v1.json', {
            'source_commit': env['STOCK_SHADOW_SOURCE_COMMIT'],
            'run_id': env['STOCK_SHADOW_RUN_ID'],
            'status': 'FAILED' if mode == 'health' else 'SUCCESS',
            'simulation_only': True, 'real_orders': False,
        })
    else:
        write_json(work, RESULTS + 'replay-v1.json', accepted_replay_fixture())
    return env


def set_base(git, work, writer, changes):
    for path, value in changes.items():
        write_json(work, path, value)
    git(work, 'add', '.')
    git(work, 'commit', '-m', 'fixture source')
    git(work, 'push', 'origin', 'main')
    git(writer, 'fetch', 'origin', 'main')
    git(writer, 'reset', '--hard', 'origin/main')


@pytest.fixture
def repo(tmp_path):
    return _p0_git_pair(tmp_path)


@pytest.mark.parametrize('owner,caller,accepted', [
    ('github', 'github', True), ('github', 'server', False),
    ('server', 'github', False), ('server', 'server', True),
    ('paused', 'github', False), ('paused', 'server', False),
])
def test_authority_is_explicit_and_never_heartbeat_failover(repo, monkeypatch, owner, caller, accepted):
    git, _, work, writer = repo
    config = json.loads((work / CONFIG).read_text())
    if owner != config['owner']:
        config.update(owner=owner, epoch=2)
        set_base(git, work, writer, {CONFIG: config})
    monkeypatch.chdir(work)
    a = runtime.admission(caller, 'main', 'offline-test.1')
    assert a['admitted'] is accepted
    assert a['epoch'] == config['epoch']
    assert runtime.read_config()['automatic_failover'] is False


@pytest.mark.parametrize('field,value', [
    ('schema', 'unknown'), ('owner', 'any'), ('epoch', 0), ('epoch', -1),
    ('epoch', True), ('epoch', '1'), ('epoch', 1.5),
    ('jobs', ['main', 'monitor']), ('shadow_only', False),
    ('automatic_failover', True),
])
def test_malformed_authority_fails_closed(repo, monkeypatch, field, value):
    git, _, work, writer = repo
    config = json.loads((work / CONFIG).read_text())
    config[field] = value
    set_base(git, work, writer, {CONFIG: config})
    monkeypatch.chdir(work)
    with pytest.raises(RuntimeError):
        runtime.admission('github', 'main', 'offline-test.1')


def test_generation_is_fixed_to_source_owner_epoch_job_and_attempt():
    args = ['a' * 40, 'github', 1, 'main', 'run.1']
    expected = runtime.generation(*args)
    assert expected == runtime.generation(*args)
    assert runtime.generation(*args, attempt=2) != expected
    for index, value in enumerate(['b' * 40, 'server', 2, 'monitor', 'run.2']):
        changed = args.copy()
        changed[index] = value
        assert runtime.generation(*changed) != expected


@pytest.mark.parametrize('run_id', ['', 'newline\ninjection', '../secret', 'x' * 161])
def test_invalid_run_identity_is_rejected(run_id):
    with pytest.raises(RuntimeError, match='INVALID_RUN_ID'):
        runtime.generation('a' * 40, 'github', 1, 'main', run_id)


@pytest.mark.parametrize('field', ['SOURCE_COMMIT', 'WRITER', 'EPOCH', 'JOB', 'RUN_ID', 'GENERATION', 'ATTEMPT'])
def test_persister_requires_every_admission_field(repo, field):
    _, _, work, _ = repo
    env = prepare_outputs(work)
    del env['STOCK_SHADOW_' + field]
    result = raw_persist(work, 'main', env)
    assert result.returncode != 0
    assert 'MISSING_STOCK_ADMISSION' in result.stderr


@pytest.mark.parametrize('field,value', [
    ('SOURCE_COMMIT', 'f' * 40), ('WRITER', 'server'), ('EPOCH', '2'),
    ('JOB', 'monitor'), ('RUN_ID', 'some-other-run'), ('GENERATION', '0' * 64), ('ATTEMPT', '2'),
])
def test_admission_tampering_cannot_rebind_old_output(repo, field, value):
    git, remote, work, _ = repo
    before = git(remote, 'rev-parse', 'main').stdout
    env = prepare_outputs(work)
    env['STOCK_SHADOW_' + field] = value
    result = raw_persist(work, 'main', env)
    assert result.returncode != 0
    assert git(remote, 'rev-parse', 'main').stdout == before


@pytest.mark.parametrize('job,mode', [('main', 'main'), ('monitor', 'monitor'), ('replay', 'replay'), ('main', 'health')])
def test_every_publication_mode_rejects_owner_change(repo, job, mode):
    git, remote, work, writer = repo
    env = prepare_outputs(work, job, mode)
    config = json.loads((writer / CONFIG).read_text())
    config.update(owner='paused', epoch=2)
    publish_other(git, writer, CONFIG, config)
    before = git(remote, 'rev-parse', 'main').stdout
    result = raw_persist(work, mode, env)
    assert result.returncode == 44, result.stdout + result.stderr
    assert git(remote, 'rev-parse', 'main').stdout == before


def test_old_github_generation_cannot_return_after_an_owner_aba(repo):
    git, remote, work, writer = repo
    env = prepare_outputs(work)
    config = json.loads((writer / CONFIG).read_text())
    for owner, epoch in [('paused', 2), ('server', 3), ('github', 4)]:
        config.update(owner=owner, epoch=epoch)
        publish_other(git, writer, CONFIG, config)
    result = raw_persist(work, 'main', env)
    assert result.returncode == 44, result.stdout + result.stderr
    assert json.loads(git(remote, 'show', 'main:' + CONFIG).stdout)['epoch'] == 4


@pytest.mark.parametrize('path', [
    'summary-v1.json', 'market-daily-cache-v1.json', 'review-v1.json',
    'main-run-health-v1.json', 'monitor-run-health-v1.json', 'replay-v1.json',
    'calendar-session-cache-v1.json', 'runtime-monitor-v1.json',
])
def test_all_stock_outputs_are_conflict_protected(repo, path):
    git, remote, work, writer = repo
    env = prepare_outputs(work)
    publish_other(git, writer, RESULTS + path, {'fixture': 'fresh'})
    before = git(remote, 'rev-parse', 'main').stdout
    result = raw_persist(work, 'main', env)
    assert result.returncode == 43, result.stdout + result.stderr
    assert git(remote, 'rev-parse', 'main').stdout == before


@pytest.mark.parametrize('path', [
    'research/stock_shadow/server/other.py', 'tests/test_stock_server_migration.py',
    '.github/workflows/stock-shadow.yml', 'deploy/stock-shadow/runner.sh',
])
def test_execution_and_deployment_changes_reject_stale_results(repo, path):
    git, _, work, writer = repo
    env = prepare_outputs(work)
    publish_other(git, writer, path, {'fixture': 'new deployment'})
    result = raw_persist(work, 'main', env)
    assert result.returncode == 42, result.stdout + result.stderr


def test_pause_between_final_fetch_and_push_fences_the_candidate(repo):
    git, remote, work, writer = repo
    env = prepare_outputs(work)
    config = json.loads((writer / CONFIG).read_text())
    config.update(owner='paused', epoch=2)
    write_json(writer, CONFIG, config)
    git(writer, 'add', CONFIG)
    git(writer, 'commit', '-m', 'cutover pause')
    hook = work / '.git/hooks/pre-push'
    hook.write_text('#!/bin/sh\nif [ ! -f .git/pause-injected ]; then\n'
                    'touch .git/pause-injected\ngit -C ' + shlex.quote(str(writer)) +
                    ' push origin main\nfi\n')
    hook.chmod(0o755)
    result = raw_persist(work, 'main', env)
    assert result.returncode == 44, result.stdout + result.stderr
    assert json.loads(git(remote, 'show', 'main:' + CONFIG).stdout)['owner'] == 'paused'
    assert git(remote, 'ls-tree', '--name-only', 'main', '--', RESULTS + 'runtime-main-v1.json').stdout == ''


@pytest.mark.parametrize('staged', [False, True])
def test_nonstock_modification_is_never_published(repo, staged):
    git, remote, work, _ = repo
    env = prepare_outputs(work)
    write_json(work, 'hunter-state.json', {'unexpected': True})
    if staged:
        git(work, 'add', 'hunter-state.json')
    before = git(remote, 'rev-parse', 'main').stdout
    result = raw_persist(work, 'main', env)
    assert result.returncode != 0
    assert 'UNEXPECTED_MUTATION' in result.stderr
    assert git(remote, 'rev-parse', 'main').stdout == before


def test_deleted_and_symlink_outputs_are_rejected(repo):
    _, _, work, _ = repo
    env = prepare_outputs(work)
    target = work / (RESULTS + 'trades-v1.json')
    target.unlink()
    result = raw_persist(work, 'main', env)
    assert result.returncode != 0 and 'DELETED_OR_SYMLINK_OUTPUT' in result.stderr
    target.symlink_to(work / 'unrelated.txt')
    result = raw_persist(work, 'main', env)
    assert result.returncode != 0 and 'DELETED_OR_SYMLINK_OUTPUT' in result.stderr


@pytest.mark.parametrize('name', ['summary-v1.json', 'calendar-session-cache-v1.json'])
def test_mode_cannot_publish_another_jobs_output(repo, name):
    git, remote, work, _ = repo
    before = git(remote, 'rev-parse', 'main').stdout
    env = prepare_outputs(work, 'replay')
    write_json(work, RESULTS + name, {'unexpected': True})
    result = raw_persist(work, 'replay', env)
    assert result.returncode != 0 and 'UNEXPECTED_MUTATION' in result.stderr
    assert git(remote, 'rev-parse', 'main').stdout == before
    assert json.loads((work / (RESULTS + name)).read_text()) == {'unexpected': True}
    assert publisher.OUTPUTS['replay'] == {'replay-v1.json'}


def test_failure_health_does_not_publish_partial_ledger(repo):
    git, remote, work, _ = repo
    before = git(remote, 'show', 'main:' + RESULTS + 'portfolio-v1.json').stdout
    env = prepare_outputs(work, 'main', 'health')
    write_json(work, RESULTS + 'portfolio-v1.json', {'broken': 'partial'})
    result = raw_persist(work, 'health', env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert git(remote, 'show', 'main:' + RESULTS + 'portfolio-v1.json').stdout == before
    receipt = json.loads(git(remote, 'show', 'main:' + RESULTS + 'runtime-main-v1.json').stdout)
    assert receipt['status'] == 'RUN_FAILED'
    assert set(receipt['files']) == {RESULTS + 'main-run-health-v1.json'}


def test_failure_health_cannot_replace_a_success_receipt(repo):
    _, _, work, _ = repo
    env = prepare_outputs(work, 'main')
    result = raw_persist(work, 'health', env)
    assert result.returncode != 0 and 'NOT_FAILURE_HEALTH' in result.stderr


@pytest.mark.parametrize('field,value', [('source_commit', 'a' * 40), ('run_id', 'wrong-run'), ('real_orders', True)])
def test_run_health_is_bound_and_shadow_only(repo, field, value):
    _, _, work, _ = repo
    env = prepare_outputs(work)
    path = RESULTS + 'main-run-health-v1.json'
    health = json.loads((work / path).read_text())
    health[field] = value
    write_json(work, path, health)
    result = raw_persist(work, 'main', env)
    assert result.returncode != 0
    assert 'HEALTH_' in result.stderr


def test_changed_portfolio_requires_matching_run_identity(repo):
    _, _, work, _ = repo
    env = prepare_outputs(work)
    write_json(work, RESULTS + 'portfolio-v1.json', {
        'positions': {}, 'closed': [], 'simulation_only': True,
        'source_commit': env['STOCK_SHADOW_SOURCE_COMMIT'], 'run_id': 'wrong-run',
    })
    result = raw_persist(work, 'main', env)
    assert result.returncode != 0 and 'PORTFOLIO_RUN_BINDING_MISMATCH' in result.stderr


def test_atomic_publication_binds_hashes_and_preserves_source_checkout(repo):
    git, remote, work, _ = repo
    env = prepare_outputs(work)
    write_json(work, RESULTS + 'summary-v1.json', {'fixture': 'atomic report'})
    source = git(work, 'rev-parse', 'HEAD').stdout.strip()
    result = raw_persist(work, 'main', env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'STOCK_POST_PUSH_READBACK_OK' in result.stdout
    commit = git(remote, 'rev-parse', 'main').stdout.strip()
    assert git(remote, 'rev-parse', commit + '^').stdout.strip() == source
    assert git(work, 'rev-parse', 'HEAD').stdout.strip() == source
    receipt = json.loads(git(remote, 'show', 'main:' + RESULTS + 'runtime-main-v1.json').stdout)
    assert receipt['source_commit'] == source
    assert receipt['generation'] == env['STOCK_SHADOW_GENERATION']
    assert receipt['run_id'] == env['STOCK_SHADOW_RUN_ID']
    assert receipt['epoch'] == 1 and receipt['owner'] == 'github'
    changed = set(git(remote, 'diff', '--name-only', source, commit).stdout.splitlines())
    assert changed == set(receipt['files']) | {RESULTS + 'runtime-main-v1.json'}
    for path, expected in receipt['files'].items():
        raw = subprocess.run(['git', 'show', 'main:' + path], cwd=remote,
                             check=True, capture_output=True).stdout
        assert hashlib.sha256(raw).hexdigest() == expected


def test_portfolio_and_trades_always_publish_as_one_pair(repo):
    git, remote, work, _ = repo
    env = prepare_outputs(work)
    write_json(work, RESULTS + 'portfolio-v1.json', {
        'positions': {}, 'closed': [], 'simulation_only': True,
        'source_commit': env['STOCK_SHADOW_SOURCE_COMMIT'], 'run_id': env['STOCK_SHADOW_RUN_ID'],
    })
    result = raw_persist(work, 'main', env)
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads(git(remote, 'show', 'main:' + RESULTS + 'runtime-main-v1.json').stdout)
    assert {RESULTS + 'portfolio-v1.json', RESULTS + 'trades-v1.json'} <= set(receipt['files'])


def test_historical_events_cannot_be_removed(repo):
    git, _, work, writer = repo
    set_base(git, work, writer, {RESULTS + 'trades-v1.json': [
        {'type': 'BUY', 'symbol': 'TEST', 'at': '2026-10-05T15:00:00Z', 'price': 100, 'notional': 1000},
    ]})
    env = prepare_outputs(work)
    write_json(work, RESULTS + 'trades-v1.json', [])
    result = raw_persist(work, 'main', env)
    assert result.returncode != 0 and 'HISTORICAL_EVENTS_CHANGED' in result.stderr


def test_readback_detects_a_snapshot_changed_after_accepted_push(repo, monkeypatch):
    git, _, work, writer = repo
    env = prepare_outputs(work)
    monkeypatch.chdir(work)
    for key, value in env.items():
        if key.startswith('STOCK_SHADOW_'):
            monkeypatch.setenv(key, value)
    original = publisher.readback

    def race_readback(commit, outputs, admission):
        git(writer, 'fetch', 'origin', 'main')
        git(writer, 'reset', '--hard', 'origin/main')
        publish_other(git, writer, RESULTS + 'main-run-health-v1.json', {'fixture': 'superseding result'})
        return original(commit, outputs, admission)

    monkeypatch.setattr(publisher, 'readback', race_readback)
    with pytest.raises(publisher.PublicationUnverified) as error:
        publisher.persist('main')
    assert 'AUTHORITATIVE_SNAPSHOT_MISMATCH' in str(error.value.__cause__)
    assert error.value.generation == env['STOCK_SHADOW_GENERATION']


def test_no_change_candidate_still_checks_remote_authority(repo, monkeypatch):
    git, _, work, writer = repo
    config = json.loads((writer / CONFIG).read_text())
    config.update(owner='paused', epoch=2)
    publish_other(git, writer, CONFIG, config)
    monkeypatch.chdir(work)
    a = runtime.admission('github', 'main', 'no-output.1')
    with pytest.raises(publisher.PublicationRejected) as error:
        publisher.persist('main', a)
    assert error.value.code == 44


def test_lost_push_acknowledgement_reads_back_without_duplicate_publication(repo, monkeypatch):
    git, remote, work, _ = repo
    env = prepare_outputs(work)
    source = env['STOCK_SHADOW_SOURCE_COMMIT']
    monkeypatch.chdir(work)
    for key, value in env.items():
        if key.startswith('STOCK_SHADOW_'):
            monkeypatch.setenv(key, value)
    original = publisher.git
    pushes = []

    def ambiguous_git(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[0] == 'push':
            pushes.append(args)
            assert result.returncode == 0
            return subprocess.CompletedProcess(result.args, 1, result.stdout, 'offline simulated lost acknowledgement')
        return result

    monkeypatch.setattr(publisher, 'git', ambiguous_git)
    result = publisher.persist('main')
    assert result['verified'] is True
    assert len(pushes) == 1
    assert git(remote, 'rev-list', '--count', source + '..main').stdout.strip() == '1'


def test_branch_rewind_cannot_turn_publication_into_a_force_overwrite(repo):
    git, remote, work, writer = repo
    ancestor = git(remote, 'rev-parse', 'main').stdout.strip()
    set_base(git, work, writer, {'fixture-source.json': {'revision': 2}})
    env = prepare_outputs(work)
    git(writer, 'push', '--force-with-lease', 'origin', ancestor + ':refs/heads/main')
    result = raw_persist(work, 'main', env)
    assert result.returncode != 0 and 'SOURCE_NO_LONGER_ANCESTOR' in result.stderr
    assert git(remote, 'rev-parse', 'main').stdout.strip() == ancestor


def test_partition_does_not_grant_ownership_and_old_inflight_run_is_fenced_on_resume(repo, monkeypatch):
    git, remote, work, writer = repo
    env = prepare_outputs(work)
    monkeypatch.chdir(work)
    a = runtime.admission('github', 'main', env['STOCK_SHADOW_RUN_ID'])
    original = publisher.git
    partitioned = True
    pushes = []

    def partition_git(*args, **kwargs):
        if args[0] == 'fetch' and partitioned:
            raise subprocess.CalledProcessError(128, ['git', *args], stderr='offline simulated partition')
        if args[0] == 'push':
            pushes.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(publisher, 'git', partition_git)
    with pytest.raises(subprocess.CalledProcessError):
        publisher.persist('main', a)
    config = json.loads(git(remote, 'show', 'main:' + CONFIG).stdout)
    assert config['owner'] == 'github' and config['epoch'] == 1
    assert pushes == []
    config.update(owner='server', epoch=3)
    publish_other(git, writer, CONFIG, config)
    partitioned = False
    with pytest.raises(publisher.PublicationRejected) as error:
        publisher.persist('main', a)
    assert error.value.code == 44 and pushes == []


@pytest.mark.parametrize('lost_ack', [False, True])
def test_partition_after_push_records_candidate_and_never_claims_success(repo, monkeypatch, capsys, lost_ack):
    git, remote, work, _ = repo
    env = prepare_outputs(work)
    source = env['STOCK_SHADOW_SOURCE_COMMIT']
    monkeypatch.chdir(work)
    a = runtime.admission('github', 'main', env['STOCK_SHADOW_RUN_ID'])
    original = publisher.git
    pushed = []

    def partition_git(*args, **kwargs):
        if args[0] == 'fetch' and pushed:
            raise subprocess.CalledProcessError(128, ['git', *args], stderr='offline simulated partition')
        result = original(*args, **kwargs)
        if args[0] == 'push':
            pushed.append(args)
            assert result.returncode == 0
            if lost_ack:
                return subprocess.CompletedProcess(result.args, 1, result.stdout, 'offline lost acknowledgement')
        return result

    monkeypatch.setattr(publisher, 'git', partition_git)
    with pytest.raises(publisher.PublicationUnverified) as error:
        publisher.persist('main', a)
    assert len(pushed) == 1
    assert error.value.commit == git(remote, 'rev-parse', 'main').stdout.strip()
    assert error.value.generation == a['generation']
    assert git(remote, 'rev-list', '--count', source + '..main').stdout.strip() == '1'
    assert 'STOCK_POST_PUSH_READBACK_OK' not in capsys.readouterr().out


def test_repeated_crypto_and_hunter_push_races_preserve_both_systems(repo):
    git, remote, work, writer = repo
    env = prepare_outputs(work)
    write_json(work, RESULTS + 'summary-v1.json', {'fixture': 'stock final'})
    writer_arg = shlex.quote(str(writer))
    hook = work / '.git/hooks/pre-push'
    hook.write_text('#!/bin/sh\nset -eu\n'
                    'n=$(cat .git/race-count 2>/dev/null || echo 0)\nn=$((n+1))\n'
                    'printf "%s" "$n" > .git/race-count\n'
                    'if [ "$n" -le 3 ]; then\n'
                    'git -C ' + writer_arg + ' fetch -q origin main\n'
                    'git -C ' + writer_arg + ' reset -q --hard origin/main\n'
                    'printf "crypto-race-%s\\n" "$n" > ' + shlex.quote(str(writer / 'state.json')) + '\n'
                    'printf "hunter-race-%s\\n" "$n" > ' + shlex.quote(str(writer / 'hunter-state.json')) + '\n'
                    'git -C ' + writer_arg + ' add state.json hunter-state.json\n'
                    'git -C ' + writer_arg + ' commit -q -m "independent-system-race-$n"\n'
                    'git -C ' + writer_arg + ' push -q origin main\nfi\n')
    hook.chmod(0o755)
    result = raw_persist(work, 'main', env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (work / '.git/race-count').read_text() == '4'
    assert git(remote, 'show', 'main:state.json').stdout == 'crypto-race-3\n'
    assert git(remote, 'show', 'main:hunter-state.json').stdout == 'hunter-race-3\n'
    assert 'stock final' in git(remote, 'show', 'main:' + RESULTS + 'summary-v1.json').stdout


def configure_server(git, work, writer):
    config = json.loads((work / CONFIG).read_text())
    config.update(owner='server', epoch=3)
    set_base(git, work, writer, {CONFIG: config})


def stub_engine(monkeypatch, fail=False):
    original = subprocess.run
    commands = []

    def run(command, **kwargs):
        if command[0] != sys.executable:
            return original(command, **kwargs)
        commands.append((command, kwargs))
        if command[1].endswith(('stock_shadow_v1.py', 'stock_position_monitor.py')):
            env = kwargs['env']
            write_json(Path.cwd(), RESULTS + env['STOCK_SHADOW_JOB'] + '-run-health-v1.json', {
                'source_commit': env['STOCK_SHADOW_SOURCE_COMMIT'],
                'run_id': env['STOCK_SHADOW_RUN_ID'],
                'status': 'FAILED' if fail else 'SUCCESS',
                'simulation_only': True, 'real_orders': False,
            })
            if fail:
                write_json(Path.cwd(), RESULTS + 'portfolio-v1.json', {'partial': True})
                raise subprocess.CalledProcessError(1, command)
        elif command[1].endswith('stock_replay.py'):
            prepare_outputs(Path.cwd(), 'replay', env=kwargs['env'])
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(runner.subprocess, 'run', run)
    return commands


@pytest.mark.parametrize('job', ['main', 'monitor'])
@pytest.mark.parametrize('preview', [False, True])
@pytest.mark.parametrize('failure', ['timeout', 'assertion'])
def test_preflight_failure_reports_health_without_running_engine_or_changing_book(
        repo, monkeypatch, tmp_path, job, preview, failure):
    git, remote, work, writer = repo
    configure_server(git, work, writer)
    monkeypatch.chdir(work)
    for key in PROVIDER_KEYS:
        monkeypatch.setenv(key, 'offline-dummy-' + key)
    before = git(remote, 'rev-parse', 'main').stdout.strip()
    old_book = git(remote, 'show', 'main:' + RESULTS + 'portfolio-v1.json').stdout
    old_trades = git(remote, 'show', 'main:' + RESULTS + 'trades-v1.json').stdout
    old_engine_tree = git(remote, 'ls-tree', 'main', '--', RESULTS + job + '-run-health-v1.json').stdout
    original = subprocess.run
    calls = []

    def fail_tests(command, **kwargs):
        if command[0] != sys.executable:
            return original(command, **kwargs)
        calls.append(command)
        assert command[1:3] == ['-m', 'pytest'], 'engine must not execute'
        assert kwargs['timeout'] == 180
        assert all(key not in kwargs['env'] for key in PROVIDER_KEYS)
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(command, 180)
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(runner.subprocess, 'run', fail_tests)
    with pytest.raises((subprocess.TimeoutExpired, subprocess.CalledProcessError)):
        runner.run(job, preview, tmp_path / 'runtime')
    assert len(calls) == 1
    record = json.loads(next((tmp_path / 'runtime').glob('run-*.json')).read_text())
    assert record['status'] == 'FAILED' and record['verified'] is False
    assert git(remote, 'show', 'main:' + RESULTS + 'portfolio-v1.json').stdout == old_book
    assert git(remote, 'show', 'main:' + RESULTS + 'trades-v1.json').stdout == old_trades
    assert git(remote, 'ls-tree', 'main', '--', RESULTS + job + '-run-health-v1.json').stdout == old_engine_tree
    if preview:
        assert git(remote, 'rev-parse', 'main').stdout.strip() == before
        assert 'failure_health_publication' not in record
    else:
        receipt = record['failure_health_publication']
        assert receipt['verified'] is True and receipt['mode'] == 'preflight'
        health = json.loads(git(remote, 'show', 'main:' + RESULTS + job + '-preflight-health-v1.json').stdout)
        assert health['status'] == 'FAILED' and health['stage'] == 'PREFLIGHT'
        assert health['engine_started'] is False
        assert health['source_commit'] == before and health['run_id'] == record['run_id']
        assert health['epoch'] == 3 and health['owner'] == 'server'
        assert health['real_orders'] is False and health['simulation_only'] is True
        changed = git(remote, 'diff', '--name-only', before, 'main').stdout.splitlines()
        assert set(changed) == {RESULTS + job + '-preflight-health-v1.json',
                                RESULTS + 'runtime-' + job + '-v1.json'}


@pytest.mark.parametrize('race', ['epoch', 'ledger', 'successful_health'])
def test_preflight_failure_health_cannot_overwrite_new_authority_or_state(
        repo, monkeypatch, tmp_path, race):
    git, remote, work, writer = repo
    configure_server(git, work, writer)
    monkeypatch.chdir(work)
    monkeypatch.setenv('APCA_API_KEY_ID', 'offline-key')
    monkeypatch.setenv('APCA_API_SECRET_KEY', 'offline-secret')
    original = subprocess.run
    published = []

    def fail_after_race(command, **kwargs):
        if command[0] != sys.executable:
            return original(command, **kwargs)
        assert command[1:3] == ['-m', 'pytest']
        if race == 'epoch':
            value = json.loads((writer / CONFIG).read_text())
            value['epoch'] += 1
            publish_other(git, writer, CONFIG, value)
        elif race == 'ledger':
            publish_other(git, writer, RESULTS + 'portfolio-v1.json', {'newer': 'book'})
        else:
            publish_other(git, writer, RESULTS + 'main-run-health-v1.json', {'status': 'SUCCESS', 'run_id': 'newer'})
        published.append(git(remote, 'rev-parse', 'main').stdout)
        raise subprocess.TimeoutExpired(command, 180)

    monkeypatch.setattr(runner.subprocess, 'run', fail_after_race)
    with pytest.raises(subprocess.TimeoutExpired):
        runner.run('main', False, tmp_path / 'runtime')
    assert git(remote, 'rev-parse', 'main').stdout == published[0]
    record = json.loads(next((tmp_path / 'runtime').glob('run-*.json')).read_text())
    assert 'failure_health_publication' not in record
    assert 'failure_health_error' in record


@pytest.mark.parametrize('job', ['main', 'monitor', 'replay'])
@pytest.mark.parametrize('preview', [False, True])
def test_server_tests_strip_provider_keys_but_engines_keep_them(repo, monkeypatch, tmp_path, job, preview):
    git, _, work, writer = repo
    configure_server(git, work, writer)
    monkeypatch.chdir(work)
    for key in PROVIDER_KEYS:
        monkeypatch.setenv(key, 'offline-dummy-' + key)
    monkeypatch.setenv('STOCK_TEST_HARMLESS_SETTING', 'preserved')
    commands = stub_engine(monkeypatch)
    publications = []

    def persist(mode, a):
        publications.append(mode)
        return {'verified': True, 'generation': a['generation'], 'commit': 'c' * 40}

    monkeypatch.setattr(runner, 'persist', persist)
    result = runner.run(job, preview, tmp_path / 'runtime')
    assert commands[0][0][1:3] == ['-m', 'pytest']
    assert len(commands) == 1 + len(runner.COMMANDS[job])
    test_env = commands[0][1]['env']
    assert all(key not in test_env for key in PROVIDER_KEYS)
    for command, kwargs in commands[1:]:
        assert command[1] in {script for script, _ in runner.COMMANDS[job]}
        assert kwargs['env'] is not test_env
        assert all(kwargs['env'][key] == 'offline-dummy-' + key for key in PROVIDER_KEYS)
    for _, kwargs in commands:
        assert kwargs['env']['STOCK_TEST_HARMLESS_SETTING'] == 'preserved'
        assert kwargs['env']['STOCK_SHADOW_GENERATION'] == result['generation']
    assert all(os.environ[key] == 'offline-dummy-' + key for key in PROVIDER_KEYS)
    assert publications == ([] if preview else [job])


@pytest.mark.parametrize('job', ['main', 'monitor', 'replay'])
@pytest.mark.parametrize('preview', [False, True])
@pytest.mark.parametrize('inject_failure_health', [False, True])
def test_server_test_mutation_stops_before_engines_or_publication(
        repo, monkeypatch, tmp_path, job, preview, inject_failure_health):
    git, remote, work, writer = repo
    configure_server(git, work, writer)
    cache_path = RESULTS + 'calendar-session-cache-v1.json'
    original_cache = calendar_fixture()
    set_base(git, work, writer, {cache_path: original_cache})
    source = git(remote, 'rev-parse', 'main').stdout
    monkeypatch.chdir(work)
    for key in PROVIDER_KEYS:
        monkeypatch.setenv(key, 'offline-dummy-' + key)
    original_run = subprocess.run
    commands = []
    changed_cache = {**original_cache, 'verified_at': '2026-10-06T19:48:00+00:00'}

    def mutate_during_tests(command, **kwargs):
        if command[0] != sys.executable:
            return original_run(command, **kwargs)
        commands.append(command)
        assert command[1:3] == ['-m', 'pytest'], 'engine executed after tests dirtied checkout'
        write_json(work, cache_path, changed_cache)
        if inject_failure_health:
            # A test-created health document must never masquerade as engine output.
            env = kwargs['env']
            write_json(work, RESULTS + job + '-run-health-v1.json', {
                'status': 'FAILED', 'run_id': env['STOCK_SHADOW_RUN_ID'],
                'source_commit': env['STOCK_SHADOW_SOURCE_COMMIT'],
                'simulation_only': True, 'real_orders': False,
            })
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(runner.subprocess, 'run', mutate_during_tests)
    monkeypatch.setattr(runner, 'persist',
                        lambda *args: pytest.fail('publication attempted after test mutation'))
    with pytest.raises(RuntimeError, match='STOCK_TESTS_MUTATED_CHECKOUT'):
        runner.run(job, preview, tmp_path / 'runtime')
    assert len(commands) == 1
    assert git(remote, 'rev-parse', 'main').stdout == source
    assert git(work, 'rev-parse', 'HEAD').stdout == source
    assert json.loads((work / cache_path).read_text()) == changed_cache
    assert cache_path in publisher.changed_paths()
    record = json.loads(next((tmp_path / 'runtime').glob('run-*.json')).read_text())
    assert record['status'] == 'FAILED' and record['verified'] is False
    assert record['error'] == 'STOCK_TESTS_MUTATED_CHECKOUT'
    assert 'failure_health_publication' not in record
    assert 'failure_health_error' not in record
    if preview:
        retained = Path(record['preview_outputs']) / 'stock-shadow/calendar-session-cache-v1.json'
        assert json.loads(retained.read_text()) == changed_cache


def test_server_preview_never_reads_or_archives_mutated_canonical_root_symlink(
        repo, monkeypatch, tmp_path):
    git, remote, work, _ = repo
    source = git(remote, 'rev-parse', 'main').stdout
    canonical_root = work / RESULTS
    original_root = tmp_path / 'preserved-original-stock-output'
    external_target = tmp_path / 'external-target'
    external_target.mkdir()
    (external_target / 'sentinel.txt').write_text('must not be read or archived\n')
    monkeypatch.chdir(work)
    original_run = subprocess.run
    original_open = Path.open
    original_rglob = Path.rglob
    commands = []

    def guarded_open(path, *args, **kwargs):
        assert external_target not in path.resolve().parents, 'symlink target file was opened'
        return original_open(path, *args, **kwargs)

    def guarded_rglob(path, *args, **kwargs):
        assert path.resolve() != external_target, 'symlink target directory was traversed'
        return original_rglob(path, *args, **kwargs)

    def mutate_during_tests(command, **kwargs):
        if command[0] != sys.executable:
            return original_run(command, **kwargs)
        commands.append(command)
        assert command[1:3] == ['-m', 'pytest'], 'engine executed after root symlink mutation'
        canonical_root.rename(original_root)
        canonical_root.symlink_to(external_target, target_is_directory=True)
        assert runner.canonical_inventory() == {RESULTS: ('symlink', str(external_target))}
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(Path, 'open', guarded_open)
    monkeypatch.setattr(Path, 'rglob', guarded_rglob)
    monkeypatch.setattr(runner.subprocess, 'run', mutate_during_tests)
    monkeypatch.setattr(runner.shutil, 'copytree',
                        lambda *args, **kwargs: pytest.fail('symlinked canonical root was archived'))
    monkeypatch.setattr(runner, 'persist', lambda *args: pytest.fail('root mutation was published'))
    with pytest.raises(RuntimeError, match='STOCK_TESTS_MUTATED_CHECKOUT'):
        runner.run('replay', True, tmp_path / 'runtime')
    assert len(commands) == 1
    assert git(remote, 'rev-parse', 'main').stdout == source
    assert canonical_root.is_symlink() and canonical_root.readlink() == external_target
    assert (original_root / 'portfolio-v1.json').is_file()
    record = json.loads(next((tmp_path / 'runtime').glob('run-*.json')).read_text())
    assert record['status'] == 'FAILED' and record['verified'] is False
    assert record['error'] == 'STOCK_TESTS_MUTATED_CHECKOUT'
    assert record['preview_archive_error'] == 'CANONICAL_ROOT_IS_SYMLINK'
    assert 'preview_outputs' not in record
    assert not list((tmp_path / 'runtime').rglob('sentinel.txt'))


@pytest.mark.parametrize('mutation', ['create', 'modify', 'delete', 'symlink'])
def test_server_test_boundary_detects_git_ignored_stock_mutations(
        repo, monkeypatch, tmp_path, mutation):
    git, remote, work, _ = repo
    source = git(remote, 'rev-parse', 'main').stdout
    ignored = work / (RESULTS + 'ignored-test-cache.json')
    (work / '.git/info/exclude').write_text(RESULTS + 'ignored-test-cache.json\n')
    if mutation in ('modify', 'delete'):
        ignored.write_text('{"fixture":"before"}\n')
    monkeypatch.chdir(work)
    assert publisher.changed_paths() == []
    original_run = subprocess.run
    commands = []

    def mutate_during_tests(command, **kwargs):
        if command[0] != sys.executable:
            return original_run(command, **kwargs)
        commands.append(command)
        assert command[1:3] == ['-m', 'pytest'], 'engine executed after ignored test mutation'
        if mutation == 'delete':
            ignored.unlink()
        elif mutation == 'symlink':
            ignored.symlink_to(work / 'unrelated.txt')
        else:
            ignored.write_text('{"fixture":"after"}\n')
        assert publisher.changed_paths() == []
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(runner.subprocess, 'run', mutate_during_tests)
    monkeypatch.setattr(runner, 'persist', lambda *args: pytest.fail('test mutation was published'))
    with pytest.raises(RuntimeError, match='STOCK_TESTS_MUTATED_CHECKOUT'):
        runner.run('replay', True, tmp_path / 'runtime')
    assert len(commands) == 1
    assert git(remote, 'rev-parse', 'main').stdout == source
    if mutation == 'delete':
        assert not ignored.exists()
    elif mutation == 'symlink':
        assert ignored.is_symlink() and ignored.readlink() == work / 'unrelated.txt'
    else:
        assert json.loads(ignored.read_text()) == {'fixture': 'after'}
    record = json.loads(next((tmp_path / 'runtime').glob('run-*.json')).read_text())
    assert record['status'] == 'FAILED' and record['verified'] is False
    if mutation == 'symlink':
        retained = Path(record['preview_outputs']) / 'stock-shadow/ignored-test-cache.json'
        assert retained.is_symlink() and retained.readlink() == work / 'unrelated.txt'


@pytest.mark.parametrize('preview', [False, True])
def test_replay_engine_timestamp_only_cache_mutation_still_fails_closed(
        repo, monkeypatch, tmp_path, preview):
    git, remote, work, writer = repo
    configure_server(git, work, writer)
    cache_path = RESULTS + 'calendar-session-cache-v1.json'
    original_cache = calendar_fixture()
    set_base(git, work, writer, {cache_path: original_cache})
    source = git(remote, 'rev-parse', 'main').stdout
    monkeypatch.chdir(work)
    monkeypatch.setenv('APCA_API_KEY_ID', 'offline-dummy-key')
    monkeypatch.setenv('APCA_API_SECRET_KEY', 'offline-dummy-secret')
    original_run = subprocess.run
    commands = []
    changed_cache = {**original_cache, 'verified_at': '2026-10-06T19:48:00+00:00'}

    def mutate_during_replay(command, **kwargs):
        if command[0] != sys.executable:
            return original_run(command, **kwargs)
        commands.append(command)
        if command[1:3] != ['-m', 'pytest']:
            assert command[1] == 'research/stock_shadow/stock_replay.py'
            prepare_outputs(work, 'replay', env=kwargs['env'])
            write_json(work, cache_path, changed_cache)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(runner.subprocess, 'run', mutate_during_replay)
    monkeypatch.setattr(runner, 'persist', lambda *args: pytest.fail('replay cache mutation was published'))
    with pytest.raises(RuntimeError, match='UNEXPECTED_MUTATION'):
        runner.run('replay', preview, tmp_path / 'runtime')
    assert len(commands) == 2
    assert git(remote, 'rev-parse', 'main').stdout == source
    assert json.loads((work / cache_path).read_text()) == changed_cache
    assert publisher.changed_paths() == sorted([cache_path, RESULTS + 'replay-v1.json'])
    assert publisher.OUTPUTS['replay'] == {'replay-v1.json'}
    record = json.loads(next((tmp_path / 'runtime').glob('run-*.json')).read_text())
    assert record['status'] == 'FAILED' and record['verified'] is False
    assert record['error'] == 'UNEXPECTED_MUTATION'


def test_server_runner_refuses_publish_when_github_owns_repo(repo, monkeypatch, tmp_path):
    _, _, work, _ = repo
    monkeypatch.chdir(work)
    commands = stub_engine(monkeypatch)
    result = runner.run('main', False, tmp_path / 'runtime')
    assert result['status'] == 'SKIPPED_NOT_OWNER'
    assert commands == []


def test_server_preview_cannot_publish_and_retains_evidence(repo, monkeypatch, tmp_path):
    git, remote, work, _ = repo
    before = git(remote, 'rev-parse', 'main').stdout
    monkeypatch.chdir(work)
    commands = stub_engine(monkeypatch)
    monkeypatch.setenv('STOCK_SHADOW_FORCE_MONITOR', '1')
    monkeypatch.setenv('STOCK_SHADOW_FORCE', '1')
    monkeypatch.setattr(runner, 'persist', lambda *args: pytest.fail('preview attempted publication'))
    result = runner.run('main', True, tmp_path / 'runtime')
    assert result['status'] == 'PREVIEW_COMPLETE' and result['verified'] is False
    assert git(work, 'remote', 'get-url', '--push', 'origin').stdout.strip().startswith('disabled://')
    assert git(remote, 'rev-parse', 'main').stdout == before
    assert (Path(result['preview_outputs']) / 'stock-shadow/portfolio-v1.json').exists()
    assert commands[0][0][1:3] == ['-m', 'pytest']
    assert len(commands) == 4
    for command, kwargs in commands:
        assert '--force' not in command
        assert 'STOCK_SHADOW_FORCE_MONITOR' not in kwargs['env']
        assert 'STOCK_SHADOW_FORCE' not in kwargs['env']
        assert kwargs['env']['STOCK_SHADOW_GENERATION'] == result['generation']


def test_server_publish_requires_credentials_before_engine_execution(repo, monkeypatch, tmp_path):
    git, _, work, writer = repo
    configure_server(git, work, writer)
    monkeypatch.chdir(work)
    monkeypatch.delenv('APCA_API_KEY_ID', raising=False)
    monkeypatch.delenv('APCA_API_SECRET_KEY', raising=False)
    commands = stub_engine(monkeypatch)
    with pytest.raises(RuntimeError, match='STOCK_PROVIDER_CREDENTIALS_NOT_CONFIGURED'):
        runner.run('main', False, tmp_path / 'runtime')
    assert commands == []


def test_server_publish_uses_one_generation_and_reports_verified_result(repo, monkeypatch, tmp_path):
    git, _, work, writer = repo
    configure_server(git, work, writer)
    monkeypatch.chdir(work)
    monkeypatch.setenv('APCA_API_KEY_ID', 'offline-key')
    monkeypatch.setenv('APCA_API_SECRET_KEY', 'offline-secret')
    commands = stub_engine(monkeypatch)
    publications = []

    def persist(mode, a):
        publications.append((mode, a.copy()))
        return {'verified': True, 'commit': 'c' * 40, 'generation': a['generation']}

    monkeypatch.setattr(runner, 'persist', persist)
    result = runner.run('main', False, tmp_path / 'runtime')
    assert result['status'] == 'PUBLISHED_AND_READ_BACK' and result['verified'] is True
    assert len(publications) == 1 and publications[0][0] == 'main'
    assert publications[0][1]['generation'] == result['generation']
    assert all(kwargs['env']['STOCK_SHADOW_GENERATION'] == result['generation'] for _, kwargs in commands)
    record = json.loads(next((tmp_path / 'runtime').glob('run-*.json')).read_text())
    assert record['status'] == result['status'] and record['commit'] == 'c' * 40


def test_server_engine_failure_only_attempts_current_failure_health(repo, monkeypatch, tmp_path):
    git, _, work, writer = repo
    configure_server(git, work, writer)
    monkeypatch.chdir(work)
    monkeypatch.setenv('APCA_API_KEY_ID', 'offline-key')
    monkeypatch.setenv('APCA_API_SECRET_KEY', 'offline-secret')
    stub_engine(monkeypatch, fail=True)
    publications = []

    def persist(mode, a):
        publications.append((mode, a.copy()))
        return {'verified': True, 'mode': mode}

    monkeypatch.setattr(runner, 'persist', persist)
    with pytest.raises(subprocess.CalledProcessError):
        runner.run('main', False, tmp_path / 'runtime')
    assert len(publications) == 1 and publications[0][0] == 'health'
    record = json.loads(next((tmp_path / 'runtime').glob('run-*.json')).read_text())
    assert record['status'] == 'FAILED' and record['verified'] is False
    assert record['failure_health_publication']['mode'] == 'health'


def test_server_uncertain_publication_keeps_candidate_without_retry_or_restore(repo, monkeypatch, tmp_path):
    git, _, work, writer = repo
    configure_server(git, work, writer)
    monkeypatch.chdir(work)
    monkeypatch.setenv('APCA_API_KEY_ID', 'offline-key')
    monkeypatch.setenv('APCA_API_SECRET_KEY', 'offline-secret')
    stub_engine(monkeypatch)
    calls = []

    def uncertain(mode, a):
        calls.append(mode)
        raise publisher.PublicationUnverified('c' * 40, a['generation'], RuntimeError('offline partition'))

    monkeypatch.setattr(runner, 'persist', uncertain)
    with pytest.raises(publisher.PublicationUnverified):
        runner.run('main', False, tmp_path / 'runtime')
    assert calls == ['main']
    record = json.loads(next((tmp_path / 'runtime').glob('run-*.json')).read_text())
    assert record['status'] == 'PUBLICATION_UNVERIFIED'
    assert record['candidate_commit'] == 'c' * 40 and record['verified'] is False


def test_restarted_runner_uses_fresh_authoritative_state_and_rebinds_environment(repo, monkeypatch, tmp_path):
    git, remote, old_work, writer = repo
    configure_server(git, old_work, writer)
    old_source = git(old_work, 'rev-parse', 'HEAD').stdout.strip()
    write_json(old_work, RESULTS + 'portfolio-v1.json', {'fixture': 'unpublished stale scratch'})
    fresh_state = {'positions': {}, 'closed': [], 'simulation_only': True, 'fixture': 'fresh authoritative'}
    publish_other(git, writer, RESULTS + 'portfolio-v1.json', fresh_state)
    fresh_source = git(remote, 'rev-parse', 'main').stdout.strip()
    restarted = tmp_path / 'restarted'
    git(tmp_path, 'clone', '-b', 'main', str(remote), str(restarted))
    monkeypatch.chdir(restarted)
    monkeypatch.setenv('APCA_API_KEY_ID', 'offline-key')
    monkeypatch.setenv('APCA_API_SECRET_KEY', 'offline-secret')
    monkeypatch.setenv('STOCK_SHADOW_SOURCE_COMMIT', old_source)
    monkeypatch.setenv('STOCK_SHADOW_RUN_ID', 'stale-prior-run')
    monkeypatch.setenv('STOCK_SHADOW_GENERATION', '0' * 64)
    commands = stub_engine(monkeypatch)

    def persisted(mode, a):
        assert json.loads(Path(RESULTS + 'portfolio-v1.json').read_text()) == fresh_state
        assert a['source_commit'] == fresh_source != old_source
        return {'verified': True, 'generation': a['generation'], 'commit': 'c' * 40}

    monkeypatch.setattr(runner, 'persist', persisted)
    result = runner.run('main', False, tmp_path / 'runtime')
    assert result['source_commit'] == fresh_source
    assert result['run_id'] != 'stale-prior-run' and result['generation'] != '0' * 64
    assert all(kwargs['env']['STOCK_SHADOW_SOURCE_COMMIT'] == fresh_source for _, kwargs in commands)
    assert json.loads((old_work / (RESULTS + 'portfolio-v1.json')).read_text())['fixture'] == 'unpublished stale scratch'


@pytest.mark.parametrize('path,value', [
    (('acceptance_status',), 'REJECTED_DATA'),
    (('acceptance_status',), None),
    (('errors',), [{'type': 'HTTPError', 'message': 'HTTP Error 403: Forbidden'}]),
    (('errors',), [{'type': 'HTTPError', 'message': 'HTTP Error 429: Too Many Requests'}]),
    (('errors',), None),
    (('data_health',), None),
    (('data_health', 'status'), 'PARTIAL'),
    (('data_health', 'status'), 'ERROR'),
    (('data_health', 'status'), 'WAITING_FOR_SESSION_DATA'),
    (('data_health', 'session_complete'), False),
    (('data_health', 'session_complete'), 1),
    (('data_health', 'missing_symbols'), ['SPY']),
    (('data_health', 'required_symbols'), ['MOVER', 'SPY']),
    (('data_health', 'required_symbols'), []),
    (('data_health', 'required_symbols'), ['MOVER', 'SPY', 'QQQ', 'MOVER']),
    (('data_health', 'regular_session_bar_counts', 'MOVER'), 0),
    (('data_health', 'regular_session_bar_counts', 'SPY'), 0),
    (('data_health', 'regular_session_bar_counts', 'QQQ'), None),
    (('data_health', 'regular_session_bar_counts', 'QQQ'), True),
    (('data_health', 'regular_session_bar_counts', 'QQQ'), 1.5),
    (('results',), [{'symbol': 'MOVER', 'high_at': None, 'lookahead_check': 'PASS'}]),
    (('results',), [{'symbol': 'MOVER', 'high_at': '', 'lookahead_check': 'PASS'}]),
    (('results',), [{'symbol': 'MOVER', 'high_at': '2026-10-06T19:55:00Z', 'lookahead_check': 'NOT_RUN'}]),
    (('results',), None),
    (('big_movers_total',), 214),
    (('future_leakage_detected',), True),
    (('future_mutation_invariance',), False),
    (('lookahead_violations',), [{'symbol': 'MOVER', 'error': 'future mutation changed past decision'}]),
    (('strategy_effect',), True),
    (('mode',), 'LIVE'),
])
def test_replay_publisher_rejects_false_green_reports_without_any_remote_or_ledger_change(repo, path, value):
    git, remote, work, _ = repo
    env = prepare_outputs(work, 'replay')
    report_path = work / (RESULTS + 'replay-v1.json')
    report = json.loads(report_path.read_text())
    cursor = report
    for key in path[:-1]:
        cursor = cursor[key]
    cursor[path[-1]] = value
    report_path.write_text(json.dumps(report) + '\n')
    before_commit = git(remote, 'rev-parse', 'main').stdout
    before_ledger = {name: (work / (RESULTS + name)).read_bytes()
                     for name in ('portfolio-v1.json', 'trades-v1.json')}
    result = raw_persist(work, 'replay', env)
    assert result.returncode != 0, result.stdout + result.stderr
    assert 'REPLAY_' in result.stderr
    assert git(remote, 'rev-parse', 'main').stdout == before_commit
    assert {name: (work / (RESULTS + name)).read_bytes() for name in before_ledger} == before_ledger
    assert git(remote, 'ls-tree', '--name-only', 'main', '--', RESULTS + 'runtime-replay-v1.json').stdout == ''


def test_replay_publisher_accepts_complete_data_and_only_publishes_report_and_receipt(repo):
    git, remote, work, _ = repo
    env = prepare_outputs(work, 'replay')
    before_commit = git(remote, 'rev-parse', 'main').stdout.strip()
    before_ledger = {name: (work / (RESULTS + name)).read_bytes()
                     for name in ('portfolio-v1.json', 'trades-v1.json')}
    result = raw_persist(work, 'replay', env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'STOCK_POST_PUSH_READBACK_OK' in result.stdout
    assert set(git(remote, 'diff', '--name-only', before_commit, 'main').stdout.splitlines()) == {
        RESULTS + 'replay-v1.json', RESULTS + 'runtime-replay-v1.json',
    }
    for name, raw in before_ledger.items():
        assert (work / (RESULTS + name)).read_bytes() == raw
        assert git(remote, 'show', 'main:' + RESULTS + name).stdout.encode() == raw
    receipt = json.loads(git(remote, 'show', 'main:' + RESULTS + 'runtime-replay-v1.json').stdout)
    assert receipt['status'] == 'RUN_COMPLETED'
    assert set(receipt['files']) == {RESULTS + 'replay-v1.json'}


def test_replay_publisher_accepts_genuine_zero_mover_session(repo):
    git, remote, work, _ = repo
    env = prepare_outputs(work, 'replay')
    report = accepted_replay_fixture()
    report['results'] = []
    report['big_movers_total'] = 0
    report['data_health']['required_symbols'] = ['SPY', 'QQQ']
    report['data_health']['regular_session_bar_counts'] = {'SPY': 78, 'QQQ': 78}
    report['data_health']['cohort_status'] = 'NO_QUALIFYING_MOVERS'
    write_json(work, RESULTS + 'replay-v1.json', report)
    result = raw_persist(work, 'replay', env)
    assert result.returncode == 0, result.stdout + result.stderr
    published = json.loads(git(remote, 'show', 'main:' + RESULTS + 'replay-v1.json').stdout)
    assert published['acceptance_status'] == 'ACCEPTED' and published['results'] == []


def test_replay_publisher_rejects_vacuous_success_with_missing_mover_results(repo):
    git, remote, work, _ = repo
    env = prepare_outputs(work, 'replay')
    report = accepted_replay_fixture()
    report['results'] = []
    report['big_movers_total'] = 0
    write_json(work, RESULTS + 'replay-v1.json', report)
    before = git(remote, 'rev-parse', 'main').stdout
    result = raw_persist(work, 'replay', env)
    assert result.returncode != 0, result.stdout + result.stderr
    assert git(remote, 'rev-parse', 'main').stdout == before


def test_replay_runner_provider_failure_never_invokes_publisher(repo, monkeypatch, tmp_path):
    git, remote, work, writer = repo
    configure_server(git, work, writer)
    before = git(remote, 'rev-parse', 'main').stdout
    monkeypatch.chdir(work)
    for key in PROVIDER_KEYS:
        monkeypatch.setenv(key, 'offline-dummy-' + key)
    original_run = subprocess.run

    def fail_replay(command, **kwargs):
        if command[0] != sys.executable:
            return original_run(command, **kwargs)
        if command[1].endswith('stock_replay.py'):
            report = accepted_replay_fixture()
            report.update(acceptance_status='REJECTED_DATA', results=[],
                          errors=[{'type': 'HTTPError', 'message': 'HTTP Error 403: Forbidden'}])
            report['data_health']['status'] = 'ERROR'
            write_json(work, RESULTS + 'replay-v1.json', report)
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(runner.subprocess, 'run', fail_replay)
    monkeypatch.setattr(runner, 'persist', lambda *args: pytest.fail('rejected replay reached publication'))
    with pytest.raises(subprocess.CalledProcessError):
        runner.run('replay', False, tmp_path / 'runtime')
    assert git(remote, 'rev-parse', 'main').stdout == before
    assert json.loads((work / (RESULTS + 'replay-v1.json')).read_text())['results'] == []


def test_sec_budget_path_is_stable_and_preflight_does_not_inherit_it(repo, monkeypatch, tmp_path):
    _, _, work, _ = repo
    monkeypatch.chdir(work)
    monkeypatch.setenv("SEC_TRANSPORT_STATE", "/untrusted/per-run-state.sqlite3")
    commands = stub_engine(monkeypatch)
    runtime_dir = tmp_path / "runtime"
    runner.run("main", True, runtime_dir)
    assert "SEC_TRANSPORT_STATE" not in commands[0][1]["env"]
    assert all(kwargs["env"]["SEC_TRANSPORT_STATE"] == str(runtime_dir / "sec-transport.sqlite3")
               for command, kwargs in commands[1:])
