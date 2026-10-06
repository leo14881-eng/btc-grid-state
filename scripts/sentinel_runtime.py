"""Sentinel execution plumbing: public evidence, no orders or portfolio mutations.

This is a conservative evidence scan, not a replacement investment strategy.
Incomplete analysis preserves previous leading state and blocks capital actions.
"""
import argparse
import copy
import datetime as dt
import json
from pathlib import Path
import subprocess
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

UTC = dt.timezone.utc
OWNED = frozenset(('run_id last_run_at last_scan_at updated_at last_successful_scan_at '
 'market_data_asof etf_data_asof derivatives_data_asof macro_data_asof run_status '
 'primary_state first_detected_at main_path leading_evidence reversal_triggers '
 'early_action confirmation_status data_gaps last_persisted_at market_state_freshness '
 'leading_warning runtime evidence freshness_gate watchdog notification_decision '
 'axs_monitor persistence').split())

def stamp(now=None):
    return (now or dt.datetime.now(UTC)).isoformat(timespec='microseconds')

def instant(value):
    if not value:
        return None
    return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(UTC)

def get_json(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'sentinel-public-evidence/1.0'})
    with urllib.request.urlopen(req, timeout=12) as response:
        return json.load(response)

def get_text(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'sentinel-public-evidence/1.0'})
    with urllib.request.urlopen(req, timeout=12) as response:
        return response.read().decode('utf-8')


def treasury_daily(xml):
    records = []
    for item in ET.fromstring(xml).iter():
        if item.tag.rsplit('}', 1)[-1] == 'properties':
            record = {child.tag.rsplit('}', 1)[-1]: child.text for child in item}
            if record.get('NEW_DATE'):
                records.append(record)
    if not records:
        raise ValueError('NO_TREASURY_DAILY_OBSERVATIONS')
    latest = max(records, key=lambda record: record['NEW_DATE'])
    day = dt.date.fromisoformat(latest['NEW_DATE'][:10])
    return {'asof': None, 'asof_date': day.isoformat(),
            'asof_definition': 'DAILY_OBSERVATION_DATE_NOT_INTRADAY_EVENT_TIMESTAMP',
            'source_provider': 'US_TREASURY', 'yield_unit': 'PERCENT',
            'us2y_pct': float(latest['BC_2YEAR']), 'us10y_pct': float(latest['BC_10YEAR']),
            'us30y_pct': float(latest['BC_30YEAR']), 'dxy': None,
            'limitation': 'TREASURY_YIELDS_ONLY; NO_DXY_OR_INTRADAY_MACRO_CONFIRMATION'}

def source(url, parser, get=get_json):
    attempted = stamp()
    try:
        raw = get(url)
        fetched = stamp()
        result = parser(raw)
        return {'source': url, 'attempted_at': attempted, 'fetched_at': fetched, **result}
    except Exception as error:
        return {'source': url, 'attempted_at': attempted, 'fetched_at': None,
                'error': type(error).__name__ + ': ' + str(error), 'asof': None}


def spot_source(url, parser, get=get_json):
    """Same Binance spot feed via its official public market-data-only host."""
    attempts = []
    for endpoint in (url, url.replace('https://api.binance.com', 'https://data-api.binance.vision')):
        record = source(endpoint, parser, get)
        attempts.append(record)
        if not record.get('error'):
            break
    result = dict(attempts[-1])
    result.update(source_venue='BINANCE', market_type='SPOT',
                  fallback_attempts=attempts)
    return result


def oi_history(data):
    if not isinstance(data, list) or len(data) < 2:
        raise ValueError('INSUFFICIENT_OI_HISTORY')
    first, last = data[0], data[-1]
    start, end = float(first['sumOpenInterest']), float(last['sumOpenInterest'])
    if start <= 0 or int(first['timestamp']) >= int(last['timestamp']):
        raise ValueError('INVALID_OI_HISTORY')
    return {'asof': millis(last['timestamp']), 'source_venue': 'BINANCE',
            'market_type': 'USD_M_FUTURES', 'open_interest': end,
            'open_interest_value_usdt': float(last['sumOpenInterestValue']),
            'change_pct_over_returned_window': (end / start - 1) * 100,
            'window_start_asof': millis(first['timestamp']),
            'window_end_asof': millis(last['timestamp']), 'samples': len(data),
            'open_interest_unit': 'BASE_ASSET', 'value_unit': 'USDT',
            'samples_raw': data}

def depth_levels(bids, asks):
    bids = [[float(price), float(size)] for price, size, *_ in bids]
    asks = [[float(price), float(size)] for price, size, *_ in asks]
    if not bids or not asks or bids[0][0] <= 0 or asks[0][0] < bids[0][0]:
        raise ValueError('INVALID_ORDERBOOK')
    mid = (bids[0][0] + asks[0][0]) / 2
    return {'bid': bids[0][0], 'ask': asks[0][0], 'bids': bids, 'asks': asks,
            'spread_bps': (asks[0][0] - bids[0][0]) / mid * 10000,
            'bid_depth_usdt_returned_levels': sum(p * q for p, q in bids),
            'ask_depth_usdt_returned_levels': sum(p * q for p, q in asks)}


def bybit_spot_depth(data):
    if data.get('retCode') != 0:
        raise ValueError('BYBIT_ORDERBOOK_RESPONSE_REJECTED')
    book = data['result']
    if book.get('s') != 'BTCUSDT':
        raise ValueError('ORDERBOOK_SYMBOL_MISMATCH')
    return {'asof': millis(book['ts']), 'timestamp_quality': 'EXCHANGE_RESPONSE_TIMESTAMP',
            'venue': 'BYBIT_SPOT', 'source_venue': 'BYBIT', 'market_type': 'SPOT',
            'price_currency': 'USDT', 'quantity_currency': 'BTC', 'symbol': book['s'],
            'last_update_id': book.get('u'), **depth_levels(book['b'], book['a'])}


def collect_depth(get=get_json):
    primary = spot_source('https://api.binance.com/api/v3/depth?symbol=BTCUSDT&limit=20',
        lambda d: {'asof': None, 'timestamp_quality': 'RECEIPT_TIME_ONLY_NOT_EXCHANGE_TIMESTAMP',
                   'venue': 'BINANCE_SPOT', 'source_venue': 'BINANCE', 'market_type': 'SPOT',
                   'price_currency': 'USDT', 'quantity_currency': 'BTC', 'symbol': 'BTCUSDT',
                   'last_update_id': d['lastUpdateId'], **depth_levels(d['bids'], d['asks'])}, get)
    # Binance REST spot depth has no event timestamp. Receipt time must not pass
    # freshness. An independently timestamped venue is evidence, never Binance depth.
    fallback = source('https://api.bybit.com/v5/market/orderbook?category=spot&symbol=BTCUSDT&limit=25',
                      bybit_spot_depth, get)
    attempts = primary.get('fallback_attempts', [primary]) + [fallback]
    if not fallback.get('asof'):
        alternate = source('https://api.bytick.com/v5/market/orderbook?category=spot&symbol=BTCUSDT&limit=25', bybit_spot_depth, get)
        attempts.append(alternate)
        if alternate.get('asof'):
            fallback = alternate
    selected = fallback if fallback.get('asof') else primary
    return {**selected, 'fallback_attempts': attempts,
            'cross_venue_usage': 'EVIDENCE_ONLY_NOT_EXECUTION_VENUE_PROOF'}

def millis(n):
    return stamp(dt.datetime.fromtimestamp(float(n) / 1000, UTC))

def collect(get=get_json):
    specs = {
      'btc_spot': ('https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT', lambda d: {'asof': millis(d['closeTime']), 'price': float(d['lastPrice']), 'volume_24h': float(d['volume']), 'price_change_pct_24h': float(d['priceChangePercent'])}),
      'btc_structure': ('https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=4h&limit=30', lambda d: {'asof': millis(d[-1][0]), 'candles': d, 'asof_definition': 'last_candle_open; close_time_future_is_not_freshness_proof'}),
      'btc_structure_1h': ('https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=30', lambda d: {'asof': millis(d[-1][0]), 'candles': d, 'interval': '1h', 'asof_definition': 'last_candle_open; close_time_future_is_not_freshness_proof'}),
      'btc_structure_daily': ('https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1d&limit=30', lambda d: {'asof': millis(d[-1][0]), 'candles': d, 'interval': '1d', 'asof_definition': 'last_candle_open; close_time_future_is_not_freshness_proof'}),
      'btc_oi_history': ('https://fapi.binance.com/futures/data/openInterestHist?symbol=BTCUSDT&period=1h&limit=25', oi_history),
      'axs_oi_history': ('https://fapi.binance.com/futures/data/openInterestHist?symbol=AXSUSDT&period=1h&limit=25', oi_history),
      'btc_oi': ('https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT', lambda d: {'asof': millis(d['time']), 'open_interest': float(d['openInterest'])}),
      'btc_funding': ('https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT', lambda d: {'asof': millis(d['time']), 'funding_rate': float(d['lastFundingRate']), 'mark_price': float(d['markPrice'])}),
      'axs_spot': ('https://api.binance.com/api/v3/ticker/24hr?symbol=AXSUSDT', lambda d: {'asof': millis(d['closeTime']), 'price': float(d['lastPrice']), 'volume_24h': float(d['volume']), 'quote_volume_24h': float(d['quoteVolume'])}),
      'axs_oi': ('https://fapi.binance.com/fapi/v1/openInterest?symbol=AXSUSDT', lambda d: {'asof': millis(d['time']), 'open_interest': float(d['openInterest'])}),
      'axs_funding': ('https://fapi.binance.com/fapi/v1/premiumIndex?symbol=AXSUSDT', lambda d: {'asof': millis(d['time']), 'funding_rate': float(d['lastFundingRate'])}),
      'axs_korea': ('https://api.upbit.com/v1/ticker?markets=KRW-AXS', lambda d: {'asof': millis(d[0]['trade_timestamp']), 'price_krw': float(d[0]['trade_price']), 'volume_24h': d[0]['acc_trade_volume_24h'], 'turnover_krw_24h': d[0]['acc_trade_price_24h']}),
      'axs_korea_hours': ('https://api.upbit.com/v1/candles/minutes/60?market=KRW-AXS&count=25', lambda d: {'asof': millis(d[0]['timestamp']), 'candles': d}),
    }
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {key: pool.submit(spot_source if url.startswith('https://api.binance.com/') else source, url, parser, get) for key, (url, parser) in specs.items()}
        depth = pool.submit(collect_depth, get)
        treasury = pool.submit(source, 'https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml?data=daily_treasury_yield_curve&field_tdr_date_value=' + str(dt.datetime.now(UTC).year), treasury_daily, get_text if get is get_json else get)
        evidence = {key: future.result() for key, future in futures.items()}
        evidence['btc_depth'] = depth.result()
        evidence['macro_treasury_daily'] = treasury.result()
        return evidence

def fresh(record, now, seconds):
    try:
        age = (now - instant(record.get('asof'))).total_seconds()
        return -30 <= age <= seconds
    except (TypeError, ValueError):
        return False

def watchdog(previous, valid, now):
    failures = 0 if valid else previous.get('watchdog', {}).get('consecutive_invalid_scans', 0) + 1
    last = now if valid else instant(previous.get('last_successful_scan_at'))
    degraded = not last or (now - last).total_seconds() > 7200 or failures >= 2
    old = previous.get('watchdog', {}).get('state')
    state = 'SENTINEL_DEGRADED' if degraded else 'HEALTHY'
    transition = ('SENTINEL_DEGRADED' if degraded else 'SENTINEL_RECOVERED') if state != old else None
    return {'state': state, 'consecutive_invalid_scans': failures, 'transition': transition, 'checked_at': stamp(now)}

def scan(previous, evidence, now=None):
    now = now or dt.datetime.now(UTC)
    limits = {'btc_structure': 14460, 'btc_structure_1h': 3660, 'btc_structure_daily': 86460, 'btc_oi_history': 3660, 'axs_oi_history': 3660}
    gates = {key: fresh(record, now, limits.get(key, 600)) for key, record in evidence.items()}
    valid = all(gates.get(key, False) for key in ('btc_spot', 'btc_structure', 'btc_oi', 'btc_funding'))
    gaps = [key + ': ' + record.get('error', 'SOURCE_TIMESTAMP_UNVERIFIED_OR_STALE') for key, record in evidence.items() if not gates[key]]
    macro_gap = 'MACRO_INTRADAY_AND_DXY_UNVERIFIED; TREASURY_DAILY_OBSERVED_' + evidence['macro_treasury_daily']['asof_date'] if evidence.get('macro_treasury_daily', {}).get('asof_date') else 'MACRO_TIMESTAMPED_EVIDENCE_NOT_ACQUIRED'
    gaps += ['ETF_LATEST_COMPLETE_SESSION_NOT_ACQUIRED', macro_gap, 'LIQUIDATIONS_AND_HOLDER_SUPPLY_UNVERIFIED', 'FULL_LEADING_WARNING_ENGINE_REMAINS_CHATGPT; server evidence scan does not replace strategy']
    # Collection is not a complete Leading Warning analysis. Never advance success.
    wd = watchdog(previous, False, now)
    axs = {'status': 'PARTIAL_DATA', 'capital_action': 'NO_AUTOMATIC_TRADE', 'left_side_research': 'UNKNOWN_REQUIRES_LOW_STRUCTURE_KOREAN_FLOW_AND_BTC_RISK', 'second_wave': 'UNCONFIRMED', 'strong_breakout': 'UNCONFIRMED'}
    if all(gates.get(key, False) for key in ('axs_spot', 'axs_korea', 'axs_korea_hours', 'axs_oi', 'axs_funding')):
        candles = evidence['axs_korea_hours']['candles']
        # Only completed hours are compared; no made-up volume significance threshold.
        if len(candles) >= 22:
            mean = sum(float(c['candle_acc_trade_volume']) for c in candles[2:22]) / 20
            axs['completed_hour_volume_ratio_to_prior20'] = float(candles[1]['candle_acc_trade_volume']) / mean if mean else None
        axs['price_usdt'] = evidence['axs_spot']['price']
        axs['second_wave_price_zone_reached'] = axs['price_usdt'] >= 1.40
        axs['strong_breakout_price_zone_reached'] = axs['price_usdt'] >= 1.454
        axs['status'] = 'OBSERVED_NOT_STRATEGY_CONFIRMED'
        axs['note'] = 'Price zone is evidence only; Korean flow and OI/price confirmation require full analysis. Lower-cost left-side research is separate.'
    at = stamp(now)
    previous_notification = previous.get('notification_decision', {})
    pending_type = wd['transition']
    if not pending_type and previous_notification.get('delivery_status') != 'DELIVERED':
        expected_type = 'SENTINEL_DEGRADED' if wd['state'] == 'SENTINEL_DEGRADED' else 'SENTINEL_RECOVERED'
        if previous_notification.get('type') == expected_type:
            pending_type = expected_type
    notification = {'required': bool(pending_type), 'type': pending_type, 'delivery_status': 'NOT_DELIVERED_NO_SERVER_CHATGPT_NOTIFICATION_TRANSPORT'}
    if pending_type:
        notification['event_id'] = previous_notification.get('event_id') if not wd['transition'] else 'sentinel-health-' + at
    result = {'run_id': 'sentinel-' + now.strftime('%Y%m%dT%H%M%S.%fZ'), 'last_run_at': at, 'last_scan_at': at, 'updated_at': at,
      'run_status': 'ANALYSIS_FAILED' if valid else 'DATA_STALE', 'market_data_asof': evidence.get('btc_spot', {}).get('asof'),
      'derivatives_data_asof': evidence.get('btc_oi', {}).get('asof'), 'etf_data_asof': None, 'macro_data_asof': None,
      'early_action': 'NO_NEW_CAPITAL_ACTION', 'confirmation_status': 'ANALYSIS_NOT_PORTED',
      'data_gaps': gaps, 'evidence': evidence, 'freshness_gate': {'sources': gates, 'valid_evidence_scan': valid, 'valid_partial_scan': False, 'new_capital_action_allowed': False},
      'watchdog': wd, 'axs_monitor': axs, 'runtime': {'source': 'VULTR_SYSTEMD', 'scope': 'EVIDENCE_SCAN_ACCEPTANCE_ONLY', 'execution_authority': 'USER_ONLY', 'real_trading_enabled': False},
      'notification_decision': notification}
    return result

def merge_owned(previous, mutation):
    if set(mutation) - OWNED:
        raise ValueError('NON_OWNED_FIELD')
    old_at, new_at = instant(previous.get('last_run_at')), instant(mutation.get('last_run_at'))
    if old_at and new_at and old_at > new_at:
        raise ValueError('STALE_RUN_REJECTED')
    if old_at == new_at and previous.get('run_id') and previous.get('run_id') != mutation.get('run_id'):
        raise ValueError('SAME_TIMESTAMP_DIFFERENT_RUN_REJECTED')
    merged = copy.deepcopy(previous)
    merged.update(mutation)
    if merged.get('runtime', {}).get('real_trading_enabled') is not False:
        raise ValueError('NO_REAL_TRADING')
    return merged

def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()

def persist(root, mutation, attempt_hook=None, readback_hook=None, required_registry=None):
    """Exact-path blob fetch/merge; non-force ref CAS retries 3, reread bytes.

    Unrelated main advances are rebased onto latest tree, never overwritten.
    """
    root = Path(root)
    for attempt in range(3):
        git(root, 'fetch', 'origin', 'main')
        base = git(root, 'rev-parse', 'origin/main')
        old_blob = git(root, 'rev-parse', base + ':sentinel-state.json')
        if required_registry is not None and json.loads(git(root, 'show', base + ':sentinel-runtime.json')) != required_registry:
            raise RuntimeError('SINGLE_WRITER_ADMISSION_CHANGED')
        previous = json.loads(git(root, 'show', base + ':sentinel-state.json'))
        if previous.get('run_id') == mutation['run_id']:
            for key, value in mutation.items():
                if key != 'last_persisted_at' and previous.get(key) != value:
                    raise ValueError('IDEMPOTENT_RUN_CONTENT_MISMATCH')
            return {'verified': True, 'idempotent': True, 'head': base, 'state': previous}
        pending = dict(mutation, last_persisted_at=stamp())
        merged = merge_owned(previous, pending)
        serialized = json.dumps(merged, indent=2, ensure_ascii=False) + '\n'
        with tempfile.TemporaryDirectory(prefix='sentinel-cas-') as path:
            git(root, 'worktree', 'add', '--detach', path, base)
            try:
                if git(path, 'rev-parse', 'HEAD:sentinel-state.json') != old_blob:
                    raise ValueError('BASE_BLOB_CHANGED')
                Path(path, 'sentinel-state.json').write_text(serialized)
                git(path, 'add', '--', 'sentinel-state.json')
                git(path, '-c', 'user.name=sentinel-shadow-runtime', '-c', 'user.email=sentinel@localhost', 'commit', '-m', 'state: Sentinel evidence scan ' + pending['run_id'])
                head = git(path, 'rev-parse', 'HEAD')
                if attempt_hook:
                    attempt_hook(attempt)
                push = subprocess.run(['git', '-C', path, 'push', 'origin', 'HEAD:main'], capture_output=True, text=True)
                if push.returncode:
                    git(root, 'fetch', 'origin', 'main')
                    if git(root, 'rev-parse', 'origin/main') == base:
                        raise RuntimeError('GITHUB_WRITE_FAILED: ' + push.stderr[-500:])
                    continue
                git(root, 'fetch', 'origin', 'main')
                if readback_hook:
                    readback_hook()
                if subprocess.run(['git', '-C', str(root), 'merge-base', '--is-ancestor', head, 'origin/main']).returncode:
                    raise ValueError('READBACK_COMMIT_NOT_ANCESTOR')
                reread = git(root, 'show', 'origin/main:sentinel-state.json') + '\n'
                if reread != serialized:
                    raise ValueError('READBACK_CONTENT_MISMATCH')
                return {'verified': True, 'idempotent': False, 'head': head, 'state': merged}
            finally:
                git(root, 'worktree', 'remove', '--force', path)
    raise RuntimeError('CONCURRENCY_EXHAUSTED')

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default='.')
    parser.add_argument('--preview', action='store_true')
    args = parser.parse_args()
    root = Path(args.root)
    git(root, 'fetch', 'origin', 'main')
    previous = json.loads(git(root, 'show', 'origin/main:sentinel-state.json'))
    for name in ('portfolio-state.json', 'decision-journal.json', 'automation-logic-final.md'):
        git(root, 'show', 'origin/main:' + name)
    mutation = scan(previous, collect())
    print('SENTINEL_SCAN', json.dumps({key: mutation[key] for key in ('run_id', 'run_status', 'data_gaps', 'market_data_asof', 'derivatives_data_asof')}))
    if args.preview:
        print(json.dumps(mutation, indent=2))
        return
    registry = json.loads(git(root, 'show', 'origin/main:sentinel-runtime.json'))
    if registry.get('writer') != 'VULTR_SYSTEMD' or registry.get('stage') != 'ACTIVE':
        raise RuntimeError('SINGLE_WRITER_ADMISSION_DENIED')
    result = persist(root, mutation, required_registry=registry)
    print('SENTINEL_MAIN_READBACK_OK', result['state']['run_id'], result['head'], 'PERSIST_VERIFIED=TRUE')

if __name__ == '__main__':
    main()
