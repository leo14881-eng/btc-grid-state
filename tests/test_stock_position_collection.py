"""Offline differential tests. All HTTP is intercepted; no provider keys/network."""
import copy
import importlib.util
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone, time as clock_time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

import pytest
from research.stock_shadow import stock_position_monitor as monitor

spec = importlib.util.spec_from_file_location(
    'legacy_stock_collector', Path(__file__).parent / 'fixtures/stock_monitor_legacy.py')
legacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(legacy)
CURRENT = datetime.fromisoformat('2026-10-09T17:00:00+00:00')


def stamp(value):
    return value.isoformat().replace('+00:00', 'Z')


def clock(monkeypatch, current=CURRENT):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return current.astimezone(tz or timezone.utc)
    monkeypatch.setattr(monitor, 'datetime', Clock)
    monkeypatch.setattr(legacy, 'datetime', Clock)
    monkeypatch.setattr(monitor, 'now', lambda: current.isoformat())


class Response:
    def __init__(self, payload):
        self.payload = payload
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def read(self):
        return self.payload


class Provider:
    """Deterministic symbol-then-time pagination with an inclusive query window."""
    def __init__(self, rows, page_size=10000, fail_at=None):
        self.rows = rows
        self.page_size = page_size
        self.fail_at = fail_at
        self.calls = []
        self.byte_count = 0
        self.bar_count = 0

    def __call__(self, request, **kwargs):
        query = parse_qs(urlparse(request.full_url).query)
        self.calls.append(query)
        if len(self.calls) == self.fail_at:
            raise HTTPError(request.full_url, 429, 'fixture rate limit', {}, None)
        start = datetime.fromisoformat(query['start'][0].replace('Z', '+00:00'))
        end = datetime.fromisoformat(query['end'][0].replace('Z', '+00:00'))
        assert end - start <= timedelta(days=3)
        assert query['timeframe'] == ['5Min']
        assert query['feed'] == ['sip'] and query['adjustment'] == ['all']
        rows = [(s, r) for s in sorted(query['symbols'][0].split(','))
                for r in sorted(self.rows.get(s, []), key=lambda r: r['t'])
                if start <= datetime.fromisoformat(r['t'].replace('Z', '+00:00')) <= end]
        offset = int(query.get('page_token', ['0'])[0])
        chunk = rows[offset:offset + self.page_size]
        bars = {}
        for symbol, row in chunk:
            bars.setdefault(symbol, []).append(row)
        next_offset = offset + self.page_size
        body = {'bars': bars, 'next_page_token': str(next_offset) if next_offset < len(rows) else None}
        payload = json.dumps(body).encode()
        self.byte_count += len(payload)
        self.bar_count += len(chunk)
        return Response(payload)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    for key in ('STOCK_SHADOW_INCREMENTAL_QUOTES', 'STOCK_SHADOW_QUOTE_CACHE_PATH', 'STOCK_SHADOW_QUOTE_CACHE_SCOPE'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('APCA_API_KEY_ID', 'offline-fixture')
    monkeypatch.setenv('APCA_API_SECRET_KEY', 'offline-fixture')
    monkeypatch.setattr(monitor.urllib.request, 'urlopen',
                        lambda *args, **kwargs: pytest.fail('unmocked HTTP'))
    clock(monkeypatch)


def collect(monkeypatch, rows, *, cache=None, old=False, page_size=10000, fail_at=None, symbols=None):
    provider = Provider(rows, page_size, fail_at)
    monkeypatch.setattr(monitor.urllib.request, 'urlopen', provider)
    symbols = symbols or [s.replace('.', '-') for s in rows]
    usage = {}
    result = (legacy.alpaca_snapshot_quotes(symbols) if old else
              monitor.alpaca_snapshot_quotes(symbols, window_cache=cache, usage=usage))
    return result, usage, provider


def warm(cursors=None):
    cache = monitor.QuoteWindowCache()
    cache.cursors.update(cursors or {'AAPL': '2026-10-09T16:35:00Z'})
    return cache


@pytest.mark.parametrize('fail_at', [None, 1, 2, 3])
def test_default_collector_matches_frozen_original_and_counts_http(monkeypatch, fail_at):
    rows = {'AAPL': [{'t': '2026-10-09T16:30:00Z', 'c': 101},
                     {'t': '2026-10-09T16:35:00Z', 'c': 102}],
            'BRK.B': [{'t': '2026-10-09T16:35:00Z', 'c': 200}], 'MISSING': []}
    expected, _, _ = collect(monkeypatch, rows, old=True, page_size=1, fail_at=fail_at)
    actual, usage, provider = collect(monkeypatch, rows, page_size=1, fail_at=fail_at)
    assert actual == expected
    assert usage['http_requests'] == len(provider.calls)
    assert usage['pages'] == len(provider.calls) - bool(actual[1])
    assert usage['response_bytes'] == provider.byte_count
    assert usage['bars'] == provider.bar_count
    assert usage['http_seconds'] >= 0 and usage['elapsed_seconds'] >= usage['http_seconds']
    if fail_at:
        assert actual[0] == {}  # No partial batch becomes a usable observation.


def test_inclusive_overlap_gets_revision_and_late_insert_not_cached_price(monkeypatch):
    cache = warm()
    rows = {'AAPL': [{'t': '2026-10-09T16:30:00Z', 'c': 90},
                     {'t': '2026-10-09T16:35:00Z', 'c': 105}]}
    result, usage, provider = collect(monkeypatch, rows, cache=cache, page_size=1)
    expected, _, _ = collect(monkeypatch, rows, old=True, page_size=1)
    assert result[0] == expected[0]
    assert result[0]['AAPL']['price'] == 105
    assert provider.calls[0]['start'] == ['2026-10-09T16:35:00Z']
    rows['AAPL'].append({'t': '2026-10-09T16:40:00Z', 'c': 106})
    result, usage, _ = collect(monkeypatch, rows, cache=cache, page_size=1)
    assert result[0] == collect(monkeypatch, rows, old=True, page_size=1)[0][0]
    assert result[0]['AAPL']['price'] == 106 and usage['pages'] == 2
    assert usage['cache_hits'] == 1 and usage['fallback_batches'] == 0


@pytest.mark.parametrize('cursor', ['broken', None, 123, {}, '2026-10-09T16:35:00',
                                  '2026-10-09T18:00:00Z', '2026-09-01T16:35:00Z'])
def test_corrupt_future_naive_or_expired_cursor_is_cold(monkeypatch, cursor):
    rows = {'AAPL': [{'t': '2026-10-09T16:35:00Z', 'c': 100}]}
    actual, usage, provider = collect(monkeypatch, rows, cache=warm({'AAPL': cursor}))
    assert actual == collect(monkeypatch, rows, old=True)[0]
    assert usage['cache_hits'] == 0
    assert provider.calls[0]['start'] == ['2026-10-06T16:40:00Z']


@pytest.mark.parametrize('rows', [
    {'AAPL': []},
    {'AAPL': [{'t': '2026-10-09T16:20:00Z', 'c': 98}]},
    {'AAPL': [{'t': '2026-10-09T14:00:00Z', 'c': 98}]},
])
def test_deleted_anchor_or_missing_symbol_backfills_once_never_fabricates(monkeypatch, rows):
    cache = warm()
    actual, usage, provider = collect(monkeypatch, rows, cache=cache)
    expected = collect(monkeypatch, rows, old=True)[0]
    assert actual[0:2] == expected[0:2]
    assert usage['fallback_batches'] == 1 and len(provider.calls) == 2
    assert provider.calls[1]['start'] == ['2026-10-06T16:40:00Z']
    if not rows['AAPL']:
        assert usage['symbol_coverage'] == 'UNKNOWN' and cache.cursors == {}
    if rows['AAPL'] and rows['AAPL'][0]['t'].endswith('14:00:00Z'):
        assert monitor.quote_validity('AAPL', actual[0]['AAPL'], CURRENT, '5Min') == 'STALE_BAR'
        assert cache.cursors == {}


def test_failed_backfill_discards_narrowed_batch_and_next_call_recovers(monkeypatch):
    cache = warm({'AAPL': '2026-10-09T16:35:00Z', 'MISS': '2026-10-09T16:35:00Z'})
    rows = {'AAPL': [{'t': '2026-10-09T16:35:00Z', 'c': 100}], 'MISS': []}
    failed, usage, _ = collect(monkeypatch, rows, cache=cache, fail_at=2)
    assert failed[0] == {} and failed[1][0]['reason'] == 'HTTP_429'
    assert cache.cursors == {} and usage['symbol_coverage'] == 'UNKNOWN'
    recovered, usage, _ = collect(monkeypatch, rows, cache=cache)
    assert recovered == collect(monkeypatch, rows, old=True)[0]
    assert usage['cache_hits'] == 0 and usage['symbol_coverage'] == 'PARTIAL'


def test_new_position_and_restart_use_full_window_and_closed_symbols_are_removed(monkeypatch):
    rows = {'AAPL': [{'t': '2026-10-09T16:35:00Z', 'c': 100}],
            'NEW': [{'t': '2026-10-09T16:40:00Z', 'c': 200}]}
    cache = warm()
    result, usage, provider = collect(monkeypatch, rows, cache=cache)
    assert usage['cache_hits'] == 1 and result[0:2] == collect(monkeypatch, rows, old=True)[0][0:2]
    assert len(provider.calls) == 2
    assert provider.calls[1]['symbols'] == ['NEW']
    assert provider.calls[1]['start'] == ['2026-10-06T16:40:00Z']
    assert set(cache.cursors) == {'AAPL', 'NEW'}
    collect(monkeypatch, rows, cache=cache, symbols=['NEW'])
    assert set(cache.cursors) == {'NEW'}
    _, usage, _ = collect(monkeypatch, rows, cache=monitor.QuoteWindowCache())
    assert usage['cache_hits'] == 0


@pytest.mark.parametrize('current,prior', [
    ('2026-10-12T13:40:00+00:00', '2026-10-09T19:55:00Z'),  # Weekend, no fresh session bar.
    ('2026-09-08T13:40:00+00:00', '2026-09-04T19:55:00Z'),  # Long holiday, beyond 3 days.
    ('2026-03-09T14:10:00+00:00', '2026-03-06T20:55:00Z'),  # Spring DST.
    ('2026-11-02T15:10:00+00:00', '2026-10-30T19:55:00Z'),  # Autumn DST.
    ('2026-11-27T18:40:00+00:00', '2026-11-27T17:55:00Z'),  # Early close fixture.
])
def test_gap_windows_preserve_three_day_bound_delay_and_stale_classification(monkeypatch, current, prior):
    current = datetime.fromisoformat(current)
    clock(monkeypatch, current)
    rows = {'AAPL': [{'t': prior, 'c': 100}]}
    actual, _, provider = collect(monkeypatch, rows, cache=warm({'AAPL': prior}))
    expected = collect(monkeypatch, rows, old=True)[0]
    assert actual[0] == expected[0]
    assert monitor.quote_validity('AAPL', actual[0].get('AAPL'), current, '5Min') == \
        monitor.quote_validity('AAPL', expected[0].get('AAPL'), current, '5Min')
    assert provider.calls[0]['end'] == [stamp(current - timedelta(minutes=20))]


def test_missing_credentials_and_empty_symbols_do_not_use_cache(monkeypatch):
    cache = warm()
    usage = {}
    monkeypatch.delenv('APCA_API_KEY_ID')
    assert monitor.alpaca_snapshot_quotes(['AAPL'], window_cache=cache, usage=usage)[0] == {}
    assert usage['http_requests'] == 0 and cache.cursors == {}
    assert monitor.alpaca_snapshot_quotes([], window_cache=cache, usage=usage) == ({}, [], 0, 0)
    assert usage['symbol_coverage'] == 'EMPTY'


@pytest.mark.parametrize('payload', [b'not json', b'null', b'{"bars":{"AAPL":17}}'])
def test_bad_response_matches_legacy_failure_and_counts_attempt_once(monkeypatch, payload):
    monkeypatch.setattr(monitor.urllib.request, 'urlopen', lambda *a, **k: Response(payload))
    expected = legacy.alpaca_snapshot_quotes(['AAPL'])
    usage = {}
    cache = warm()
    actual = monitor.alpaca_snapshot_quotes(['AAPL'], window_cache=cache, usage=usage)
    assert actual == expected and actual[0] == {}
    assert usage['http_requests'] == 1 and usage['response_bytes'] == len(payload)
    assert cache.cursors == {} and usage['interval_coverage'] == 'UNKNOWN'


def test_failed_incremental_page_discards_partial_rows_then_retries_cold(monkeypatch):
    rows = {'AAPL': [{'t': '2026-10-09T16:35:00Z', 'c': 100},
                     {'t': '2026-10-09T16:40:00Z', 'c': 104}]}
    cache = warm()
    actual, usage, _ = collect(monkeypatch, rows, cache=cache, page_size=1, fail_at=2)
    assert actual[0] == {} and usage['http_requests'] == 2
    assert usage['pages'] == 1 and cache.cursors == {}
    recovered, usage, _ = collect(monkeypatch, rows, cache=cache, page_size=1)
    assert recovered == collect(monkeypatch, rows, old=True, page_size=1)[0]
    assert usage['cache_hits'] == 0


def test_multiple_logical_batches_keep_other_successes_after_failure(monkeypatch):
    monkeypatch.setattr(monitor, 'MAX_REQUEST_TARGET_CHARS', 1)
    monkeypatch.setattr(legacy, 'MAX_REQUEST_TARGET_CHARS', 1)
    rows = {'AAPL': [{'t': '2026-10-09T16:35:00Z', 'c': 100}],
            'MSFT': [{'t': '2026-10-09T16:35:00Z', 'c': 200}]}
    actual, usage, _ = collect(monkeypatch, rows, cache=warm(), fail_at=2)
    expected = collect(monkeypatch, rows, old=True, fail_at=2)[0]
    assert actual == expected and actual[3] == 2
    assert set(actual[0]) == {'AAPL'} and usage['symbol_coverage'] == 'PARTIAL'


def test_missing_symbol_does_not_force_warm_cohort_to_full_window(monkeypatch):
    rows = synthetic_rows()
    rows['MISSING'] = []
    cache = monitor.QuoteWindowCache()
    clock(monkeypatch, CURRENT - timedelta(minutes=5))
    collect(monkeypatch, rows, cache=cache)
    clock(monkeypatch)
    actual, usage, provider = collect(monkeypatch, rows, cache=cache)
    expected = collect(monkeypatch, rows, old=True)[0]
    assert actual[0:2] == expected[0:2] and 'MISSING' not in actual[0]
    assert len(provider.calls) == 2 and usage['cache_hits'] == 536
    assert usage['symbol_coverage'] == 'PARTIAL' and usage['interval_coverage'] == 'UNKNOWN'
    assert provider.calls[-1]['symbols'] == ['MISSING']


@pytest.mark.parametrize('close', [float('nan'), float('inf'), None, False, -1])
def test_invalid_price_is_not_replaced_by_an_older_valid_bar_or_cached(monkeypatch, close):
    rows = {'AAPL': [{'t': '2026-10-09T16:30:00Z', 'c': 100},
                     {'t': '2026-10-09T16:35:00Z', 'c': close}]}
    cache = warm()
    actual, _, _ = collect(monkeypatch, rows, cache=cache)
    assert actual[0]['AAPL']['price_asof'] == '2026-10-09T16:35:00Z'
    assert monitor.quote_validity('AAPL', actual[0]['AAPL'], CURRENT, '5Min') == 'INVALID_PRICE'
    assert cache.cursors == {}


@pytest.mark.parametrize('shared', [True, False])
@pytest.mark.parametrize('explicit_usage', [True, False])
def test_concurrent_callers_have_isolated_metrics_and_safe_cache_updates(monkeypatch, shared, explicit_usage):
    rows = {'AAPL': [{'t': '2026-10-09T16:35:00Z', 'c': 100}]}
    provider = Provider(rows)
    guard = threading.Lock()
    active = 0
    peak = 0
    barrier = threading.Barrier(2)
    def get(*args, **kwargs):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        if shared:
            time.sleep(0.02)
        else:
            barrier.wait(timeout=5)
        try:
            return provider(*args, **kwargs)
        finally:
            with guard:
                active -= 1
    monkeypatch.setattr(monitor.urllib.request, 'urlopen', get)
    cache = warm()
    def run(index):
        usage = {}
        result = monitor.alpaca_snapshot_quotes(['AAPL'], window_cache=cache if shared else warm(),
                                               usage=usage if explicit_usage else None)
        return result, usage if explicit_usage else copy.deepcopy(monitor.QUOTE_USAGE)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(run, range(2)))
    assert results[0][0] == results[1][0]
    assert peak == (1 if shared else 2)
    for result, usage in results:
        assert result[1] == [] and usage['http_requests'] == usage['pages'] == 1
        assert usage['bars'] == 1 and usage['cache_hits'] == 1


def synthetic_rows():
    # A reproducible workload, not a capture or an estimate of production savings.
    symbols = [f'S{i:04d}' for i in range(536)]
    times = []
    for day in range(6, 10):
        start = datetime(2026, 10, day, 13, 30, tzinfo=timezone.utc)
        times.extend(start + timedelta(minutes=5 * i) for i in range(78))
    return {s: [{'t': stamp(t), 'c': 100 + i / 1000} for i, t in enumerate(times)] for s in symbols}


def test_536_position_replay_measures_actual_fixture_requests_and_bytes(monkeypatch, capsys):
    rows = synthetic_rows()
    cache = monitor.QuoteWindowCache()
    clock(monkeypatch, CURRENT - timedelta(minutes=5))
    cold, cold_usage, cold_provider = collect(monkeypatch, rows, cache=cache)
    clock(monkeypatch)
    baseline, _, baseline_provider = collect(monkeypatch, rows, old=True)
    actual, usage, provider = collect(monkeypatch, rows, cache=cache)
    assert len(actual[0]) == 536 and actual[0:2] == baseline[0:2]
    assert cold_usage['cache_hits'] == 0 and usage['cache_hits'] == 536
    assert baseline[3] == actual[3] == 1
    assert len(provider.calls) == 1 < len(baseline_provider.calls)
    assert provider.bar_count == 1072 and provider.byte_count < baseline_provider.byte_count
    print(json.dumps({'kind': 'SYNTHETIC_OFFLINE_NOT_LIVE', 'positions': len(rows),
                      'cold_requests': len(cold_provider.calls),
                      'baseline_requests': len(baseline_provider.calls),
                      'incremental_requests': len(provider.calls),
                      'baseline_bars': baseline_provider.bar_count,
                      'incremental_bars': provider.bar_count,
                      'baseline_bytes': baseline_provider.byte_count,
                      'incremental_bytes': provider.byte_count,
                      'equal_symbol_outputs': len(actual[0])}))
    with capsys.disabled():
        print(capsys.readouterr().out.strip())


def test_lifecycle_output_and_exit_decision_equal_original(monkeypatch):
    rows = {'AAPL': [{'t': '2026-10-09T16:35:00Z', 'c': 104}],
            'HOLD': [{'t': '2026-10-09T16:35:00Z', 'c': 120}],
            'STALE': [{'t': '2026-10-08T16:35:00Z', 'c': 104}], 'MISS': []}
    position = {'opened_at': '2026-10-08T15:00:00Z', 'tranches': [
        {'at': '2026-10-08T15:00:00Z', 'price': 100, 'notional': 1000, 'reason': 'TEST'}],
        'mfe_net_pct': 10, 'mae_net_pct': -1}
    original = {monitor.STATE: {'positions': {s: dict(copy.deepcopy(position), symbol=s) for s in rows}, 'closed': []},
                monitor.EVENTS: []}
    monkeypatch.setattr(monitor, '_alpaca_exchange_session', lambda ts=None:
                        {'date': '2026-10-09', 'open': clock_time(9, 30), 'close': clock_time(16)})
    collector = monitor.alpaca_snapshot_quotes
    outputs = []
    for old in (True, False):
        store = copy.deepcopy(original)
        provider = Provider(rows, page_size=1)
        monkeypatch.setattr(monitor.urllib.request, 'urlopen', provider)
        cache = warm({s: '2026-10-09T16:35:00Z' for s in rows})
        monkeypatch.setattr(monitor, 'alpaca_snapshot_quotes', legacy.alpaca_snapshot_quotes if old else
                            lambda symbols: collector(symbols, window_cache=cache))
        monkeypatch.setattr(monitor, 'load', lambda p, d: copy.deepcopy(store.get(p, d)))
        monkeypatch.setattr(monitor, 'save', lambda p, d: store.__setitem__(p, copy.deepcopy(d)))
        monitor.main()
        outputs.append(store)
    for path in (monitor.STATE, monitor.EVENTS, monitor.ROOT / 'summary-v1.json'):
        assert outputs[0][path] == outputs[1][path]
    assert outputs[1][monitor.HEALTH]['shadow_sells'] == 1
    assert outputs[1][monitor.HEALTH]['invalid_quotes'] == {'MISS': 'MISSING_QUOTE', 'STALE': 'STALE_SESSION'}
    timings = outputs[1][monitor.HEALTH]['stage_timings']
    assert all(t >= 0 for t in timings.values())
    assert timings['through_summary_seconds'] >= timings['quotes_seconds']


def test_production_entry_point_does_not_enable_incremental_cache(monkeypatch):
    # The deployed/default template leaves collection on the original path.
    monkeypatch.setenv('STOCK_SHADOW_INCREMENTAL_QUOTES', '0')
    monkeypatch.setattr(monitor, 'load', lambda p, d: copy.deepcopy(d))
    monkeypatch.setattr(monitor, 'save', lambda *args: None)
    monkeypatch.setattr(monitor, '_alpaca_exchange_session', lambda ts=None:
                        {'date': '2026-10-09', 'open': clock_time(9, 30), 'close': clock_time(16)})
    called = []
    def collect_default(symbols, **kwargs):
        called.append(kwargs)
        return {}, [], 0, 0
    monkeypatch.setattr(monitor, 'alpaca_snapshot_quotes', collect_default)
    monitor.main()
    assert called == [{}]
