"""Bounded, resumable evidence for completed 5-minute observation candles.

Only whole candles inside the requested window are eligible. Sub-candle edges
are disclosed, never filled with a candle containing pre-entry or future prices.
The checkpoint holds counts and peaks, not an ever-growing copy of market data.
"""
import datetime as dt
import math

STEP_MS = 300000
PAGE_BARS = 1000
SCHEMA = 'hunter_completed_5m_coverage_v1'


def millis(value):
    return int(value.timestamp() * 1000)


def bounds(start, end):
    return ((millis(start) + STEP_MS - 1) // STEP_MS * STEP_MS,
            millis(end) // STEP_MS * STEP_MS)


def checkpoint(pos, opened, closed):
    identity = [pos.get('asset'), pos.get('execution_venue', 'BINANCE_SPOT'),
                opened.isoformat(), closed.isoformat() if closed else None]
    progress = pos.get('opportunity_history_progress') or {}
    if progress.get('schema') != SCHEMA or progress.get('identity') != identity:
        start = bounds(opened, opened)[0]
        progress = dict(schema=SCHEMA, identity=identity, next_open_ms=start,
                        bars_seen=0, peaks={})
        pos['opportunity_history_progress'] = progress
    return progress


def advance(progress, bars, opened, closed, end, hours):
    """Consume a contiguous prefix only; retry the first gap on the next cycle."""
    stop = bounds(opened, end)[1]
    rows = {}
    for row in bars or []:
        if not isinstance(row, (list, tuple)) or len(row) != 3:
            continue  # Legacy timestamp/high pairs cannot prove time coverage.
        start, close, high = row
        if (type(start) is not int or type(close) is not int or
                start % STEP_MS or close != start + STEP_MS - 1 or
                type(high) not in (int, float) or not math.isfinite(high) or high <= 0):
            continue
        if start in rows and rows[start] != high:
            rows[start] = None  # Contradictory duplicates cannot certify a bar.
        else:
            rows[start] = high
    cursor = progress['next_open_ms']
    limits = {'full': (bounds(opened, end)[0], stop),
              'holding': bounds(opened, closed or end)}
    if closed:
        limits.update({f'{int(h)}h': bounds(closed, closed + dt.timedelta(hours=h)) for h in hours})
    consumed = 0
    while cursor < stop and consumed < PAGE_BARS:
        high = rows.get(cursor)
        if high is None:
            break
        for key, (first, last) in limits.items():
            if first <= cursor < last:
                old = progress['peaks'].get(key)
                if not old or high > old['peak_price']:
                    progress['peaks'][key] = dict(peak_price=high,
                        peak_at_utc=dt.datetime.fromtimestamp(cursor/1000, dt.timezone.utc).isoformat(),
                        source='BINANCE_SPOT_KLINES_5M_COMPLETED_ONLY')
        cursor += STEP_MS
        consumed += 1
    progress['next_open_ms'] = cursor
    progress['bars_seen'] += consumed
    progress['status'] = ('COMPLETE' if cursor >= stop else
                          'BACKFILL_PENDING' if consumed == PAGE_BARS else 'HISTORY_GAP')
    return progress


def coverage(progress, start, end):
    first, stop = bounds(start, end)
    expected = max(0, (stop-first)//STEP_MS)
    seen = max(0, (min(stop, progress['next_open_ms'])-first)//STEP_MS)
    return dict(schema=SCHEMA, status='COMPLETE' if expected > 0 and seen == expected else 'HISTORY_GAP',
                requested_start_utc=start.isoformat(), requested_end_utc=end.isoformat(),
                eligible_start_ms=first, eligible_end_ms=stop,
                start_edge_excluded_ms=first-millis(start), end_edge_excluded_ms=millis(end)-stop,
                expected_bars=expected, observed_bars=seen, missing_bars=expected-seen)


def verified(record, start, end):
    cov = (record or {}).get('coverage') or {}
    first, stop = bounds(start, end)
    expected = max(0, (stop-first)//STEP_MS)
    return (cov.get('schema') == SCHEMA and cov.get('status') == 'COMPLETE'
            and cov.get('requested_start_utc') == start.isoformat()
            and cov.get('requested_end_utc') == end.isoformat()
            and cov.get('eligible_start_ms') == first and cov.get('eligible_end_ms') == stop
            and expected > 0 and cov.get('expected_bars') == cov.get('observed_bars') == expected
            and cov.get('missing_bars') == 0)
