"""Isolated staged V2 user exit. No I/O, orders, scheduler or publisher.

NOT a production execution entry point. The returned copy must only be consumed
by a reviewed SingleWriter transaction with fresh acquisition and CAS/readback.
"""
import copy
import datetime as dt
import hashlib
import json
import math
import re

from research import hunter_lifecycle_state as lifecycle
from research import hunter_shadow_trader_v2 as eng

REQUEST_ID = 'Sentinel_035030c90a0881919c51c31e1ac68700'
REQUESTED_AT = '2026-10-10T22:04:13.099124Z'
REASON = 'USER_REQUESTED_MANUAL_STOP_LOSS'
USER_TEXT = '�ǾͰ�ENA��PENDLE���ھ���������ֹ�𣬼�¼��ԭ�򣬲�Ҫһֱ���ţ�Ȼ��Ϊ����ĩ�ĸ�����׼��'
TARGETS = {
    'ENA': ('SHV2-20261005T075147-ENA-b10cd5', 0.2527, 5.313),
    'PENDLE': ('SHV2-20261005T075147-PENDLE-ea9b45', 2.509, 4.433),
}


def request():
    return dict(request_id=REQUEST_ID, requested_at_utc=REQUESTED_AT,
                reason=REASON, user_text=USER_TEXT,
                targets={a: row[0] for a, row in TARGETS.items()},
                capital_authority='NONE_SHADOW_ONLY',
                real_trading_enabled=False, real_order_count=0)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def event_id(asset):
    return 'MANUAL_V2_' + hashlib.sha256(
        (REQUEST_ID + '|' + TARGETS[asset][0] + '|SELL').encode()).hexdigest()


def validate_position(pos, asset):
    shadow_id, entry, slip = TARGETS[asset]
    require(pos.get('shadow_id') == shadow_id and pos.get('asset') == asset,
            'POSITION_IDENTITY_CHANGED')
    require((pos.get('execution_venue'), pos.get('market_symbol'), pos.get('market_type'))
            == ('BINANCE_SPOT', asset + 'USDT', 'spot'), 'EXECUTION_IDENTITY_CHANGED')
    require(pos.get('execution_fee_bps') == eng.FEE_BPS == 10, 'FEE_MODEL_CHANGED')
    tranches = pos.get('tranches') or []
    require(len(tranches) == 1, 'AUTHORIZED_QUANTITY_CHANGED')
    require(all(tranches[0].get(k) == v for k, v in
                {'notional_usdt': 1000, 'price': entry, 'buy_slippage_bps': slip}.items()),
            'AUTHORIZED_QUANTITY_CHANGED')
    require(pos.get('capital_authority') == 'NONE_SHADOW_ONLY', 'POSITION_AUTHORITY')


def stage(state, instruction, books, now, source_main_sha):
    """Return (staged_copy, audit); caller state never changes, even on failure.

    One invalid book may block one target while staging the other. Durable
    receipts live with portfolio state, independent of monitor generations and
    event truncation. Retry only uncompleted targets after reloading main.
    """
    require(instruction == request(), 'REQUEST_SCOPE_MISMATCH')
    require(isinstance(source_main_sha, str) and re.fullmatch('[0-9a-f]{40}', source_main_sha),
            'SOURCE_MAIN_SHA_REQUIRED')
    require(now.tzinfo is not None and now >= eng.parse(REQUESTED_AT), 'INVALID_EXECUTION_TIME')
    require(state.get('schema') == 'hunter_shadow_v2_portfolio_v2' and
            state.get('mode') == 'SIMULATION_ONLY_NO_REAL_ORDERS', 'SHADOW_STATE_REQUIRED')
    require(eng.EVENT_PREFIX == 'SHADOW_V2' and eng.STRATEGY_ID == 'CAPITAL_DECISION_ENGINE_V2'
            and eng.CAPITAL_POOL_USDT == 20000, 'V2_ENGINE_REQUIRED')
    require(state.get('real_trading_enabled', False) is False and
            type(state.get('real_order_count', 0)) is int and state.get('real_order_count', 0) == 0,
            'REAL_ORDER_INVARIANT')
    require(all(isinstance(state.get(k), list) for k in
                ('open_positions', 'closed_positions', 'events', 'decisions')), 'STATE_LISTS_REQUIRED')
    ids = [p.get('shadow_id') for p in state['open_positions']]
    require(len(ids) == len(set(ids)), 'DUPLICATE_OPEN_ID')
    require(all(str(p.get('shadow_id', '')).startswith('SHV2-') for p in state['open_positions']),
            'V2_POSITIONS_REQUIRED')
    result = copy.deepcopy(state)
    prior = result.get('manual_exit_requests', {}).get(REQUEST_ID)
    fingerprint = digest(instruction)
    require(not prior or prior.get('request_sha256') == fingerprint, 'REQUEST_ID_COLLISION')
    receipts = copy.deepcopy((prior or {}).get('receipts', {}))
    rows = []
    generation = 'MANUAL_' + REQUEST_ID
    for asset, (sid, _, _) in TARGETS.items():
        opened = [p for p in result['open_positions'] if p.get('shadow_id') == sid]
        closed = [p for p in result['closed_positions'] + result.get('closed_trade_archive', [])
                  if p.get('shadow_id') == sid]
        sells = [e for e in result['events'] if e.get('shadow_id') == sid
                 and e.get('type') == 'SHADOW_V2_SELL']
        if asset in receipts:
            saved = receipts[asset]
            require(not opened and len(closed) == 1 and len(sells) <= 1 and
                    saved.get('event_id') == event_id(asset) and
                    saved.get('request_sha256') == fingerprint and
                    closed[0].get('manual_request_id') == REQUEST_ID and
                    closed[0].get('manual_event_id') == event_id(asset) and
                    (not sells or sells[0].get('event_id') == event_id(asset)),
                    'IDEMPOTENCY_STATE_INCONSISTENT')
            rows.append(dict(asset=asset, status='ALREADY_APPLIED', receipt=saved))
            continue
        require(not sells or not opened, 'OPEN_ALREADY_SOLD')
        if not opened:
            rows.append(dict(asset=asset, status='NOT_OPEN_NO_ACTION', shadow_id=sid))
            continue
        require(not closed, 'OPEN_CLOSED_ID_COLLISION')
        pos = opened[0]
        validate_position(pos, asset)
        book = books.get(asset, {})
        if not lifecycle.fresh(book.get('fetched_at'), now, seconds=30):
            rows.append(dict(asset=asset, status='BLOCKED', reason='BOOK_STALE_OR_MISSING'))
            continue
        if eng.parse(book['fetched_at']) < eng.parse(REQUESTED_AT):
            rows.append(dict(asset=asset, status='BLOCKED', reason='PRE_REQUEST_BOOK'))
            continue
        execution = lifecycle.liquidation(pos, book, now, pos['execution_fee_bps'])
        if execution.get('status') != 'SHADOW_RECEIPT_ESTIMATE':
            rows.append(dict(asset=asset, status='BLOCKED', reason=execution.get('reason')))
            continue
        require(all(math.isfinite(execution[k]) for k in ('quantity', 'vwap', 'capital', 'net_pnl_usdt')),
                'NONFINITE_EXECUTION')
        pnl, price, capital = execution['net_pnl_usdt'], execution['vwap'], execution['capital']
        receipt = dict(request_id=REQUEST_ID, requested_at_utc=REQUESTED_AT,
                       executed_at_utc=now.isoformat(), request_sha256=fingerprint,
                       event_id=event_id(asset), shadow_id=sid, reason=REASON,
                       source_main_sha=source_main_sha, before_portfolio_sha256=digest(state),
                       raw_book_sha256=digest(book), raw_book=copy.deepcopy(book),
                       execution=copy.deepcopy(execution), capital_authority='NONE_SHADOW_ONLY',
                       real_trading_enabled=False, real_order_count=0)
        pos.update(closed_at_utc=now.isoformat(), exit_reference_price=price,
                   exit_reference_basis='FULL_QUANTITY_BID_VWAP', exit_reason=REASON,
                   weighted_entry_price=eng.weighted_entry(pos), total_notional_usdt=capital,
                   net_pnl_usdt=round(pnl, 2), net_return_pct=round(pnl/capital*100, 4),
                   btc_return_pct=None, btc_relative_return_pct=None,
                   btc_relative_return_status='NOT_OBSERVED_FOR_MANUAL_EXIT',
                   exit_execution_estimate=execution, manual_request_id=REQUEST_ID,
                   manual_event_id=event_id(asset), manual_exit_receipt=receipt)
        pos.update(eng.exit_analysis(pos, price, REASON))
        eng.refresh_post_exit_status(pos, now)
        eng.update_loss_exit_guard(result, pnl, REASON, now, capital)
        eng.register_exit_for_reentry(result, pos, price, REASON, now)
        eng.record(result, pos, 'EXIT', now, [REASON], receipt, price)
        eng.trade_event(result, pos, 'SELL', now, price, REASON, pnl)
        result['events'][-1].update(event_id=event_id(asset), request_id=REQUEST_ID,
                                   requested_at_utc=REQUESTED_AT, generation_id=generation,
                                   execution_quantity=execution['quantity'],
                                   execution_receipt=copy.deepcopy(receipt))
        result['closed_positions'].append(pos)
        result['open_positions'] = [p for p in result['open_positions'] if p.get('shadow_id') != sid]
        receipts[asset] = receipt
        rows.append(dict(asset=asset, status='STAGED_NOT_PUBLISHED', receipt=receipt))
    staged = sum(row['status'] == 'STAGED_NOT_PUBLISHED' for row in rows)
    if staged:
        result.setdefault('manual_exit_requests', {})[REQUEST_ID] = dict(
            request_sha256=fingerprint, instruction=copy.deepcopy(instruction), receipts=receipts)
        result['updated_at_utc'] = now.isoformat()
    # Never modify last_cycle_generation_id or active_observation_generation_id:
    # neither scheduler completion nor a natural monitor cycle occurred here.
    audit = dict(status='NOT_PUBLISHED', request_id=REQUEST_ID, source_main_sha=source_main_sha,
                 before_sha256=digest(state), staged_sha256=digest(result), targets=rows,
                 staged_count=staged, capital_authority='NONE_SHADOW_ONLY',
                 real_trading_enabled=False, real_order_count=0)
    return result, audit
