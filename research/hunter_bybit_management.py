"""Bounded, read-only primary Bybit spot evidence for existing V2 positions.

No universe scan, entry decision, portfolio write, private API or order endpoint.
"""
import copy
import datetime as dt
import json
import math
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

from research.hunter_bybit_signal_capture import features
from research.hunter_early_signals import score_row
from research.hunter_execution_cost import estimate
from research.hunter_liquidity_probe import measure
from research.hunter_lifecycle_state import fresh, liquidation
from research.hunter_policy import stamp

PUBLIC_PATHS = {'/v5/market/tickers', '/v5/market/kline', '/v5/market/orderbook'}


def request(path, params):
    if path not in PUBLIC_PATHS or params.get('category') != 'spot':
        raise ValueError('PUBLIC_SPOT_MARKET_ENDPOINT_REQUIRED')
    req = urllib.request.Request('https://api.bybit.com'+path+'?'+urllib.parse.urlencode(params),
        headers={'User-Agent': 'hunter-bybit-held-monitor/1', 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=12) as response:
        data = json.load(response)
    if data.get('retCode') != 0:
        raise ValueError('BYBIT_PUBLIC_RESPONSE_ERROR')
    return data


def source_fresh(value, now):
    try:
        age = now.timestamp()-float(value)/1000
        return math.isfinite(age) and 0 <= age <= 600
    except (ValueError, TypeError, OverflowError):
        return False


def candles(body, symbol, interval, count, now):
    result = body.get('result') or {}
    if (result.get('category') != 'spot' or result.get('symbol') != symbol
            or not source_fresh(body.get('time'), now)):
        raise ValueError('BYBIT_SIGNAL_IDENTITY_OR_TIME_INVALID')
    rows = sorted(result.get('list') or [], key=lambda r:int(r[0]))[-count:]
    period = int(interval)*60*1000
    expected = int(now.timestamp()*1000)//period*period
    if len(rows) != count or int(rows[-1][0]) != expected:
        raise ValueError('BYBIT_KLINES_STALE_OR_MISSING')
    if any(int(r[0]) != expected-(count-1-i)*period for i,r in enumerate(rows)):
        raise ValueError('BYBIT_KLINES_GAPPED_OR_DUPLICATE')
    for row in rows:
        if len(row)<7 or any(not math.isfinite(float(v)) for v in row[:7]):
            raise ValueError('BYBIT_KLINES_INVALID')
        o,h,l,c = map(float,row[1:5])
        if min(o,h,l,c)<=0 or not l<=min(o,c)<=max(o,c)<=h or min(map(float,row[5:7]))<0:
            raise ValueError('BYBIT_KLINES_INVALID')
    return rows


def collect(positions, review, generation, fetcher=request, clock=None):
    clock = clock or (lambda:dt.datetime.now(dt.timezone.utc))
    wanted = [p for p in positions if p.get('execution_venue')=='BYBIT_SPOT']
    packets = {}; failures = {}
    if not wanted:
        return packets, failures
    candidates = {c['asset']:c for c in review.get('candidates',[]) if c.get('asset')}

    def signal(symbol):
        hourly = fetcher('/v5/market/kline', dict(category='spot',symbol=symbol,interval='60',limit=5))
        quarter = fetcher('/v5/market/kline', dict(category='spot',symbol=symbol,interval='15',limit=25))
        at = clock()
        return features(candles(hourly,symbol,'60',5,at),candles(quarter,symbol,'15',25,at),symbol,at)

    try:
        btc1,btc4,btc_micro = signal('BTCUSDT')
    except Exception as exc:
        return {}, {p.get('market_symbol',p['asset']):'BYBIT_BTC_EVIDENCE_UNAVAILABLE:'+type(exc).__name__ for p in wanted}

    def one(pos):
        symbol = pos.get('market_symbol')
        fee = pos.get('execution_fee_bps')
        if symbol != pos['asset']+'USDT' or pos.get('market_type')!='spot':
            raise ValueError('POSITION_EXECUTION_IDENTITY_MISMATCH')
        if fee is None or not math.isfinite(float(fee)) or not 0<=float(fee)<=100:
            raise ValueError('BYBIT_FEE_MODEL_UNKNOWN')
        ticker = fetcher('/v5/market/tickers',dict(category='spot',symbol=symbol))
        result = ticker.get('result') or {}; rows = result.get('list') or []
        if result.get('category')!='spot' or len(rows)!=1 or rows[0].get('symbol')!=symbol:
            raise ValueError('BYBIT_TICKER_IDENTITY_INVALID')
        price = float(rows[0]['lastPrice'])
        if not math.isfinite(price) or price<=0 or not source_fresh(ticker.get('time'),clock()):
            raise ValueError('BYBIT_TICKER_STALE_OR_INVALID')
        r1,r4,micro = signal(symbol)
        row = score_row(symbol,pos['asset'],{symbol:r1},{symbol:r4},btc1,btc4,{symbol:micro,'BTCUSDT':btc_micro})
        depth = fetcher('/v5/market/orderbook',dict(category='spot',symbol=symbol,limit=200))
        raw = depth.get('result') or {}; at = clock()
        if raw.get('s')!=symbol or not source_fresh(raw.get('ts'),at):
            raise ValueError('BYBIT_DEPTH_IDENTITY_OR_TIME_INVALID')
        snapshot = measure({'bids':raw['b'],'asks':raw['a']},at,pair=symbol)
        snapshot['raw_book_evidence'] = dict(schema='bybit_spot_rest_depth_receipt_v1',
            exchange='bybit',market='spot',symbol=symbol,price_unit='USDT',quantity_unit='BASE',
            fetched_at=at.isoformat(),source_timestamp=raw['ts'],last_update_id=raw.get('u'),
            bids=copy.deepcopy(raw['b']),asks=copy.deepcopy(raw['a']),execution_verified=False)
        execution = liquidation(pos,snapshot['raw_book_evidence'],at,fee)
        if execution['status']!='SHADOW_RECEIPT_ESTIMATE':
            raise ValueError('BYBIT_FULL_QUANTITY_EXECUTION_UNKNOWN')
        candidate = copy.deepcopy(candidates.get(pos['asset'],{'asset':pos['asset'],'blockers':[]}))
        candidate.update(signal=row,signal_evidence=dict(stamp(pos['asset'],generation,at.isoformat()),
            execution_venue='BYBIT_SPOT',market_symbol=symbol),management_only=True)
        try:
            candidate['execution_scenario'] = estimate({'bids':raw['b'],'asks':raw['a']},3000,fee_bps=fee)
        except ValueError:
            candidate['execution_scenario'] = {}  # ADD unknown does not erase held-quantity protection.
        market_flags={'LIVE_ORDERBOOK_MISSING','LIVE_ORDERBOOK_STALE','LIVE_ORDERBOOK_INVALID','SPREAD_EXCEEDS_50_BPS','DEPTH_BELOW_30K_USDT','BTC_RELATIVE_SIGNAL_MISSING','SIGNAL_EVIDENCE_STALE'}
        candidate['blockers']=[b for b in candidate.get('blockers',[]) if b not in market_flags]
        return symbol,dict(execution_venue='BYBIT_SPOT',market_symbol=symbol,generation_id=generation,
            observed_at_utc=at.isoformat(),market_source_timestamp=ticker['time'],
            market_timestamp_quality='BYBIT_RESPONSE_TIME_NOT_TRADE_EVENT',
            market=dict(reference_price=price),candidate=candidate,liquidity=snapshot,
            fee_bps=fee,capital_authority='NONE_SHADOW_ONLY')

    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs={pool.submit(one,p):p for p in wanted}
        for future in as_completed(jobs):
            pos=jobs[future];symbol=pos.get('market_symbol',pos['asset'])
            try:
                symbol,packet=future.result();packets[symbol]=packet
            except Exception as exc:
                failures[symbol]=type(exc).__name__+':'+str(exc)[:120]
    return packets,failures


def management_context(pos, scan, now):
    """Reject old/mixed evidence before any mark, health or lifecycle mutation."""
    packet=((scan.get('venue_management') or {}).get('BYBIT_SPOT') or {}).get(pos.get('market_symbol'))
    if not packet or packet.get('execution_venue')!='BYBIT_SPOT' or packet.get('market_symbol')!=pos['asset']+'USDT':
        raise ValueError('PRIMARY_VENUE_MANAGEMENT_EVIDENCE_UNAVAILABLE')
    if packet.get('generation_id')!=scan.get('generation_id') or not fresh(packet.get('observed_at_utc'),now):
        raise ValueError('PRIMARY_VENUE_MANAGEMENT_GENERATION_OR_TIME_INVALID')
    if not source_fresh(packet.get('market_source_timestamp'),now):
        raise ValueError('PRIMARY_VENUE_MARK_STALE')
    price=float(packet['market']['reference_price'])
    if not math.isfinite(price) or price<=0:
        raise ValueError('PRIMARY_VENUE_MARK_INVALID')
    previous=pos.get('last_primary_venue_market_source_timestamp')
    if previous is not None and float(packet['market_source_timestamp'])<=float(previous):
        raise ValueError('PRIMARY_VENUE_MARK_DUPLICATE_OR_OLD')
    if pos.get('last_marked_at_utc') and dt.datetime.fromisoformat(packet['observed_at_utc'])<=dt.datetime.fromisoformat(pos['last_marked_at_utc']):
        raise ValueError('PRIMARY_VENUE_OBSERVATION_NOT_NEWER')
    candidate=packet['candidate'];meta=candidate.get('signal_evidence') or {}
    if (candidate.get('asset')!=pos['asset'] or meta.get('execution_venue')!='BYBIT_SPOT'
            or meta.get('market_symbol')!=pos.get('market_symbol') or meta.get('generation_id')!=scan.get('generation_id')
            or not fresh(meta.get('observed_at_utc'),now)):
        raise ValueError('PRIMARY_VENUE_SIGNAL_IDENTITY_INVALID')
    fee=pos.get('execution_fee_bps')
    if fee is None or packet.get('fee_bps')!=fee:
        raise ValueError('BYBIT_FEE_MODEL_UNKNOWN_OR_MISMATCH')
    snapshot=packet['liquidity']
    if not fresh(snapshot.get('as_of_utc'),now):raise ValueError('PRIMARY_VENUE_LIQUIDITY_STALE')
    execution=liquidation(pos,snapshot.get('raw_book_evidence',{}),now,fee)
    if execution['status']!='SHADOW_RECEIPT_ESTIMATE':
        raise ValueError('PRIMARY_VENUE_FULL_DEPTH_UNAVAILABLE')
    return packet
