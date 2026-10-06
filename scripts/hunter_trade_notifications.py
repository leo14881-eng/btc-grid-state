"""Read-only, stable event batches for the ChatGPT Hunter notification consumer.

This module never writes a portfolio or calls an exchange. A prepared batch is
not a delivery receipt. Acknowledgements require an observed ChatGPT message.
"""
import hashlib
import datetime as dt
import json
import math

TYPES = frozenset(('SHADOW_V2_BUY', 'SHADOW_V2_ADD', 'SHADOW_V2_SELL'))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def event_id(event):
    return digest({key: event[key] for key in
                   ('type', 'at', 'asset', 'shadow_id', 'tranches')})


def verified_events(portfolio):
    if portfolio.get('mode') != 'SIMULATION_ONLY_NO_REAL_ORDERS':
        raise ValueError('SHADOW_ONLY_REQUIRED')
    if not isinstance(portfolio.get('events'), list):
        raise ValueError('EVENT_LEDGER_MISSING')
    events = {}
    for event in portfolio['events']:
        if event.get('type') not in TYPES:
            continue
        for key in ('at', 'asset', 'shadow_id'):
            if not isinstance(event.get(key), str) or not event[key]:
                raise ValueError('EVENT_IDENTITY_MISSING')
        if type(event.get('tranches')) is not int or event['tranches'] < 1:
            raise ValueError('EVENT_TRANCHE_INVALID')
        for key in ('price', 'notional_usdt'):
            value = event.get(key)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError('EVENT_AMOUNT_INVALID')
        if event['type'] == 'SHADOW_V2_SELL':
            value = event.get('net_pnl_usdt')
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError('SELL_NET_PNL_UNVERIFIED')
        identity = event_id(event)
        if identity in events and events[identity] != event:
            raise ValueError('EVENT_ID_CONTENT_CONFLICT')
        events[identity] = event
    return events


def make_batch(portfolio, acknowledged_ids, source_sha, after=None):
    """Do not advance acknowledgements or equate clock progress with delivery."""
    if not isinstance(source_sha, str) or len(source_sha) != 40:
        raise ValueError('SOURCE_SHA_REQUIRED')
    events = verified_events(portfolio)
    def instant(value):
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('EVENT_TIMESTAMP_TIMEZONE_REQUIRED')
        return parsed
    cutoff = instant(after) if after is not None else None
    acknowledged = set(acknowledged_ids)
    selected = [{'event_id': identity, **event}
                for identity, event in events.items()
                if identity not in acknowledged
                and (cutoff is None or instant(event['at']) >= cutoff)]
    selected.sort(key=lambda e: (instant(e['at']), e['event_id']))
    ids = [e['event_id'] for e in selected]
    return {'schema': 'hunter_notification_batch_v1', 'source_main_sha': source_sha,
            'batch_id': digest(ids), 'events': selected,
            'delivery_status': 'PREPARED_NOT_DELIVERED',
            'capital_authority': 'NONE_SHADOW_ONLY', 'real_trading_enabled': False}


def acknowledge(batch, observed_message):
    """The consumer must observe the previous task's real assistant message."""
    if not isinstance(observed_message, dict) or observed_message.get('role') != 'assistant':
        raise ValueError('DELIVERY_RECEIPT_REQUIRED')
    message_id = observed_message.get('message_id')
    text = observed_message.get('text', '')
    if not message_id or batch['batch_id'] not in text:
        raise ValueError('MATCHING_CHATGPT_MESSAGE_REQUIRED')
    return {'event_ids': [e['event_id'] for e in batch['events']],
            'chatgpt_message_id': message_id, 'batch_id': batch['batch_id'],
            'delivery_status': 'CHATGPT_MESSAGE_OBSERVED',
            'device_push_received': 'UNVERIFIED'}
