"""Audit original REST kline windows without changing any feature calculation."""
import datetime as dt
import hashlib
import json
import math

SCHEMA = 'hunter_signal_sources_v1'


def hash_value(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def capture(rows, symbol, interval, observed):
    """Binance REST close time is inclusive. Still-open candles stay unconfirmed.

    Invalid provenance never removes an existing signal: only reentry consumes
    the UNKNOWN result. The caller computes returns from its unchanged rows.
    """
    try:
        bars = []
        for r in rows:
            start, end = int(r[0]), int(r[6])
            values = [float(r[i]) for i in (1, 2, 3, 4, 5, 7)]
            if not all(math.isfinite(v) for v in values):raise ValueError('nonfinite')
            bars.append({'open_ms': start, 'close_ms': end, 'ohlcv_quote': values,
                         'confirmed': end < observed.timestamp()*1000})
        return {'schema': SCHEMA, 'venue': 'binance', 'market': 'spot',
                'symbol': symbol, 'interval': interval, 'source': '/api/v3/klines',
                'observed_at_utc': observed.isoformat(), 'bars': bars,
                'content_hash': hash_value(bars)}
    except (TypeError, ValueError, IndexError, OverflowError):
        return {'schema': SCHEMA, 'status': 'UNKNOWN'}


def validate(packet, symbol, interval, count, now, exited, fresh):
    """Return canonical window/content identity and source time, or raise."""
    if not isinstance(packet, dict):raise ValueError('SOURCE_WINDOW_MISSING')
    if any(packet.get(k) != v for k, v in {
        'schema': SCHEMA, 'venue': 'binance', 'market': 'spot', 'symbol': symbol,
        'interval': interval, 'source': '/api/v3/klines'}.items()):
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
        if type(b.get('confirmed')) is not bool or b['confirmed'] != (end < observed.timestamp()*1000):
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


def signal_sources(signal, asset, now, exited, fresh):
    packets = (signal or {}).get('source_provenance') or {}
    expected = {'asset_1h': (asset+'USDT', '1h', 2), 'asset_4h': (asset+'USDT', '1h', 5),
                'btc_1h': ('BTCUSDT', '1h', 2), 'btc_4h': ('BTCUSDT', '1h', 5),
                'micro': (asset+'USDT', '15m', 25)}
    return {key: validate(packets.get(key), *args, now, exited, fresh) for key, args in expected.items()}
