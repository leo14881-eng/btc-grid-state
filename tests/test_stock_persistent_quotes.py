"""Persistent cache/runner candidate: offline HTTP and temporary files only."""
from datetime import datetime, timedelta
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from research.stock_shadow import quote_window_store as storage
from research.stock_shadow import stock_position_monitor as monitor
from research.stock_shadow.server import runner
from test_stock_position_collection import CURRENT, Provider, clock, collect, synthetic_rows, offline

END = CURRENT - timedelta(minutes=20)
LOOKBACK = timedelta(days=3)
CONFIG = {'endpoint': 'https://data.alpaca.markets/v2/stocks/bars', 'delay_minutes': 20,
          'lookback_seconds': 259200, 'timeframe': '5Min', 'feed': 'sip', 'adjustment': 'all',
          'limit': 10000, 'target_chars': 7000, 'source': 'ALPACA_SIP_5M_DELAYED',
          'selection_version': 2, 'enforce_delay_endpoint': True, 'scope': 'direct'}
ROW = {'AAPL': [{'t': '2026-10-09T16:35:00Z', 'c': 100}]}
PROCESS_FIXTURE = Path(__file__).parent / 'fixtures/stock_quote_process.py'


def enable(monkeypatch, tmp_path):
    path = tmp_path / 'quotes.sqlite3'
    monkeypatch.setenv('STOCK_SHADOW_INCREMENTAL_QUOTES', '1')
    monkeypatch.setenv('STOCK_SHADOW_QUOTE_CACHE_PATH', str(path))
    return path


def read(path, end=END, config=None, symbols=None):
    store = storage.QuoteWindowStore(path, config or CONFIG)
    usage = {}
    lease = store.read(symbols or ['AAPL'], end, LOOKBACK, usage)
    return store, lease, usage


def seed(path, *, end=END, cursors=None):
    store, lease, usage = read(path, end=end)
    store.publish(lease, cursors or {'AAPL': '2026-10-09T16:35:00Z'}, end, LOOKBACK, usage)
    assert usage['publish_status'] == 'PUBLISHED'
    return lease


@pytest.mark.parametrize('flag', [None, '', '0', 'true', 'yes', '01'])
def test_disabled_collection_never_opens_cache(monkeypatch, tmp_path, flag):
    if flag is not None:
        monkeypatch.setenv('STOCK_SHADOW_INCREMENTAL_QUOTES', flag)
    monkeypatch.setenv('STOCK_SHADOW_QUOTE_CACHE_PATH', str(tmp_path / 'must-not-exist.sqlite3'))
    monkeypatch.setattr(storage.sqlite3, 'connect', lambda *a, **k: pytest.fail('disabled cache opened'))
    result, usage, _ = collect(monkeypatch, ROW)
    assert result == collect(monkeypatch, ROW, old=True)[0]
    assert 'disk_cache' not in usage


def test_restart_reuses_only_cursors_and_reloads_revised_price(monkeypatch, tmp_path):
    path = enable(monkeypatch, tmp_path)
    first, usage, _ = collect(monkeypatch, ROW)
    assert usage['disk_cache']['read_status'] == 'EMPTY'
    assert usage['disk_cache']['publish_status'] == 'PUBLISHED'
    changed = {'AAPL': [{'t': '2026-10-09T16:35:00Z', 'c': 107}]}
    second, usage, provider = collect(monkeypatch, changed)
    assert second[0] == collect(monkeypatch, changed, old=True)[0][0]
    assert first[0]['AAPL']['price'] == 100 and second[0]['AAPL']['price'] == 107
    assert provider.calls[0]['start'] == ['2026-10-09T16:35:00Z']
    _, lease, _ = read(path)
    assert lease.cursors == {'AAPL': '2026-10-09T16:35:00Z'}
    with sqlite3.connect(path) as con:
        payload = con.execute('SELECT payload FROM cache').fetchone()[0]
    assert 'price' not in payload and '107' not in payload


@pytest.mark.parametrize('field,value', [
    ('endpoint', 'https://different.invalid/bars'), ('feed', 'iex'), ('adjustment', 'raw'),
    ('delay_minutes', 15), ('timeframe', '1Min'), ('lookback_seconds', 1000),
    ('selection_version', 3), ('scope', 'preview'), ('limit', 999), ('target_chars', 1234),
    ('enforce_delay_endpoint', False),
])
def test_configuration_identity_mismatch_always_cold(tmp_path, field, value):
    path = tmp_path / 'quotes.sqlite3'
    seed(path)
    _, lease, usage = read(path, config={**CONFIG, field: value})
    assert lease.cursors == {} and usage['read_status'] == 'CONFIG_MISMATCH'


@pytest.mark.parametrize('fault', ['sqlite', 'schema', 'payload', 'checksum', 'oversize', 'missing_parent'])
def test_corruption_and_path_failures_fall_back_without_old_prices(monkeypatch, tmp_path, fault):
    path = enable(monkeypatch, tmp_path)
    seed(path)
    if fault == 'sqlite':
        path.write_bytes(b'not a sqlite database')
    elif fault == 'oversize':
        with path.open('ab') as out:
            out.write(b'x' * storage.MAX_DATABASE_BYTES)
    elif fault == 'missing_parent':
        path = tmp_path / 'absent' / 'quotes.sqlite3'
        monkeypatch.setenv('STOCK_SHADOW_QUOTE_CACHE_PATH', str(path))
    else:
        with sqlite3.connect(path) as con:
            if fault == 'schema':
                con.execute('PRAGMA user_version=999')
            elif fault == 'payload':
                con.execute("UPDATE cache SET payload='invalid-json'")
            else:
                con.execute("UPDATE cache SET digest='wrong'")
    result, usage, provider = collect(monkeypatch, {'AAPL': []})
    assert result[0] == {} and provider.calls[0]['start'] == ['2026-10-06T16:40:00Z']
    assert usage['cache_hits'] == 0 and usage['symbol_coverage'] == 'UNKNOWN'
    assert usage['disk_cache']['read_status'] in ('UNAVAILABLE', 'CORRUPT_PAYLOAD')
    if fault in ('payload', 'checksum'):
        assert usage['disk_cache']['publish_status'] == 'PUBLISHED'
        assert read(path)[1].cursors == {}
    if fault == 'schema':
        with sqlite3.connect(path) as con:
            assert con.execute('PRAGMA user_version').fetchone()[0] == 999


@pytest.mark.parametrize('with_table', [True, False])
def test_foreign_database_is_not_reconfigured_or_written(monkeypatch, tmp_path, with_table):
    path = enable(monkeypatch, tmp_path)
    with sqlite3.connect(path) as con:
        if with_table:
            con.execute('CREATE TABLE foreign_data(value TEXT)')
            con.execute("INSERT INTO foreign_data VALUES ('keep')")
        con.execute('PRAGMA user_version=17')
    before = path.read_bytes()
    result, usage, _ = collect(monkeypatch, ROW)
    assert result[0]['AAPL']['price'] == 100
    assert usage['disk_cache']['read_status'] == 'UNAVAILABLE'
    assert path.read_bytes() == before


@pytest.mark.parametrize('bad_path', ['', 'relative.sqlite3', 'inside-checkout.sqlite3'])
def test_missing_relative_or_checkout_path_cannot_create_cache(monkeypatch, bad_path):
    monkeypatch.setenv('STOCK_SHADOW_INCREMENTAL_QUOTES', '1')
    if bad_path == 'inside-checkout.sqlite3':
        bad_path = str(Path.cwd() / bad_path)
    monkeypatch.setenv('STOCK_SHADOW_QUOTE_CACHE_PATH', bad_path)
    monkeypatch.setattr(storage.sqlite3, 'connect', lambda *a, **k: pytest.fail('rejected path opened'))
    result, usage, _ = collect(monkeypatch, ROW)
    assert result[0] == collect(monkeypatch, ROW, old=True)[0][0]
    assert usage['disk_cache']['read_status'] == 'PATH_REJECTED'


def test_unwritable_cache_falls_back_without_changing_quote_result(monkeypatch, tmp_path):
    enable(monkeypatch, tmp_path)
    monkeypatch.setattr(storage.sqlite3, 'connect', lambda *a, **k: (_ for _ in ()).throw(PermissionError('offline fixture')))
    actual, usage, _ = collect(monkeypatch, ROW)
    assert actual == collect(monkeypatch, ROW, old=True)[0]
    assert usage['disk_cache']['read_status'] == 'UNAVAILABLE'
    assert usage['disk_cache']['read_error_type'] == 'PermissionError'


def test_future_snapshot_and_backward_clock_never_make_new_data(monkeypatch, tmp_path):
    path = enable(monkeypatch, tmp_path)
    seed(path)
    clock(monkeypatch, CURRENT - timedelta(hours=1))
    result, usage, _ = collect(monkeypatch, ROW)
    assert result[0] == {} and usage['cache_hits'] == 0
    assert usage['disk_cache']['read_status'] == 'FUTURE_SNAPSHOT'
    assert usage['disk_cache']['publish_status'] == 'CLOCK_REGRESSION'
    assert read(path)[1].cursors == {'AAPL': '2026-10-09T16:35:00Z'}


def test_expired_cache_cannot_cover_holiday_or_supply_a_missing_quote(monkeypatch, tmp_path):
    path = enable(monkeypatch, tmp_path)
    seed(path)
    clock(monkeypatch, CURRENT + timedelta(days=4))
    result, usage, _ = collect(monkeypatch, ROW)
    assert result[0] == {} and usage['disk_cache']['read_status'] == 'EXPIRED'
    assert usage['cache_hits'] == 0
    assert read(path, end=END + timedelta(days=4))[1].cursors == {}


def test_capacity_is_bounded_and_oversized_cohort_uses_full_query(monkeypatch, tmp_path):
    path = enable(monkeypatch, tmp_path)
    symbols = [f'S{i}' for i in range(storage.MAX_SYMBOLS)]
    store, lease, usage = read(path, symbols=symbols)
    cursors = {s: '2026-10-09T16:35:00Z' for s in symbols}
    for _ in range(12):
        store.publish(lease, cursors, END, LOOKBACK, usage)
        assert usage['publish_status'] == 'PUBLISHED'
        _, lease, _ = read(path, symbols=symbols)
    assert path.stat().st_size <= storage.MAX_DATABASE_BYTES
    assert not Path(str(path) + '-wal').exists()
    assert not Path(str(path) + '-journal').exists()
    rows = {f'S{i}': [] for i in range(storage.MAX_SYMBOLS + 1)}
    result, usage, _ = collect(monkeypatch, rows)
    assert result[0] == {} and usage['disk_cache']['read_status'] == 'CAPACITY_BYPASS'
    assert usage['cache_hits'] == 0


def test_failed_pagination_invalidates_snapshot_and_next_process_cold_starts(monkeypatch, tmp_path):
    path = enable(monkeypatch, tmp_path)
    seed(path)
    rows = {'AAPL': ROW['AAPL'] + [{'t': '2026-10-09T16:40:00Z', 'c': 102}]}
    result, usage, _ = collect(monkeypatch, rows, page_size=1, fail_at=2)
    assert result[0] == {} and usage['disk_cache']['publish_status'] == 'PUBLISHED'
    assert read(path)[1].cursors == {}
    result, usage, _ = collect(monkeypatch, rows)
    assert result[0] == collect(monkeypatch, rows, old=True)[0][0]
    assert usage['cache_hits'] == 0


def test_empty_holdings_publishes_empty_snapshot(monkeypatch, tmp_path):
    path = enable(monkeypatch, tmp_path)
    seed(path)
    usage = {}
    assert monitor.alpaca_snapshot_quotes([], usage=usage) == ({}, [], 0, 0)
    assert usage['disk_cache']['publish_status'] == 'PUBLISHED'
    assert read(path)[1].cursors == {}


@pytest.mark.parametrize('fail_at', [None, 1])
def test_corrupt_max_sqlite_revision_falls_back_without_dropping_fresh_quote(monkeypatch, tmp_path, fail_at):
    path = enable(monkeypatch, tmp_path)
    seed(path)
    with sqlite3.connect(path) as con:
        # The existing payload digest remains valid: revision is header metadata.
        con.execute('UPDATE cache SET revision=?', ((1 << 63) - 1,))
    rows = {'AAPL': [{'t': '2026-10-09T16:35:00Z', 'c': 107}]}
    expected = collect(monkeypatch, rows, old=True, fail_at=fail_at)[0]
    actual, usage, provider = collect(monkeypatch, rows, fail_at=fail_at)
    assert actual == expected
    if fail_at is None:
        assert actual[0]['AAPL']['price'] == 107
    else:
        assert actual[0] == {} and actual[1][0]['reason'] == 'HTTP_429'
    assert provider.calls[0]['start'] == ['2026-10-06T16:40:00Z']
    assert usage['disk_cache']['read_status'] == 'UNAVAILABLE'
    assert usage['disk_cache']['publish_status'] == 'NOT_ATTEMPTED'
    with sqlite3.connect(path) as con:
        assert con.execute('SELECT revision FROM cache').fetchone()[0] == (1 << 63) - 1


def test_last_incrementable_revision_publishes_safely_then_next_read_is_cold(monkeypatch, tmp_path):
    path = enable(monkeypatch, tmp_path)
    seed(path)
    with sqlite3.connect(path) as con:
        con.execute('UPDATE cache SET revision=?', (storage.MAX_INCREMENTABLE_REVISION,))
    result, usage, _ = collect(monkeypatch, ROW)
    assert result == collect(monkeypatch, ROW, old=True)[0]
    assert usage['disk_cache']['read_status'] == 'HIT'
    assert usage['disk_cache']['publish_status'] == 'PUBLISHED'
    again, usage, _ = collect(monkeypatch, ROW)
    assert again == result and usage['disk_cache']['read_status'] == 'UNAVAILABLE'
    assert usage['cache_hits'] == 0


def test_publisher_rejects_nonincrementable_lease_before_database_mutation(tmp_path):
    path = tmp_path / 'quotes.sqlite3'
    seed(path)
    store, lease, _ = read(path)
    before = path.read_bytes()
    lease.revision = (1 << 63) - 1
    usage = {}
    store.publish(lease, {}, END, LOOKBACK, usage)
    assert usage['publish_status'] == 'UNAVAILABLE'
    assert usage['publish_error_type'] == 'ValueError'
    assert path.read_bytes() == before


@pytest.mark.parametrize('fail_at', [None, 1])
def test_binding_overflow_is_optional_and_preserves_provider_result(monkeypatch, tmp_path, fail_at):
    path = enable(monkeypatch, tmp_path)
    seed(path)
    original_connect = sqlite3.connect
    class BindingOverflow(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql.startswith('UPDATE cache'):
                raise OverflowError('offline binding overflow')
            return super().execute(sql, *args, **kwargs)
    rows = {'AAPL': ROW['AAPL'] + [{'t': '2026-10-09T16:40:00Z', 'c': 107}]}
    with monkeypatch.context() as scoped:
        scoped.setattr(storage.sqlite3, 'connect', lambda *a, **k: original_connect(*a, **k, factory=BindingOverflow))
        result, usage, _ = collect(scoped, rows, fail_at=fail_at)
        assert result == collect(scoped, rows, old=True, fail_at=fail_at)[0]
        assert usage['disk_cache']['publish_status'] == 'UNAVAILABLE'
        assert usage['disk_cache']['publish_error_type'] == 'OverflowError'
    assert read(path)[1].cursors == {'AAPL': '2026-10-09T16:35:00Z'}


def test_corrupt_revision_cannot_abort_monitor_or_erase_existing_ledger(monkeypatch, tmp_path):
    path = enable(monkeypatch, tmp_path)
    seed(path)
    with sqlite3.connect(path) as con:
        con.execute('UPDATE cache SET revision=?', ((1 << 63) - 1,))
    rows_path = tmp_path / 'rows.json'
    rows_path.write_text(json.dumps({'AAPL': [{'t': '2026-10-09T16:35:00Z', 'c': 107}]}))
    state_root = tmp_path / 'monitor-state'
    state_root.mkdir()
    tranche = {'at': '2026-10-08T15:00:00Z', 'price': 100, 'notional': 1000, 'reason': 'FIXTURE'}
    position = {'symbol': 'AAPL', 'opened_at': tranche['at'], 'tranches': [tranche],
                'mfe_net_pct': 0, 'mae_net_pct': -1}
    events = [{'type': 'BUY', 'symbol': 'AAPL', **tranche}]
    (state_root / 'portfolio-v1.json').write_text(json.dumps({'positions': {'AAPL': position}, 'closed': []}))
    (state_root / 'trades-v1.json').write_text(json.dumps(events))
    completed = subprocess.run([sys.executable, str(PROCESS_FIXTURE), '--rows', str(rows_path),
                                '--now', CURRENT.isoformat(), '--state-root', str(state_root)],
                               capture_output=True, text=True, check=True, timeout=30)
    health = json.loads(completed.stdout)
    state = json.loads((state_root / 'portfolio-v1.json').read_text())
    assert health['status'] == 'OK' and health['positions_updated'] == 1
    assert state['positions']['AAPL']['last_price'] == 107
    assert state['positions']['AAPL']['tranches'] == [tranche] and state['closed'] == []
    assert json.loads((state_root / 'trades-v1.json').read_text()) == events
    assert health['quote_collection']['disk_cache']['read_status'] == 'UNAVAILABLE'


@pytest.mark.parametrize('stage', ['UPDATE cache', 'COMMIT'])
def test_simulated_disk_full_rolls_back_without_losing_fresh_observations(monkeypatch, tmp_path, stage):
    path = enable(monkeypatch, tmp_path)
    seed(path)
    original_connect = sqlite3.connect
    class DiskFull(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql.startswith(stage):
                raise sqlite3.OperationalError('database or disk is full')
            return super().execute(sql, *args, **kwargs)
    with monkeypatch.context() as scoped:
        scoped.setattr(storage.sqlite3, 'connect', lambda *a, **k: original_connect(*a, **k, factory=DiskFull))
        rows = {'AAPL': ROW['AAPL'] + [{'t': '2026-10-09T16:40:00Z', 'c': 102}]}
        actual, usage, _ = collect(scoped, rows)
        assert actual[0] == collect(scoped, rows, old=True)[0][0]
        assert usage['disk_cache']['publish_status'] == 'UNAVAILABLE'
    assert read(path)[1].cursors == {'AAPL': '2026-10-09T16:35:00Z'}


def test_busy_database_does_not_block_quote_collection_or_forge_success(monkeypatch, tmp_path):
    path = enable(monkeypatch, tmp_path)
    seed(path)
    with sqlite3.connect(path, isolation_level=None) as locked:
        locked.execute('BEGIN IMMEDIATE')
        result, usage, _ = collect(monkeypatch, ROW)
        assert result[0] == collect(monkeypatch, ROW, old=True)[0][0]
        assert usage['disk_cache']['publish_status'] in ('UNAVAILABLE', 'NOT_ATTEMPTED')
        locked.execute('ROLLBACK')


def conflict_worker(path, barrier, newer_done, output, newer):
    store, lease, usage = read(path)
    barrier.wait(timeout=15)
    if not newer:
        assert newer_done.wait(timeout=15)
    end = END + (timedelta(minutes=5) if newer else timedelta())
    store.publish(lease, {'AAPL': '2026-10-09T16:40:00Z' if newer else '2026-10-09T16:35:00Z'}, end, LOOKBACK, usage)
    if newer:
        newer_done.set()
    output.put(usage['publish_status'])


def crash_worker(path):
    con = storage.QuoteWindowStore(path, CONFIG)._connect()
    con.execute('BEGIN IMMEDIATE')
    con.execute("UPDATE cache SET payload='uncommitted' WHERE id=1")
    os._exit(0)


def test_competing_processes_cannot_overwrite_a_newer_snapshot(tmp_path):
    path = tmp_path / 'quotes.sqlite3'
    seed(path)
    ctx = multiprocessing.get_context('spawn')
    barrier, event, output = ctx.Barrier(2), ctx.Event(), ctx.Queue()
    children = [ctx.Process(target=conflict_worker, args=(str(path), barrier, event, output, newer))
                for newer in (False, True)]
    for child in children:
        child.start()
    for child in children:
        child.join(20)
        assert child.exitcode == 0
    assert sorted(output.get(timeout=5) for _ in children) == ['CONFLICT', 'PUBLISHED']
    assert read(path, end=END + timedelta(minutes=5))[1].cursors == {'AAPL': '2026-10-09T16:40:00Z'}


def test_process_crash_rolls_back_uncommitted_cache(tmp_path):
    path = tmp_path / 'quotes.sqlite3'
    seed(path)
    child = multiprocessing.get_context('spawn').Process(target=crash_worker, args=(str(path),))
    child.start()
    child.join(15)
    assert child.exitcode == 0
    assert read(path)[1].cursors == {'AAPL': '2026-10-09T16:35:00Z'}


def test_recreated_file_rejects_an_old_lease(tmp_path):
    path = tmp_path / 'quotes.sqlite3'
    seed(path)
    store, old, _ = read(path)
    path.unlink()
    seed(path)
    usage = {}
    store.publish(old, {}, END, LOOKBACK, usage)
    assert usage['publish_status'] == 'CONFLICT'
    assert read(path)[1].cursors


def test_536_symbols_multi_cycle_corrections_missing_data_and_recovery(monkeypatch, tmp_path, capsys):
    path = enable(monkeypatch, tmp_path)
    rows = synthetic_rows()
    results = []
    for cycle in range(4):
        checked = CURRENT + timedelta(minutes=5 * (cycle - 1))
        clock(monkeypatch, checked)
        if cycle == 1:
            # Same-time correction remains latest for this symbol.
            rows['S0000'] = [r for r in rows['S0000'] if r['t'] != '2026-10-09T16:40:00Z']
            next(r for r in rows['S0000'] if r['t'] == '2026-10-09T16:35:00Z')['c'] = 108
        if cycle == 2:
            # Latest bar withdrawn and one holding absent: bounded backfill.
            rows['S0001'] = [r for r in rows['S0001'] if r['t'] < '2026-10-09T16:25:00Z']
            rows['S0002'] = []
        if cycle == 3:
            rows['S0002'] = [{'t': '2026-10-09T16:50:00Z', 'c': 111}]
        expected, _, legacy_provider = collect(monkeypatch, rows, old=True)
        actual, usage, provider = collect(monkeypatch, rows)
        assert actual[0:2] == expected[0:2]
        assert len(rows) == 536
        assert usage['disk_cache']['publish_status'] == 'PUBLISHED'
        results.append((len(legacy_provider.calls), len(provider.calls), usage['fallback_batches']))
    assert results[0][:2] == (13, 13)
    assert results[1][:2] == (13, 1)
    assert results[2][2] == 1
    assert read(path, end=END + timedelta(minutes=10), symbols=['S0002'])[1].cursors == {'S0002': '2026-10-09T16:50:00Z'}
    with capsys.disabled():
        print(json.dumps({'kind': 'PERSISTENT_SYNTHETIC_OFFLINE_NOT_LIVE', 'positions': 536,
                          'cycles_original_incremental_fallback': results}))


def test_two_isolated_python_processes_replay_536_symbols_equally(monkeypatch, tmp_path):
    enable(monkeypatch, tmp_path)
    rows = synthetic_rows()
    rows_path = tmp_path / 'rows.json'
    rows_path.write_text(json.dumps(rows))
    outputs = []
    for checked in (CURRENT - timedelta(minutes=5), CURRENT):
        completed = subprocess.run([sys.executable, str(PROCESS_FIXTURE), '--rows', str(rows_path),
                                    '--now', checked.isoformat()], capture_output=True, text=True, check=True, timeout=45)
        actual = json.loads(completed.stdout)
        clock(monkeypatch, checked)
        expected, _, _ = collect(monkeypatch, rows, old=True)
        assert actual['result'][0:2] == list(expected[0:2])
        assert len(actual['result'][0]) == 536
        outputs.append(actual['usage'])
    assert outputs[0]['http_requests'] == 13 and outputs[1]['http_requests'] == 1
    assert outputs[1]['disk_cache']['read_status'] == 'HIT'


@pytest.mark.parametrize('job', ['main', 'monitor', 'replay'])
@pytest.mark.parametrize('preview', [False, True])
def test_runner_binds_only_opted_in_monitor_and_strips_preflight_state(tmp_path, job, preview):
    env = {'STOCK_SHADOW_INCREMENTAL_QUOTES': '1', 'STOCK_SHADOW_QUOTE_CACHE_PATH': '/wrong.sqlite3',
           'STOCK_SHADOW_QUOTE_CACHE_SCOPE': 'wrong', 'HARMLESS': 'kept'}
    result = runner.quote_cache_environment(env, job, preview, tmp_path)
    assert result['HARMLESS'] == 'kept'
    assert not any(k in runner.test_environment(result) for k in storage.ENV_KEYS)
    if job == 'monitor':
        scope = 'preview' if preview else 'publish'
        assert result['STOCK_SHADOW_QUOTE_CACHE_PATH'] == str(tmp_path / f'stock-quotes-{scope}-v1.sqlite3')
        assert result['STOCK_SHADOW_QUOTE_CACHE_SCOPE'] == scope
    else:
        assert not any(k in result for k in storage.ENV_KEYS)
    assert not any(k in runner.quote_cache_environment({}, job, preview, tmp_path) for k in storage.ENV_KEYS)


def test_actual_runner_handoff_restarts_monitor_process_with_isolated_cache(monkeypatch, tmp_path):
    # Exercise runner.run and its real child environment; intercept Git/publication
    # and replace provider I/O with the subprocess fixture. All files are temporary.
    runtime_dir = tmp_path / 'runtime'
    rows_path = tmp_path / 'rows.json'
    rows_path.write_text(json.dumps(synthetic_rows()))
    sentinel = tmp_path / 'untrusted-cache.sqlite3'
    sentinel.write_text('untouched')
    monkeypatch.setenv('STOCK_SHADOW_INCREMENTAL_QUOTES', '1')
    monkeypatch.setenv('STOCK_SHADOW_QUOTE_CACHE_PATH', str(sentinel))
    monkeypatch.setattr(runner, 'changed_paths', lambda: [])
    monkeypatch.setattr(runner, 'canonical_inventory', lambda: {})
    monkeypatch.setattr(runner, 'git', lambda *a, **k: subprocess.CompletedProcess(a, 0, 'f' * 40, ''))
    monkeypatch.setattr(runner, 'admission', lambda owner, job, run_id, source: {
        'admitted': True, 'owner': owner, 'job': job, 'run_id': run_id, 'source_commit': source,
        'generation': run_id, 'epoch': 1, 'attempt': 1})
    monkeypatch.setattr(runner, 'snapshot', lambda *a: {})
    monkeypatch.setattr(runner, 'validate_outputs', lambda *a: {})
    monkeypatch.setattr(runner, 'persist', lambda *a: pytest.fail('offline preview attempted publication'))
    monkeypatch.setattr(runner.shutil, 'copytree', lambda *a, **k: None)
    real_run = subprocess.run
    clocks = iter([CURRENT - timedelta(minutes=5), CURRENT])
    reports = []

    def child(command, **kwargs):
        if command[1:3] == ['-m', 'pytest']:
            assert not any(key in kwargs['env'] for key in storage.ENV_KEYS)
            return subprocess.CompletedProcess(command, 0)
        if command[1].endswith('stock_position_monitor.py'):
            checked = next(clocks)
            prefix = [sys.executable, str(PROCESS_FIXTURE), '--rows', str(rows_path), '--now', checked.isoformat()]
            actual = real_run(prefix + ['--state-root', str(tmp_path / 'candidate-state')], env=kwargs['env'],
                              capture_output=True, text=True, check=True, timeout=45)
            baseline_env = {k: v for k, v in kwargs['env'].items() if k not in storage.ENV_KEYS}
            baseline = real_run(prefix + ['--state-root', str(tmp_path / 'baseline-state')], env=baseline_env,
                                capture_output=True, text=True, check=True, timeout=45)
            reports.append((json.loads(actual.stdout), json.loads(baseline.stdout)))
            for filename in ('portfolio-v1.json', 'trades-v1.json', 'summary-v1.json'):
                assert json.loads((tmp_path / 'candidate-state' / filename).read_text()) == \
                    json.loads((tmp_path / 'baseline-state' / filename).read_text())
            return actual
        assert command[1].endswith('stock_review.py')
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(runner.subprocess, 'run', child)
    for _ in range(2):
        assert runner.run('monitor', True, runtime_dir)['status'] == 'PREVIEW_COMPLETE'
    assert [r[0]['requests'] for r in reports] == [13, 1]
    assert [r[1]['requests'] for r in reports] == [13, 13]
    assert all(actual['positions_updated'] == baseline['positions_updated'] == 536 for actual, baseline in reports)
    assert (runtime_dir / 'stock-quotes-preview-v1.sqlite3').exists()
    assert not (runtime_dir / 'stock-quotes-publish-v1.sqlite3').exists()
    assert sentinel.read_text() == 'untouched'


def test_enabled_path_rejects_provider_bar_after_delay_endpoint(monkeypatch, tmp_path):
    path = enable(monkeypatch, tmp_path)
    # Enforce the same 20-minute endpoint if a provider violates its request window.
    # Disabled collection stays unchanged; enabled collection fails closed.
    from test_stock_position_collection import Response, legacy
    payload = json.dumps({'bars': {'AAPL': [{'t': '2026-10-09T16:50:00Z', 'c': 100}]}}).encode()
    monkeypatch.setattr(monitor.urllib.request, 'urlopen', lambda *a, **k: Response(payload))
    expected = legacy.alpaca_snapshot_quotes(['AAPL'])
    actual = monitor.alpaca_snapshot_quotes(['AAPL'])
    assert actual[0] == {} and actual[1][0]['detail'] == 'BAR_AFTER_DELAY_ENDPOINT'
    assert read(path)[1].cursors == {}
    monkeypatch.setenv('STOCK_SHADOW_INCREMENTAL_QUOTES', '0')
    assert monitor.alpaca_snapshot_quotes(['AAPL']) == expected
