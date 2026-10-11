"""Opt-in cache Klines for the existing single-writer discovery pipeline.

Tickers/listings retain their explicit Worker source. This module reads one
local generation, computes the unchanged features, and never performs network
IO, repairs, fallback, publication, or ledger writes.
"""
import copy
import datetime as dt
import time

try:
    from research.hunter_bybit_stream_store import Store, LIMITS, FRESH_MS, content_hash, roster_hash, topic, valid_row
    from research.hunter_bybit_signal_capture import capture
    from research.hunter_signal_provenance import hash_value, capture as provenance, validate_cache_packet
except ModuleNotFoundError as exc:
    if exc.name != 'research':
        raise
    from hunter_bybit_stream_store import Store, LIMITS, FRESH_MS, content_hash, roster_hash, topic, valid_row
    from hunter_bybit_signal_capture import capture
    from hunter_signal_provenance import hash_value, capture as provenance, validate_cache_packet

SOURCE = 'OFFICIAL_BYBIT_V5_SPOT_CACHE'
SCHEMA = 'hunter_bybit_stream_snapshot_v1'
WORKER_SOURCE = 'OFFICIAL_BYBIT_V5_VIA_WORKER'


def topics_for(bases):
    if (not isinstance(bases, list) or bases != sorted(set(bases)) or 'BTC' in bases):
        raise ValueError('CACHE_EXPECTED_ROSTER_INVALID')
    return sorted(topic(b+'USDT', i) for b in bases+['BTC'] for i in LIMITS)


def binding(generation):
    return {k: generation[k] for k in ('schema', 'generation_end_ms', 'roster_hash',
                                      'manifest_sha256', 'epoch', 'generation_sha256')}


def packet(generation, symbol, interval, count):
    window = generation['windows'][topic(symbol, {'1h':'60','15m':'15'}[interval])]
    end = dt.datetime.fromtimestamp(generation['generation_end_ms']/1000, dt.timezone.utc)
    out = provenance(window['rows'][-count:], symbol, interval, end, 'bybit')
    out.update(source=SOURCE, cache=binding(generation), transport=copy.deepcopy(window['provenance'][-count:]))
    for bar, transport in zip(out['bars'], out['transport']):
        bar['confirmed'] = transport['confirm']
    out['content_hash'] = hash_value(out['bars'])
    return out


def packets(generation, base):
    return {key: packet(generation, symbol, interval, count) for key, symbol, interval, count in (
        ('asset_1h',base+'USDT','1h',2), ('asset_4h',base+'USDT','1h',5),
        ('btc_1h','BTCUSDT','1h',2), ('btc_4h','BTCUSDT','1h',5),
        ('micro',base+'USDT','15m',25), ('btc_micro','BTCUSDT','15m',25))}


def collect(required, now, reader=None, clock=None):
    end = int(now.timestamp()*1000)
    names = topics_for(sorted(r['base'] for r in required))
    reader = reader or Store.reader()
    clock = clock or (lambda: time.time_ns()//1_000_000)
    read_at = clock()
    generation = reader.freeze(names, end, read_at)
    # SQLite lock waits and a slow full-roster read consume the same freshness
    # allowance. Admission before the limit is not proof of a timely read exit.
    finished = clock()
    if generation['status'] == 'READY' and (finished < read_at or not 0 <= finished-end <= FRESH_MS):
        generation.update(status='UNKNOWN', windows={}, reason='CACHE_READ_FINISHED_STALE_OR_CLOCK_ROLLBACK')
    generation['generation_sha256'] = content_hash({k:v for k,v in generation.items() if k != 'generation_sha256'})
    if generation['status'] != 'READY':
        return {}, {s: 'CACHE_UNKNOWN:'+generation['reason']
                    for s in sorted({r['pair'] for r in required}|{'BTCUSDT'})}, generation
    signals, failures = signals_from_generation(required, now, generation)
    return signals, failures, generation


def signals_from_generation(required, now, generation):
    """The original feature/score implementation, with truthful cache provenance."""
    def get(symbol, interval, limit):
        if limit != LIMITS[interval]:
            raise ValueError('CACHE_WINDOW_SIZE_MISMATCH')
        return copy.deepcopy(generation['windows'][topic(symbol, interval)]['rows'])
    signals, failures = capture(required, get, observed_at=now)
    for base, signal in signals.items():
        signal['source_provenance'] = packets(generation, base)
        signal['source_observed_at_utc'] = now.isoformat()
    return signals, failures


def validate(snapshot):
    """Validate source, complete scope, immutable evidence and UNKNOWN together."""
    status = snapshot['venue_status']
    bases = status.get('signal_expected_bases')
    names = topics_for(bases)
    symbols = {b+'USDT' for b in bases}|{'BTCUSDT'}
    generation = snapshot.get('cache_generation', {})
    end = int(dt.datetime.fromisoformat(snapshot['captured_at_utc']).timestamp()*1000)
    if (snapshot.get('schema') != SCHEMA or snapshot.get('source') != SOURCE
            or status.get('source') != SOURCE or status.get('ticker_source') != WORKER_SOURCE
            or status.get('listing_source') != WORKER_SOURCE or status.get('kline_source') != SOURCE
            or status.get('captured_at_utc') != snapshot['captured_at_utc']
            or status.get('kline_batch_requests') != 0 or 'signal_failure_details' in snapshot
            or generation.get('schema') != 'bybit_stream_generation_v1'
            or generation.get('generation_end_ms') != end
            or generation.get('roster_hash') != roster_hash(names)
            or generation.get('generation_sha256') != content_hash({k:v for k,v in generation.items() if k != 'generation_sha256'})
            or status.get('cache_generation_sha256') != generation['generation_sha256']):
        raise ValueError('CACHE_SNAPSHOT_BINDING_INVALID')
    signals, failures = snapshot.get('early_signals'), snapshot.get('signal_failures')
    if not isinstance(signals, dict) or not isinstance(failures, dict) or not set(bases) <= {r['base'] for r in snapshot['rows']}:
        raise ValueError('CACHE_SNAPSHOT_SCOPE_INVALID')
    if generation.get('status') == 'UNKNOWN':
        reason = generation.get('reason')
        if (generation.get('windows') != {} or signals or set(failures) != symbols
                or not isinstance(reason, str) or len(reason) > 80
                or any(v != 'CACHE_UNKNOWN:'+reason for v in failures.values())
                or status.get('signal_complete') is not False):
            raise ValueError('CACHE_UNKNOWN_NOT_PROPAGATED')
    elif generation.get('status') == 'READY':
        if (set(generation.get('windows', {})) != set(names) or set(signals) != set(bases)
                or failures or status.get('signal_complete') is not True):
            raise ValueError('CACHE_READY_COVERAGE_INVALID')
        for name, window in generation['windows'].items():
            _, interval, symbol = name.split('.')
            rows = window['rows']
            step = int(interval)*60_000
            if (len(rows) != LIMITS[interval] or len(window['provenance']) != len(rows)
                    or [int(r[0]) for r in rows] != sorted((end//step-i)*step for i in range(LIMITS[interval]))):
                raise ValueError('CACHE_WINDOW_BOUNDARY_INVALID')
            for row in rows:
                valid_row(row, interval)
            interval_name = '1h' if interval == '60' else '15m'
            validate_cache_packet(packet(generation, symbol, interval_name, len(rows)), symbol, interval_name, len(rows))
        for base, signal in signals.items():
            if (signal.get('source_provenance') != packets(generation, base)
                    or signal.get('source_observed_at_utc') != snapshot['captured_at_utc']):
                raise ValueError('CACHE_SIGNAL_SOURCE_MISMATCH')
        expected_signals, expected_failures = signals_from_generation(
            [dict(base=b,pair=b+'USDT') for b in bases],
            dt.datetime.fromisoformat(snapshot['captured_at_utc']), generation)
        if failures != expected_failures or signals != expected_signals:
            raise ValueError('CACHE_SIGNAL_CONTENT_MISMATCH')
    else:
        raise ValueError('CACHE_GENERATION_STATUS_INVALID')
    if status.get('signal_pairs') != len(signals) or status.get('signal_failures') != len(failures):
        raise ValueError('CACHE_SIGNAL_COUNTS_INVALID')
