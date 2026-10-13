"""No-CLI, read-side-only Research/Monitor diagnostic for the Linux paired guard.

Call run_consumer_cost_probe(market, stamp=STAMP+1, scratch_root=scratch)
immediately after the existing 409-symbol seed, before any concurrent leg.
The caller's network/child/scratch guards remain mandatory. This helper adds
an active-only read boundary; its permanent audit hook becomes inert on exit.
Selection/read/clock bindings are scoped fixture injections. Formulas, rows,
packets, fences and the executor all run their actual implementations.
"""
from contextlib import ExitStack, contextmanager
import datetime as dt
from functools import wraps
import hashlib
import json
import math
import os
from pathlib import Path
import sys
from threading import Lock
import time
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlsplit


class ConsumerProbeBoundary(BaseException):
    """A boundary violation must escape production's best-effort Exception paths."""


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
        separators=(',', ':'), allow_nan=False).encode('ascii')).hexdigest()


def _require(condition, reason):
    if not condition:
        raise AssertionError('CONSUMER_COST_PROBE:'+reason)


@contextmanager
def original_delivery_queries():
    """Only the two original query algorithms from immutable 6a5c8590.

    The rest of the reader, formulas, scope, checks and fixture are identical.
    This is not a replay of an entire old deployment or a production fallback.
    """
    from research.hunter_binance_market_control import MarketControlMixin
    from research.hunter_binance_control import Control

    def states(db, requested):
        return {r['topic']: dict(r) for r in db.execute('SELECT * FROM topics')
                if r['topic'] in requested}

    def versions(db, scope):
        return dict(db.execute('SELECT topic,version FROM versions'))

    with patch.object(MarketControlMixin, '_delivery_states', staticmethod(states)), \
            patch.object(Control, '_fence_versions', staticmethod(versions)):
        yield


def compare_delivery_cost(baseline, candidate):
    """Exact outputs/call volume, plus non-overlapping measured segment costs."""
    for field in ('research_scope', 'monitor_scope', 'research_output',
                  'monitor_output', 'actual_read_scopes'):
        _require(baseline[field] == candidate[field], 'PROJECTION_CHANGED_'+field.upper())
    for key, value in baseline['measurements'].items():
        other = candidate['measurements'].get(key)
        _require(other is not None and value['count'] == other['count'] and
                 value['errors'] == other['errors'] == 0, 'PROJECTION_CALL_VOLUME_OR_FAILURE')
    _require(set(baseline['measurements']) == set(candidate['measurements']), 'PROJECTION_METHOD_SET')
    top = dict(research=['research:research.research_one'], monitor=[
        'monitor:consumer.management_inputs', 'monitor:signals.rolling:1h',
        'monitor:signals.rolling:4h', 'monitor:signals.micro', 'monitor:signals.score_row',
        'monitor:monitor.check_cache_delivery:after_reads',
        'monitor:monitor.check_cache_delivery:final_delivery'])
    costs = {}
    for segment, names in top.items():
        old = sum(baseline['measurements'][key]['wall_us']['total'] for key in names)
        new = sum(candidate['measurements'][key]['wall_us']['total'] for key in names)
        costs[segment] = dict(baseline_wall_us=old, candidate_wall_us=new,
                             candidate_over_baseline=new/old, non_regression=new <= old)
    return dict(prior_query_head='6a5c8590ea6b38e7f9d1fd3fb1c7573b334db887',
        scope='TWO_QUERY_ALGORITHMS_ONLY_SAME_PROCESS_SEED_NO_WRITER',
        exact_outputs_and_call_volume_equal=True, segment_costs=costs,
        measured_segments_not_slower=all(item['non_regression'] for item in costs.values()),
        limitations='One fixed-order instrumented baseline/candidate pair; not a statistical tail benchmark, whole-job cost or concurrent-writer acceptance')


@contextmanager
def _read_boundary(paths, results_root, policy_path, blocked):
    active = True

    def reject(event):
        if len(blocked) < 32:
            blocked.append(event)
        raise ConsumerProbeBoundary('CONSUMER_COST_PROBE_FORBIDDEN:'+event)

    def audit(event, values):
        if not active:
            return
        if (event.startswith(('socket.', 'os.exec', 'os.spawn')) or event in
                ('subprocess.Popen', 'os.system', 'os.posix_spawn', 'os.fork', 'os.forkpty')):
            reject(event)
        elif event == 'sqlite3.connect':
            address = os.fsdecode(values[0])
            parsed = urlsplit(address)
            if (parsed.scheme != 'file' or parsed.netloc or
                    parse_qs(parsed.query).get('mode') != ['ro'] or
                    Path(unquote(parsed.path)).resolve(strict=True) not in paths):
                reject('sqlite3.connect:not_exact_existing_readonly_scratch_db')
        elif event == 'open':
            value, mode, flags = values
            if (isinstance(mode, str) and any(x in mode for x in 'wax+') or
                    flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)):
                reject('open:write')
            if not isinstance(value, int):
                path = Path(os.fsdecode(value)).resolve()
                if results_root in path.parents and path != policy_path:
                    reject('open:portfolio_or_results_read')
        elif event in ('os.mkdir', 'os.rmdir', 'os.remove', 'os.rename', 'os.link',
                       'os.symlink', 'os.chmod', 'os.chown', 'os.utime', 'os.truncate'):
            reject(event)

    sys.addaudithook(audit)
    try:
        yield
    finally:
        active = False  # Never restrict the caller's later seed restore/paired legs.


class _Measurements:
    def __init__(self):
        self.segment = 'setup'
        self.calls = []
        self.lock = Lock()

    def call(self, label, function, *args, **kwargs):
        segment = self.segment
        wall = time.perf_counter_ns()
        cpu = time.thread_time_ns()
        error = None
        try:
            return function(*args, **kwargs)
        except BaseException as exc:
            error = dict(type=type(exc).__name__, message=str(exc)[:500])
            raise
        finally:
            elapsed = (time.perf_counter_ns()-wall)//1000
            used = (time.thread_time_ns()-cpu)//1000
            with self.lock:
                _require(len(self.calls) < 20000, 'MEASUREMENT_BOUND')
                self.calls.append(dict(segment=segment, method=label, wall_us=elapsed,
                                       thread_cpu_us=used, error=error))

    def wrap(self, label, function):
        @wraps(function)
        def measured(*args, **kwargs):
            return self.call(label, function, *args, **kwargs)
        return measured

    def summary(self):
        groups = {}
        for call in self.calls:
            groups.setdefault(call['segment']+':'+call['method'], []).append(call)
        result = {}
        for key, calls in sorted(groups.items()):
            item = dict(count=len(calls), errors=sum(c['error'] is not None for c in calls))
            for field in ('wall_us', 'thread_cpu_us'):
                values = sorted(c[field] for c in calls)
                item[field] = dict(total=sum(values), maximum=values[-1],
                    p99=values[(99*len(values)+99)//100-1])
            result[key] = item
        return result


def run_consumer_cost_probe(market, *, stamp, scratch_root):
    """Use an already seeded scratch Market; return bounded diagnostics or raise.

    stamp is the latest seed WS timestamp, not the live wall clock. Measurements
    are inclusive/nested, never additive. Top-level rolling/micro thread CPU
    excludes workers; Frozen method observations measure their executing workers.
    Any failure carries the partial report as consumer_cost_probe_report.
    """
    _require(sys.platform == 'linux', 'LINUX_ONLY')
    _require(type(stamp) is int and stamp > 0, 'FIXED_SEED_STAMP_REQUIRED')
    root = Path(scratch_root).resolve(strict=True)
    path = Path(market.path).resolve(strict=True)
    store = market.store
    _require(root in path.parents and path.parent.name == 'active' and
             path.name == 'state.sqlite3', 'EXISTING_ACTIVE_SCRATCH_MARKET_REQUIRED')
    _require(Path(store.path).resolve(strict=True) == path, 'STORE_PATH_MISMATCH')
    control_path = path.with_name('control.sqlite3').resolve(strict=True)
    _require(getattr(store, 'control', None) is not None and
             Path(store.control.path).resolve(strict=True) == control_path,
             'EXISTING_SCRATCH_CONTROL_REQUIRED')
    repo = Path(__file__).resolve().parents[2]
    results_root = repo/'research/results'
    measured = _Measurements()
    blocked = []
    report = dict(scope='SYNTHETIC_RESEARCH_AND_MONITOR_CACHE_READ_SEGMENTS_ONLY',
        production_evidence=False, real=False, capital_authority='NONE_SHADOW_ONLY',
        fixed_seed_stamp_ms=stamp, acceptance=False, status='RUNNING',
        clock='FIXED_AT_SEED_WS_STAMP_NO_NATURAL_UTC_ROLLOVER',
        excluded=['REST/enrichment/orderbook', 'full Monitor', 'ledger/results I/O',
                  'trade decisions/workflows', 'writer contention/acceptance'],
        actual_methods=['research.hunter_forward_research.research_one',
            'research.hunter_binance_stream_consumer.window/read/Frozen.rows/Frozen.packet',
            'research.hunter_position_monitor.management_selection/check_cache_delivery',
            'research.hunter_binance_stream_consumer.management_inputs',
            'research.hunter_early_signals.rolling(1h)/rolling(4h)/micro/score_row'],
        measurement_semantics=dict(unit='microseconds', percentile='EXACT_NEAREST_RANK_P99',
            inclusive_nested_not_additive=True, thread_cpu='EXECUTING_THREAD_ONLY',
            worker_cpu='Frozen.rows/packet/check_delivery observations include worker threads',
            instrumentation='transparent wrappers; fixed order; no warmup or repeated-load claim'))
    try:
        with _read_boundary({path, control_path}, results_root,
                            results_root/'hunter-shadow-rules.json', blocked):
            from research import hunter_binance_stream_consumer as consumer
            from research import hunter_forward_research as research
            from research import hunter_position_monitor as monitor
            from research.hunter_binance_stream_store import CACHE_SOURCE, topic

            symbols = ['BTCUSDT']+['S%03dUSDT' % index for index in range(408)]
            expected_names = sorted({topic(symbol, interval) for symbol in symbols
                for interval in ('15m', '1h', '1d')} | {topic('BTCUSDT', '5m')})
            _require(sorted(market.names) == expected_names, 'EXACT_409_SYMBOL_1228_TOPIC_SEED')
            now = dt.datetime.fromtimestamp(stamp/1000, dt.timezone.utc)
            coins = {symbol[:-4]: dict(reference_price=101.0, change_since_previous_scan_pct=0,
                change_24h_pct=0, pairs=[dict(venue='binance', pair=symbol,
                    volume_24h_usdt=1000000)]) for symbol in symbols}
            chosen, cursor, triggered = research.select_rotation(coins, {}, [])
            _require(not triggered and len(chosen) == min(max(1, research.ROTATION), 409),
                     'COMPLETE_REGULAR_RESEARCH_ROTATION')
            states = [dict(open_positions=[dict(asset=s[:-4]) for s in symbols]),
                      dict(open_positions=[])]
            selected, monitor_cursor = monitor.management_selection(
                states, 0, monitor.C['MONITOR_EVIDENCE_BATCH'])
            _require(len(selected) == min(monitor.C['MONITOR_EVIDENCE_BATCH'], 409),
                     'COMPLETE_ORIGINAL_MONITOR_BATCH')
            pairs = sorted({asset+'USDT' for asset in selected} | {'BTCUSDT'})
            required = sorted(topic(symbol, interval) for symbol in pairs for interval in ('15m', '1h'))
            report['research_scope'] = dict(universe_symbols=409, rotation=research.ROTATION,
                trigger_limit=research.TRIGGER_LIMIT, triggered=0, early_priority=0,
                scope='REGULAR_ROTATION_ONLY_NO_TRIGGER_OR_MAXIMUM_BATCH_CLAIM',
                selected=chosen, selected_sha256=_hash(chosen), next_cursor=cursor)
            report['monitor_scope'] = dict(input_fixture='409_V1_HOLDINGS_NO_V2_PRIORITY_HOLDINGS',
                policy_batch=monitor.C['MONITOR_EVIDENCE_BATCH'], selected=selected,
                selected_sha256=_hash(selected), pairs=pairs, pair_count=len(pairs),
                required_topics=required, required_sha256=_hash(required), next_cursor=monitor_cursor)
            read_scopes = []
            original_read = consumer.read

            def scratch_read(names, requested_now, **kwargs):
                names = sorted(names)
                _require(int(requested_now.timestamp()*1000) == stamp, 'READ_CLOCK_ESCAPED_SEED')
                _require(not ({'reader', 'clock'} & kwargs.keys()), 'UNEXPECTED_READER_OVERRIDE')
                _require(names == required if measured.segment == 'monitor' else
                    len(names) == 1 and names[0] in {topic(s+'USDT', '1d') for s in chosen},
                    'REQUESTED_SCOPE_CHANGED')
                read_scopes.append(dict(segment=measured.segment, names=names))
                return measured.call('consumer.read', original_read, names, requested_now,
                                     reader=store, clock=lambda: stamp, **kwargs)

            class SeedDatetime(dt.datetime):
                @classmethod
                def now(cls, tz=None):
                    _require(tz is not None, 'NAIVE_RESEARCH_CLOCK')
                    return now.astimezone(tz)

            def packet_present(packet, count):
                _require(isinstance(packet, dict) and packet.get('source') == CACHE_SOURCE and
                         len(packet.get('bars', [])) == count and
                         len(packet.get('transport', [])) == count and
                         packet.get('content_hash') and packet.get('cache', {}).get('epoch') == market.epoch and
                         packet['cache'].get('generation_end_ms') == stamp and
                         packet['cache'].get('generation_sha256'), 'MISSING_CACHE_OUTPUT_OR_PROVENANCE')

            with ExitStack() as stack:
                stack.enter_context(patch.object(consumer, 'selected', lambda: 'SPOT_CACHE'))
                stack.enter_context(patch.object(consumer, 'read', scratch_read))
                stack.enter_context(patch.object(research, 'dt',
                    SimpleNamespace(datetime=SeedDatetime, timezone=dt.timezone)))
                for owner, name, label in ((consumer, 'window', 'consumer.window'),
                        (consumer, 'management_inputs', 'consumer.management_inputs'),
                        (consumer.Frozen, 'rows', 'Frozen.rows'),
                        (consumer.Frozen, 'packet', 'Frozen.packet'),
                        (consumer.Frozen, 'check_delivery', 'Frozen.check_delivery')):
                    stack.enter_context(patch.object(owner, name,
                        measured.wrap(label, getattr(owner, name))))

                measured.segment = 'research'
                research_results = {}
                for asset in chosen:
                    result = measured.call('research.research_one', research.research_one,
                                           asset, coins[asset], {}, {}, now)
                    _require(not any(str(reason).startswith('Binance candle API:')
                        for reason in result.get('missing_facts', [])), 'RESEARCH_SWALLOWED_CACHE_FAILURE')
                    observations = result.get('observations', {})
                    _require(bool(observations.get('market_structure')), 'RESEARCH_FEATURES_MISSING')
                    packet_present(observations.get('market_structure_source'), 35)
                    research_results[asset] = result
                _require(set(research_results) == set(chosen), 'RESEARCH_OUTPUT_SCOPE_SHRANK')
                report['research_output'] = dict(count=len(research_results),
                    sha256=_hash(research_results), expected_missing_fundamentals=True,
                    generation_sha256_by_asset={asset: value['observations']['market_structure_source']
                        ['cache']['generation_sha256'] for asset, value in research_results.items()})

                measured.segment = 'monitor'
                cached = consumer.management_inputs(pairs, now)
                _require(isinstance(cached, consumer.Frozen), 'MONITOR_CACHE_NOT_SELECTED')
                r1 = measured.call('signals.rolling:1h', monitor.signals.rolling, pairs, '1h', cached)
                r4 = measured.call('signals.rolling:4h', monitor.signals.rolling, pairs, '4h', cached)
                micro = measured.call('signals.micro', monitor.signals.micro, pairs, cached)
                measured.call('monitor.check_cache_delivery:after_reads', monitor.check_cache_delivery, cached)
                for values, count in ((r1, 2), (r4, 5), (micro, 25)):
                    _require(set(values) == set(pairs), 'MONITOR_FORMULA_OUTPUT_SCOPE_SHRANK')
                    for value in values.values():
                        _require(bool(value), 'MONITOR_EMPTY_FORMULA_OUTPUT')
                        packet_present(value.get('source_provenance'), count)
                        _require(all(math.isfinite(number) for key, number in value.items()
                            if key != 'source_provenance'), 'MONITOR_NONFINITE_FORMULA_OUTPUT')
                scores = {}
                for asset in selected:
                    row = measured.call('signals.score_row', monitor.signals.score_row,
                        asset+'USDT', asset, r1, r4, r1['BTCUSDT'], r4['BTCUSDT'], micro)
                    _require(bool(row) and row.get('source_provenance'), 'MONITOR_SCORE_MISSING')
                    sources = row['source_provenance']
                    for key, count in (('asset_1h', 2), ('asset_4h', 5), ('btc_1h', 2),
                                       ('btc_4h', 5), ('micro', 25), ('btc_micro', 25)):
                        packet_present(sources.get(key), count)
                    scores[asset] = row
                measured.call('monitor.check_cache_delivery:final_delivery', monitor.check_cache_delivery, cached)
                _require(set(scores) == set(selected), 'MONITOR_SCORE_SCOPE_SHRANK')
                generations = {value['source_provenance']['cache']['generation_sha256']
                    for values in (r1, r4, micro) for value in values.values()}
                _require(len(generations) == 1, 'MONITOR_MIXED_GENERATION')
                report['monitor_output'] = dict(count=len(scores), sha256=_hash(scores),
                    rolling_1h_count=len(r1), rolling_4h_count=len(r4), micro_count=len(micro),
                    generation_sha256=next(iter(generations)))
                _require(len(read_scopes) == len(chosen)+1 and
                         sum(x['segment'] == 'monitor' for x in read_scopes) == 1,
                         'ACTUAL_READ_CALL_COUNT_CHANGED')
                report['actual_read_scopes'] = read_scopes
                _require(not any(call['error'] for call in measured.calls), 'SWALLOWED_METHOD_EXCEPTION')
                _require(not blocked, 'BOUNDARY_VIOLATION')
                report['status'] = 'PASSED_READ_SEGMENTS_ONLY'
    except BaseException as error:
        report['status'] = 'FAILED'
        report['error'] = dict(type=type(error).__name__, message=str(error)[:1000])
        error.consumer_cost_probe_report = report
        raise
    finally:
        report['measurements'] = measured.summary()
        report['calls'] = measured.calls
        report['blocked_events'] = blocked
    return report
