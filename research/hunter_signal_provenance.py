"""Audit original market windows without changing any feature calculation."""
import datetime as dt
import hashlib
import json
import math
import re

SCHEMA = 'hunter_signal_sources_v1'
CACHE_SOURCE = 'OFFICIAL_BYBIT_V5_SPOT_CACHE'
CACHE_FIELDS = {'schema', 'generation_end_ms', 'roster_hash', 'manifest_sha256',
                'epoch', 'generation_sha256'}
TRANSPORT_FIELDS = {'source', 'received_at_ms', 'source_time_ms', 'confirm',
                    'confirmation', 'session'}


def hash_value(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def capture(rows, symbol, interval, observed, venue='binance'):
    """Binance REST close time is inclusive. Still-open candles stay unconfirmed.

    Invalid provenance never removes an existing signal: only reentry consumes
    the UNKNOWN result. The caller computes returns from its unchanged rows.
    """
    try:
        bars = []
        for r in rows:
            start = int(r[0])
            end = (start+{'1h':3600000,'15m':900000}[interval]-1) if venue == 'bybit' else int(r[6])
            values = [float(r[i]) for i in ((1, 2, 3, 4, 5, 6) if venue == 'bybit' else (1, 2, 3, 4, 5, 7))]
            if not all(math.isfinite(v) for v in values):raise ValueError('nonfinite')
            bars.append({'open_ms': start, 'close_ms': end, 'ohlcv_quote': values,
                         'confirmed': end < observed.timestamp()*1000})
        return {'schema': SCHEMA, 'venue': venue, 'market': 'spot',
                'symbol': symbol, 'interval': interval, 'source': '/v5/market/kline' if venue == 'bybit' else '/api/v3/klines',
                'observed_at_utc': observed.isoformat(), 'bars': bars,
                'content_hash': hash_value(bars)}
    except (TypeError, ValueError, IndexError, OverflowError):
        return {'schema': SCHEMA, 'status': 'UNKNOWN'}


def validate_cache(packet, observed, period):
    """Keep the frozen generation and actual transport observations distinct."""
    cache = packet.get('cache')
    if (not isinstance(cache, dict) or set(cache) != CACHE_FIELDS
            or cache.get('schema') != 'bybit_stream_generation_v1'
            or type(cache.get('generation_end_ms')) is not int
            or observed.tzinfo is None
            or cache['generation_end_ms'] != int(observed.timestamp()*1000)
            or any(not isinstance(cache.get(key), str)
                   or re.fullmatch('[0-9a-f]{64}', cache[key]) is None
                   for key in ('roster_hash', 'manifest_sha256', 'generation_sha256'))
            or not isinstance(cache.get('epoch'), str)
            or re.fullmatch('[0-9a-f]{32}', cache['epoch']) is None):
        raise ValueError('SOURCE_CACHE_GENERATION_INVALID')
    generation = cache['generation_end_ms']
    bars, transport = packet.get('bars'), packet.get('transport')
    if (not isinstance(bars, list) or not bars or not isinstance(transport, list)
            or len(transport) != len(bars)):
        raise ValueError('SOURCE_CACHE_TRANSPORT_INCOMPLETE')
    for bar, item in zip(bars, transport):
        if (not isinstance(bar, dict) or not isinstance(item, dict)
                or set(item) != TRANSPORT_FIELDS
                or item.get('source') not in ('OFFICIAL_BYBIT_V5_SPOT_WS', 'OFFICIAL_BYBIT_V5_SPOT_REST')
                or type(item.get('received_at_ms')) is not int
                or type(item.get('source_time_ms')) is not int
                or not 0 <= item['source_time_ms'] <= item['received_at_ms'] <= generation
                or type(item.get('confirm')) is not bool
                or type(bar.get('confirmed')) is not bool
                or item['confirm'] != bar['confirmed']
                or not isinstance(item.get('session'), str)
                or re.fullmatch('[0-9a-f]{32}', item['session']) is None):
            raise ValueError('SOURCE_CACHE_TRANSPORT_INVALID')
        start, end = bar.get('open_ms'), bar.get('close_ms')
        if (type(start) is not int or type(end) is not int
                or start > item['source_time_ms']):
            raise ValueError('SOURCE_CACHE_TRANSPORT_TIME_INVALID')
        rest = item['source'] == 'OFFICIAL_BYBIT_V5_SPOT_REST'
        if (item['confirmation'] != ('REST_INFERRED' if rest else 'WS_EXPLICIT')
                or (rest and item['confirm'] != (end < item['source_time_ms']))
                or (not rest and item['confirm'] and end > item['source_time_ms'])):
            raise ValueError('SOURCE_CACHE_CONFIRMATION_INVALID')
    current = transport[-1]
    if (bars[-1]['open_ms'] != generation//period*period
            or current['session'] != cache['epoch']
            or not 0 <= generation-current['source_time_ms'] <= 90000
            or not 0 <= generation-current['received_at_ms'] <= 90000):
        raise ValueError('SOURCE_CACHE_CURRENT_WINDOW_NOT_FRESH')
    return cache


def validate_cache_packet(packet, symbol, interval, count):
    """Validate cache evidence internally, without an entry or wall-clock gate."""
    if (not isinstance(packet, dict)
            or interval not in ('1h', '15m')
            or count not in ((2, 5) if interval == '1h' else (25,))
            or any(packet.get(key) != value for key, value in {
                'schema': SCHEMA, 'venue': 'bybit', 'market': 'spot',
                'symbol': symbol, 'interval': interval, 'source': CACHE_SOURCE}.items())):
        raise ValueError('SOURCE_WINDOW_IDENTITY_UNKNOWN')
    try:
        observed = dt.datetime.fromisoformat(str(packet.get('observed_at_utc')).replace('Z', '+00:00'))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError('SOURCE_CACHE_GENERATION_INVALID') from exc
    period = {'1h': 3600000, '15m': 900000}[interval]
    bars = packet.get('bars')
    if not isinstance(bars, list) or len(bars) != count:
        raise ValueError('SOURCE_WINDOW_INCOMPLETE')
    previous = None
    for bar in bars:
        if not isinstance(bar, dict):raise ValueError('SOURCE_WINDOW_CONTENT_INVALID')
        start, end = bar.get('open_ms'), bar.get('close_ms')
        if (type(start) is not int or type(end) is not int or start % period
                or end != start+period-1 or (previous is not None and start != previous+period)):
            raise ValueError('SOURCE_WINDOW_BOUNDARY_INVALID')
        values = bar.get('ohlcv_quote')
        if (not isinstance(values, list) or len(values) != 6
                or any(type(value) not in (float, int) or not math.isfinite(value) for value in values)):
            raise ValueError('SOURCE_WINDOW_CONTENT_INVALID')
        opening, high, low, close, volume, quote = values
        if min(opening, high, low, close) <= 0 or not low <= min(opening, close) <= max(opening, close) <= high or min(volume, quote) < 0:
            raise ValueError('SOURCE_WINDOW_CONTENT_INVALID')
        previous = start
    validate_cache(packet, observed, period)
    if packet.get('content_hash') != hash_value(bars):raise ValueError('SOURCE_WINDOW_HASH_MISMATCH')


def validate(packet, symbol, interval, count, now, exited, fresh, venue='binance'):
    """Return canonical window/content identity and source time, or raise."""
    if not isinstance(packet, dict):raise ValueError('SOURCE_WINDOW_MISSING')
    cached = packet.get('source') == CACHE_SOURCE
    if cached:validate_cache_packet(packet, symbol, interval, count)
    source = CACHE_SOURCE if cached and venue == 'bybit' else '/v5/market/kline' if venue == 'bybit' else '/api/v3/klines'
    if any(packet.get(k) != v for k, v in {
        'schema': SCHEMA, 'venue': venue, 'market': 'spot', 'symbol': symbol,
        'interval': interval, 'source': source}.items()):
        raise ValueError('SOURCE_WINDOW_IDENTITY_UNKNOWN')
    observed = dt.datetime.fromisoformat(str(packet.get('observed_at_utc')).replace('Z', '+00:00'))
    if not fresh(packet.get('observed_at_utc'), now) or not exited < observed <= now:
        raise ValueError('SOURCE_WINDOW_NOT_FRESH_POST_EXIT')
    period = {'1h': 3600000, '15m': 900000}[interval]
    bars = packet.get('bars') or []
    if len(bars) != count:raise ValueError('SOURCE_WINDOW_INCOMPLETE')
    previous = None
    for b in bars:
        start, end = b.get('open_ms'), b.get('close_ms')
        if (type(start) is not int or type(end) is not int or start % period
                or end != start+period-1 or (previous is not None and start != previous+period)
                or start > observed.timestamp()*1000):
            raise ValueError('SOURCE_WINDOW_BOUNDARY_INVALID')
        values = b.get('ohlcv_quote') or []
        if len(values) != 6 or any(type(v) not in (float, int) or not math.isfinite(v) for v in values):
            raise ValueError('SOURCE_WINDOW_CONTENT_INVALID')
        o, h, l, c, volume, quote = values
        if min(o, h, l, c) <= 0 or not l <= min(o, c) <= max(o, c) <= h or min(volume, quote) < 0:
            raise ValueError('SOURCE_WINDOW_CONTENT_INVALID')
        if type(b.get('confirmed')) is not bool or (not cached and b['confirmed'] != (end < observed.timestamp()*1000)):
            raise ValueError('SOURCE_WINDOW_CONFIRMATION_INVALID')
        previous = start
    # A current partial bar is allowed, but never relabel an old completed
    # window with a fresh fetch time. Also require post-exit market coverage.
    closed_at = dt.datetime.fromtimestamp(bars[-1]['close_ms']/1000, dt.timezone.utc)
    if bars[-1]['close_ms'] <= exited.timestamp()*1000 or (closed_at < observed and not fresh(closed_at.isoformat(), observed)):
        raise ValueError('SOURCE_WINDOW_PRE_EXIT_OR_OLD')
    if packet.get('content_hash') != hash_value(bars):raise ValueError('SOURCE_WINDOW_HASH_MISMATCH')
    return {'window': [bars[0]['open_ms'], bars[-1]['close_ms']],
            'hash': hash_value(bars), 'observed_at_utc': observed.isoformat()}


def cache_consistency(packets, keys):
    """An asset, its benchmark and both lookbacks must share one frozen read."""
    cached = any(isinstance(packets.get(key), dict)
                 and packets[key].get('source') == CACHE_SOURCE for key in keys)
    if not cached:return
    if not isinstance(packets.get('btc_micro'), dict):
        raise ValueError('SOURCE_CACHE_BTC_MICRO_MISSING')
    selected = [packets.get(key) for key in keys]
    if any(not isinstance(packet, dict) or packet.get('source') != CACHE_SOURCE for packet in selected):
        raise ValueError('SOURCE_CACHE_MIXED_SOURCE')
    binding = selected[0].get('cache')
    if any(packet.get('cache') != binding for packet in selected):
        raise ValueError('SOURCE_CACHE_GENERATION_MISMATCH')
    for short, long in (('asset_1h', 'asset_4h'), ('btc_1h', 'btc_4h')):
        if short in keys and long in keys:
            for field in ('bars', 'transport'):
                if packets[short].get(field) != (packets[long].get(field) or [])[-2:]:
                    raise ValueError('SOURCE_CACHE_LOOKBACK_MISMATCH')


def signal_sources(signal, asset, now, exited, fresh, venue='binance'):
    packets = (signal or {}).get('source_provenance') or {}
    expected = {'asset_1h': (asset+'USDT', '1h', 2), 'asset_4h': (asset+'USDT', '1h', 5),
                'btc_1h': ('BTCUSDT', '1h', 2), 'btc_4h': ('BTCUSDT', '1h', 5),
                'micro': (asset+'USDT', '15m', 25)}
    # Old live-observation records can still be read. Reconstruction requires
    # btc_micro explicitly and never substitutes the current hourly BTC close.
    if packets.get('btc_micro') is not None:expected['btc_micro'] = ('BTCUSDT', '15m', 25)
    cache_consistency(packets, expected)
    return {key: validate(packets.get(key), *args, now, exited, fresh, venue) for key, args in expected.items()}


def reconstruct_trend(signal, asset, after):
    """Latest closed market point, reconstructed at THIS fetch, never a receipt.

    Call only after signal_sources validated both complete source packets. With
    25 quarters including the live candle, 24 remain at the prior close. Existing
    micro formulas consume at most 24, and all five hourly opens are covered.
    Historical identity, orderbook, health and execution are deliberately absent.
    """
    packets = signal.get('source_provenance') or {}
    if not packets.get('btc_micro'):raise ValueError('HISTORICAL_BTC_MICRO_MISSING')
    cache_consistency(packets, ('micro', 'btc_micro'))
    closed = {}
    for key in ('micro', 'btc_micro'):
        packet = packets[key]
        observed = dt.datetime.fromisoformat(packet['observed_at_utc'])
        if packet.get('source') == CACHE_SOURCE:
            validate_cache_packet(packet, asset+'USDT' if key == 'micro' else 'BTCUSDT', '15m', 25)
        bars = [b for b in packet['bars'] if b['confirmed']]
        expected_close = int(observed.timestamp()*1000)//900000*900000-1
        if len(bars) < 24 or bars[-1]['close_ms'] != expected_close:
            raise ValueError('LATEST_CLOSED_HISTORY_INCOMPLETE')
        closed[key] = bars
    point = closed['micro'][-1]['close_ms']
    if point != closed['btc_micro'][-1]['close_ms'] or point <= after.timestamp()*1000:
        raise ValueError('HISTORICAL_POINT_NOT_ALIGNED_POST_EXIT')
    # Import lazily: producers use this module for lossless capture, while this
    # reader reuses their original feature/score formulas without another policy.
    try:
        from research.hunter_bybit_signal_capture import features
        from research.hunter_early_signals import score_row
    except ModuleNotFoundError as exc:
        if exc.name != 'research':raise
        from hunter_bybit_signal_capture import features
        from hunter_early_signals import score_row
    def inputs(bars):
        terminal = point//3600000*3600000
        hourly = []
        for start in range(terminal-4*3600000, terminal+1, 3600000):
            group = [b for b in bars if start <= b['open_ms'] < start+3600000]
            expected = (point-start)//900000+1 if start == terminal else 4
            if len(group) != expected or group[0]['open_ms'] != start:
                raise ValueError('HISTORICAL_HOURLY_LOOKBACK_INCOMPLETE')
            values = [b['ohlcv_quote'] for b in group]
            hourly.append([start,values[0][0],max(v[1] for v in values),min(v[2] for v in values),
                           values[-1][3],sum(v[4] for v in values),sum(v[5] for v in values)])
        quarter = [[b['open_ms'],*b['ohlcv_quote']] for b in bars[-24:]]
        return features(hourly, quarter)
    a1,a4,micro = inputs(closed['micro'])
    b1,b4,_ = inputs(closed['btc_micro'])
    reconstructed = score_row(asset+'USDT',asset,{asset+'USDT':a1},{asset+'USDT':a4},b1,b4,{asset+'USDT':micro})
    return {'kind':'RECONSTRUCTED_MARKET_TREND_NOT_HISTORICAL_HEALTH',
            'market_at_utc':dt.datetime.fromtimestamp(point/1000,dt.timezone.utc).isoformat(),
            'reconstructed_from_observed_at_utc':{k:packets[k]['observed_at_utc'] for k in closed},
            'source_hashes':{k:packets[k]['content_hash'] for k in closed},
            'price':closed['micro'][-1]['ohlcv_quote'][3], 'signal':reconstructed}
