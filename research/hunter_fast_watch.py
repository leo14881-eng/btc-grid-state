"""Phase 1 market observation only. Never admits mutations to Hunter's SSOT.

BookTicker has NO exchange timestamp. Receipt freshness and update-id ordering
are explicit; aggTrade timestamps are independent evidence, not borrowed for books.
"""
import copy
import datetime as dt
import hashlib
import json
import math
import statistics
from dataclasses import dataclass

from research.hunter_lifecycle_state import liquidation, protect
from research.hunter_shadow_trader_v2 import PROTECT_ARM_PCT, GIVEBACK_MAX_PCT, MIN_PROTECTED_NET_PCT, FEE_BPS


@dataclass(frozen=True)
class Config:
    stale_seconds: float = 12
    fallback_seconds: float = 7
    unavailable_seconds: float = 60
    evidence_seconds: float = 5
    future_tolerance_seconds: float = 2
    recovery_events: int = 3
    conflict_bps: float = 50
    max_spread_bps: float = 100
    review_seconds: float = 5
    reconcile_seconds: float = 30
    save_seconds: float = 5
    rotate_seconds: float = 23 * 3600 + 50 * 60
    timeout_seconds: float = 4
    concurrency: int = 3
    weight_budget_per_minute: int = 300
    circuit_failures: int = 3
    max_backoff_seconds: float = 60
    jitter_seconds: float = 1
    max_symbols: int = 64  # fail closed; never silently truncate open holdings


def utc(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat()


def finite(value):
    n = float(value)
    if not math.isfinite(n) or n <= 0:
        raise ValueError('INVALID_PRICE')
    return n


class Watch:
    def __init__(self, config=None, venue='BINANCE_SPOT'):
        self.config = config or Config()
        if venue not in ('BINANCE_SPOT', 'BYBIT_SPOT'):
            raise ValueError('VENUE_INVALID')
        self.venue = venue
        self.symbols = {}
        self.positions = {}
        self.counterfactual = {}
        self.ab = {}
        self.pending = {}
        self.history = []
        self.metrics = {k: 0 for k in ('connection_count', 'reconnect_count', 'ws_recovery_count',
            'event_count', 'duplicate_drop_count', 'out_of_order_drop_count', 'invalid_drop_count',
            'stale_incident_count', 'rest_fallback_count', 'rest_fallback_requests',
            'both_sources_failed_count', 'false_fast_trigger_count', 'review_count')}
        self.connected = False
        self.started = None
        self.connection_id = None
        self.actual = set()
        self.source_sha = None
        self.measurement_started = None
        self.connected_seconds = 0
        self.latencies = []

    def record(self, kind, now, **data):
        self.history.append(dict(kind=kind, at=utc(now), **data))
        self.history = self.history[-2000:]

    def reconcile(self, portfolio, source_sha, now):
        if self.measurement_started is None:
            self.measurement_started = now
        if portfolio.get('mode') != 'SIMULATION_ONLY_NO_REAL_ORDERS':
            raise ValueError('SHADOW_BOUNDARY_INVALID')
        rows = portfolio['open_positions']
        if any(p.get('execution_venue') != self.venue or p.get('market_type') != 'spot'
               or not p.get('market_symbol') for p in rows):
            raise ValueError('PRIMARY_VENUE_IDENTITY_MISSING')
        expected = ({'BTCUSDT'} if self.venue == 'BINANCE_SPOT' else set()) | {p['market_symbol'] for p in rows}
        for market in portfolio.get('verified_reference_markets', []):
            if market.get('venue') == self.venue and market.get('market_type') == 'spot' and market.get('verified') is True:
                expected.add(market['symbol'])
        if len(expected) > self.config.max_symbols:
            raise ValueError('SUBSCRIPTION_SCOPE_EXCEEDED')
        # Position id, not symbol, owns counterfactual lifecycle (re-entry isolated).
        new = {p['shadow_id']: copy.deepcopy(p) for p in rows}
        for key, p in new.items():
            if p.get('capital_authority') != 'NONE_SHADOW_ONLY':
                raise ValueError('CAPITAL_AUTHORITY_INVALID')
            if key not in self.counterfactual:
                self.counterfactual[key] = copy.deepcopy(p)
            elif self.counterfactual[key]['tranches'] != p['tranches']:
                self.counterfactual[key] = copy.deepcopy(p)  # authoritative cashflow rebase
            a = self.ab.setdefault(key, {'asset': p['asset'], 'ws_first_arm_seen_at': None,
                'monitor_first_arm_seen_at': None, 'ws_exit_review_at': None,
                'monitor_exit_review_at': None, 'ws_peak': None, 'monitor_sampled_peak': None,
                'ws_theoretical_net_exit_pnl': None, 'monitor_theoretical_net_exit_pnl': None,
                'potential_missed_profit_window': None, 'false_fast_trigger': 0})
            life = p.get('protection_lifecycle', {})
            if life.get('armed_at_utc'):
                a['monitor_first_arm_seen_at'] = life['armed_at_utc']
            if life.get('state') == 'EXIT_TRIGGERED':
                a['monitor_exit_review_at'] = life.get('last_observed_at_utc')
                a['monitor_theoretical_net_exit_pnl'] = p.get('last_exit_estimate', {}).get('net_pnl_usdt')
            a['monitor_sampled_peak'] = p.get('holding_peak_price', p.get('last_price'))
        # Closed positions supply the monitor exit baseline without watching them.
        for p in portfolio.get('closed_positions', []):
            if p['shadow_id'] in self.ab:
                a = self.ab[p['shadow_id']]
                a['monitor_exit_review_at'] = p.get('closed_at_utc', p.get('exit_at_utc'))
                a['monitor_theoretical_net_exit_pnl'] = p.get('net_pnl_usdt')
        self.positions = new
        self.counterfactual = {k: v for k, v in self.counterfactual.items() if k in new}
        self.pending = {k: v for k, v in self.pending.items() if k in new}
        self.symbols = {s: self.symbols.get(s, {'first_seen': now, 'last_valid_event_at': None,
            'last_event_at': None, 'last_event_time': None, 'sequences': {}, 'fallback': False,
            'recovery_count': 0, 'consecutive_stale_checks': 0, 'last_rest_at': None,
            'next_probe': now, 'state': 'CONNECTING'}) for s in expected}
        self.source_sha = source_sha
        return {'subscribe': sorted(expected - self.actual), 'unsubscribe': sorted(self.actual - expected)}

    def connect(self, now):
        self.metrics['connection_count'] += 1
        self.connected = True
        self.started = now
        self.connection_id = str(self.metrics['connection_count'])
        self.actual = set()

    def disconnect(self, now):
        if self.connected and self.started is not None:
            self.connected_seconds += max(0, now-self.started)
        self.connected = False
        self.actual = set()
        self.metrics['reconnect_count'] += 1
        for row in self.symbols.values():
            row.update(state='RECONNECTING', next_probe=now, recovery_count=0)

    def rotation_due(self, now):
        return self.started is not None and now - self.started >= self.config.rotate_seconds

    def event(self, payload, now):
        if self.venue == 'BYBIT_SPOT':
            payload = normalize_bybit(payload)
        data = payload.get('data', payload)
        symbol = data.get('s')
        if symbol not in self.symbols:
            self.metrics['invalid_drop_count'] += 1
            return 'SYMBOL_IDENTITY_INVALID'
        row = self.symbols[symbol]
        row['last_event_at'] = now
        kind = data.get('e', 'bookTicker')
        try:
            if kind not in ('bookTicker', 'aggTrade'):
                raise ValueError('EVENT_TYPE_INVALID')
            if 'stream' in payload and payload['stream'] != symbol.lower() + '@' + kind:
                raise ValueError('SYMBOL_IDENTITY_INVALID')
            sequence = int(data['a'] if kind == 'aggTrade' else data['u'])
            event_time = float(data['E']) / 1000 if 'E' in data else None
            if kind == 'aggTrade' and event_time is None:
                raise ValueError('EVENT_TIME_INVALID')
            if event_time is not None and (event_time > now + self.config.future_tolerance_seconds or now - event_time > self.config.evidence_seconds):
                raise ValueError('EVENT_TIME_INVALID')
            if event_time is not None:
                self.latencies.append(max(0,(now-event_time)*1000))
                self.latencies=self.latencies[-2000:]
            prior = row['sequences'].get(kind)
            if prior is not None and sequence <= prior:
                flag = 'DEDUPLICATE' if sequence == prior else 'DROP_OLD_EVENT'
                self.metrics['duplicate_drop_count' if sequence == prior else 'out_of_order_drop_count'] += 1
                return flag
            if kind == 'aggTrade':
                if row.get('last_event_time') is not None and event_time <= row['last_event_time']:
                    self.metrics['out_of_order_drop_count'] += 1
                    return 'DROP_OLD_EVENT'
                price = finite(data['p'])
                row.update(last_event_time=event_time, trade_price=price)
                row['sequences'][kind] = sequence
                # Trade freshness does not conceal a stopped book stream.
                row['last_trade_at'] = now
                return 'TRADE_ACCEPTED'
            if event_time is not None and row.get('event_time') is not None and event_time <= row['event_time']:
                self.metrics['out_of_order_drop_count'] += 1
                return 'DROP_OLD_EVENT'
            bid, ask = finite(data['b']), finite(data['a'])
            if bid >= ask or (ask / bid - 1) * 10000 > self.config.max_spread_bps:
                raise ValueError('BOOK_INVALID')
            row['sequences'][kind] = sequence
            row.update(last_valid_event_at=now, last_bid=bid, last_ask=ask,
                event_time=event_time, received_at=utc(now), update_id=sequence,
                source=self.venue + '_WS', venue=self.venue, symbol=symbol,
                event_type=kind, timestamp_quality='EXCHANGE' if event_time else 'RECEIPT_BOUND',
                connection_id=self.connection_id, consecutive_stale_checks=0)
            row.update(venue_update_id=data.get('venue_update_id', sequence), venue_sequence=data.get('venue_sequence'))
            allow_candidate = True
            if row['fallback']:
                rest_fresh = row['last_rest_at'] is not None and now - row['last_rest_at'] <= self.config.evidence_seconds
                agrees = rest_fresh and abs(bid / row['rest_bid'] - 1) * 10000 <= self.config.conflict_bps
                row['recovery_count'] = row['recovery_count'] + 1 if agrees else 0
                row['state'] = 'WS_RECOVERY_VALIDATING'
                if not agrees:
                    row['next_probe'] = now
                    allow_candidate = False
                    row['incident'] = 'WS_REST_PRICE_CONFLICT_OR_REST_STALE'
                if row['recovery_count'] >= self.config.recovery_events:
                    row.update(fallback=False, state='FAST_PATH_HEALTHY', recovery_validated_at=utc(now))
                    self.metrics['ws_recovery_count'] += 1
                    self.record('WS_RECOVERED', now, symbol=symbol,
                        fallback_duration_seconds=now - row['fallback_started'])
            else:
                row['state'] = 'FAST_PATH_HEALTHY'
            self.metrics['event_count'] += 1
            if allow_candidate:
                self.candidate(symbol, bid, now, 'WS', sequence)
            return 'ACCEPTED'
        except (ValueError, TypeError, KeyError, OverflowError):
            self.metrics['invalid_drop_count'] += 1
            return 'EVENT_TIME_INVALID' if 'E' in data else 'EVENT_INVALID'

    def probe_due(self, now):
        due = []
        for symbol, row in self.symbols.items():
            age = now - (row['last_valid_event_at'] if row['last_valid_event_at'] is not None else row['first_seen'])
            row['stale_seconds'] = age
            if age >= self.config.stale_seconds or not self.connected or row['fallback']:
                row['consecutive_stale_checks'] += 1
                if not row.get('stale_detected_at'):
                    row['stale_detected_at'] = utc(now)
                    self.metrics['stale_incident_count'] += 1
                row['state'] = 'REST_FALLBACK_ACTIVE' if row['fallback'] else 'STALE'
                if now >= row['next_probe']:
                    due.append(symbol)
        priority = {p['market_symbol'] for p in self.counterfactual.values()
                    if p.get('protection_lifecycle', {}).get('state', 'UNARMED') != 'UNARMED'}
        return sorted(due, key=lambda s: (s not in priority, s != 'BTCUSDT', s))

    def rest(self, symbol, data, started, completed, error=None):
        row = self.symbols.get(symbol)
        if row is None:
            return 'UNSUBSCRIBED'
        self.metrics['rest_fallback_requests'] += 1
        row.update(rest_probe_started_at=utc(started), rest_probe_completed_at=utc(completed),
            next_probe=completed + self.config.fallback_seconds)
        try:
            if error or completed - started > self.config.timeout_seconds:
                raise ValueError(str(error or 'TIMEOUT'))
            if data['symbol'] != symbol:
                raise ValueError('SYMBOL_IDENTITY_INVALID')
            bid, ask = finite(data['bidPrice']), finite(data['askPrice'])
            if self.venue == 'BYBIT_SPOT' and (data.get('source_timestamp') is None or not
                0 <= completed-float(data['source_timestamp'])/1000 <= self.config.evidence_seconds):
                raise ValueError('REST_EXCHANGE_TIME_INVALID')
            if bid >= ask or (ask / bid - 1) * 10000 > self.config.max_spread_bps:
                raise ValueError('BOOK_INVALID')
            if not row['fallback']:
                row.update(fallback=True, fallback_started=completed, fallback_activated_at=utc(completed))
                self.metrics['rest_fallback_count'] += 1
                self.record('REST_TAKEOVER', completed, symbol=symbol,
                    rest_takeover_latency_ms=(completed - dt.datetime.fromisoformat(row['stale_detected_at']).timestamp()) * 1000)
            row.update(last_rest_at=completed, rest_bid=bid, rest_ask=ask,
                state='REST_FALLBACK_ACTIVE', rest_error=None)
            self.candidate(symbol, bid, completed, 'REST', utc(completed))
            return 'REST_FALLBACK_ACTIVE'
        except (ValueError, TypeError, KeyError) as exc:
            self.metrics['both_sources_failed_count'] += 1
            row.update(state='MARKET_DATA_UNAVAILABLE' if completed - row['first_seen'] >= self.config.unavailable_seconds else 'FAST_MARKET_DATA_DEGRADED', rest_error=str(exc), recovery_count=0)
            return row['state']

    def candidate(self, symbol, price, now, source, sequence):
        for key, p in self.counterfactual.items():
            if p['market_symbol'] != symbol or p.get('_observation_exited'):
                continue
            a = self.ab[key]
            a['ws_peak'] = max(a['ws_peak'] or price, price)
            n = sum(t['notional_usdt'] for t in p['tranches'])
            entry = sum(t['notional_usdt'] * t['price'] for t in p['tranches']) / n
            armed = p.get('protection_lifecycle', {}).get('state', 'UNARMED') != 'UNARMED'
            if not armed and (price / entry - 1) * 100 < PROTECT_ARM_PCT:
                continue
            if now - p.get('_last_review_at', -1e30) < self.config.review_seconds:
                continue
            event_id = hashlib.sha256(f'{key}:{source}:{sequence}'.encode()).hexdigest()
            self.pending.setdefault(key, dict(position_id=key, symbol=symbol, price=price,
                received_at=now, source=source, event_id=event_id,
                execution_venue=self.venue,
                tranche_fingerprint=hashlib.sha256(json.dumps(p['tranches'], sort_keys=True).encode()).hexdigest(),
                kind='FAST_EXIT_OR_PEAK_REVIEW' if armed else 'FAST_ARM_REVIEW'))

    def review(self, key, trigger, book, now):
        p = self.counterfactual.get(key)
        if p is None or p.get('_observation_exited'):
            return {'status': 'DROPPED_CLOSED'}
        fingerprint=hashlib.sha256(json.dumps(p['tranches'], sort_keys=True).encode()).hexdigest()
        if trigger.get('execution_venue') != self.venue or trigger.get('symbol') != p['market_symbol'] or trigger.get('tranche_fingerprint') != fingerprint:
            return {'status':'POSITION_REBOUND_REVIEW_REJECTED'}
        if now - trigger['received_at'] > self.config.evidence_seconds or now < trigger['received_at']:
            return {'status': 'STALE_TRIGGER'}
        if not book or not 0 <= now - dt.datetime.fromisoformat(book['fetched_at']).timestamp() <= self.config.evidence_seconds:
            return {'status': 'FRESH_FULL_DEPTH_REQUIRED'}
        if book.get('exchange') != ('binance' if self.venue == 'BINANCE_SPOT' else 'bybit') or book.get('symbol') != p['market_symbol']:
            return {'status': 'VENUE_MATCH_REQUIRED'}
        estimate = venue_liquidation(p, book, dt.datetime.fromtimestamp(now, dt.timezone.utc))
        if estimate['status'] == 'UNKNOWN':
            return {'status': 'FRESH_FULL_DEPTH_REQUIRED', 'execution': estimate}
        if p.get('_last_event_id') == trigger['event_id']:
            return {'status': 'DEDUPLICATE'}
        p.update(_last_review_at=now, _last_event_id=trigger['event_id'])
        result = protect(p, trigger['price'], estimate['net_pnl_usdt'], estimate,
            dt.datetime.fromtimestamp(now, dt.timezone.utc), 'FAST_OBSERVATION_' + trigger['event_id'],
            utc(trigger['received_at']), PROTECT_ARM_PCT, GIVEBACK_MAX_PCT, MIN_PROTECTED_NET_PCT)
        a = self.ab[key]
        if result['armed'] and a['ws_first_arm_seen_at'] is None:
            a['ws_first_arm_seen_at'] = utc(now)
        if result['exit']:
            p['_observation_exited'] = True  # one hypothetical exit per episode, never a ledger event
            a.update(ws_exit_review_at=utc(now), ws_theoretical_net_exit_pnl=estimate['net_pnl_usdt'])
        if not result['armed']:
            a['false_fast_trigger'] += 1
            self.metrics['false_fast_trigger_count'] += 1
        self.metrics['review_count'] += 1
        self.record('OBSERVATION_REVIEW', now, position_id=key, trigger=trigger,
            result=result, lifecycle=copy.deepcopy(p.get('protection_lifecycle')),
            formal_mutation=False)
        return dict(result, status='OBSERVATION_ONLY', formal_mutation=False)

    def snapshot(self, now):
        stale = [s for s, r in self.symbols.items() if r.get('state') != 'FAST_PATH_HEALTHY']
        fallback = [s for s, r in self.symbols.items() if r['fallback']]
        good = all((r['last_valid_event_at'] is not None and now - r['last_valid_event_at'] <= self.config.stale_seconds) or
                   (r['last_rest_at'] is not None and now - r['last_rest_at'] <= self.config.fallback_seconds + self.config.timeout_seconds)
                   for r in self.symbols.values())
        state = 'NOT_REQUIRED' if not self.symbols else 'FAST_PATH_HEALTHY' if not stale and self.connected else 'FAST_PATH_DEGRADED_REST_FALLBACK_HEALTHY' if good else 'FAST_MARKET_DATA_DEGRADED'
        for a in self.ab.values():
            for left, right, name in (('ws_first_arm_seen_at','monitor_first_arm_seen_at','arm_detection_delta_seconds'),
                                     ('ws_exit_review_at','monitor_exit_review_at','exit_review_delta_seconds')):
                a[name] = (dt.datetime.fromisoformat(a[right])-dt.datetime.fromisoformat(a[left])).total_seconds() if a[left] and a[right] else None
        report_metrics = dict(self.metrics, current_subscription_count=len(self.actual),
            ws_uptime_pct=100*(self.connected_seconds+(now-self.started if self.connected and self.started is not None else 0))/(now-self.measurement_started) if self.measurement_started is not None and now>self.measurement_started else None,
            median_price_detection_latency_ms=statistics.median(self.latencies) if self.latencies else None,
            arm_detection_improvement_seconds=None, exit_review_improvement_seconds=None,
            missed_profit_windows_5m=None, missed_profit_windows_ws=None,
            profit_protection_capture_delta_usdt=None, rest_takeover_latency_ms=None,
            measurement_status='UNKNOWN_UNTIL_PAIRED_LIVE_OBSERVATIONS')
        takeover=[h['rest_takeover_latency_ms'] for h in self.history if h['kind']=='REST_TAKEOVER']
        report_metrics['rest_takeover_latency_ms']=statistics.median(takeover) if takeover else None
        for field,name in [('arm_detection_delta_seconds','arm_detection_improvement_seconds'),('exit_review_delta_seconds','exit_review_improvement_seconds')]:
            paired=[a[field] for a in self.ab.values() if a.get(field) is not None]
            report_metrics[name]=statistics.median(paired) if paired else None
        return dict(schema='hunter_fast_watch_v1', venue=self.venue, mode='OBSERVATION_ONLY', real_order_count=0,
            real_trading_enabled=False, capital_authority='NONE_SHADOW_ONLY', formal_writer=False,
            generated_at=utc(now), source_sha=self.source_sha, status=state,
            ws_connected=self.connected, connection_started_at=utc(self.started) if self.started is not None else None,
            connection_age=now - self.started if self.started is not None else None,
            expected_symbols=sorted(self.symbols), actual_subscriptions=sorted(self.actual),
            subscription_count=len(self.actual), expected_subscription_count=len(self.symbols),
            stale_symbols=stale, rest_fallback_symbols=fallback, symbols=copy.deepcopy(self.symbols),
            metrics=report_metrics, ab=copy.deepcopy(self.ab), history=copy.deepcopy(self.history))


def normalize_bybit(payload):
    """Spot tickers omit best bid/ask: orderbook.1 snapshot is the primary quote.

    No local depth is maintained. Exchange-generated ts orders L1 snapshots,
    including legitimate idle heartbeat snapshots with the same update id.
    """
    if payload.get('type') != 'snapshot':
        return {}
    row = payload.get('data', {})
    symbol = row.get('s')
    if payload.get('topic') != 'orderbook.1.' + str(symbol):
        return {}
    try:
        return {'s': symbol, 'u': payload['ts'], 'E': payload['ts'],
                'b': row['b'][0][0], 'a': row['a'][0][0],
                'venue_update_id': row.get('u'), 'venue_sequence': row.get('seq')}
    except (KeyError, IndexError, TypeError):
        return {}


def venue_liquidation(pos, book, now):
    if pos['execution_venue'] == 'BINANCE_SPOT':
        return liquidation(pos, book, now, FEE_BPS)
    # Same held-quantity model, explicitly Bybit evidence; never relabel as Binance.
    try:
        fee = pos.get('execution_fee_bps')
        if fee is None or not math.isfinite(float(fee)) or float(fee) < 0:
            raise ValueError('BYBIT_FEE_MODEL_UNKNOWN')
        fee = float(fee)
        if book.get('exchange') != 'bybit' or book.get('market') != 'spot' or book.get('symbol') != pos['market_symbol']:
            raise ValueError('BOOK_IDENTITY_MISMATCH')
        if book.get('price_unit') != 'USDT' or book.get('quantity_unit') != 'BASE':
            raise ValueError('BOOK_UNITS_UNKNOWN')
        age = (now - dt.datetime.fromisoformat(book['fetched_at'])).total_seconds()
        exchange_age = now.timestamp() - float(book['source_timestamp']) / 1000
        if not 0 <= age <= Config().evidence_seconds or not 0 <= exchange_age <= Config().evidence_seconds:
            raise ValueError('BOOK_STALE_OR_MISSING')
        bids = [(finite(p), finite(q)) for p, q in book['bids']]
        asks = [(finite(p), finite(q)) for p, q in book['asks']]
        if not bids or not asks or bids[0][0] >= asks[0][0] or any(bids[i][0] <= bids[i+1][0] for i in range(len(bids)-1)) or any(asks[i][0] >= asks[i+1][0] for i in range(len(asks)-1)):
            raise ValueError('BOOK_INVALID')
        capital = sum(t['notional_usdt'] for t in pos['tranches'])
        quantity = sum(t['notional_usdt'] / (t['price'] * (1 + (t.get('buy_slippage_bps', 0) + fee) / 10000)) for t in pos['tranches'])
        remaining, proceeds = quantity, 0
        for price, size in bids:
            take = min(size, remaining); proceeds += take * price; remaining -= take
        if remaining > 1e-10:
            raise ValueError('FULL_QUANTITY_DEPTH_UNKNOWN')
        return dict(status='SHADOW_RECEIPT_ESTIMATE', venue='BYBIT_SPOT',
            net_pnl_usdt=proceeds * (1-fee/10000) - capital, vwap=proceeds/quantity,
            quantity=quantity, capital=capital, fee_bps=fee, fetched_at=book['fetched_at'],
            source_timestamp=book['source_timestamp'], historical_execution_verified=False)
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        return dict(status='UNKNOWN', reason=str(exc), net_pnl_usdt=None, historical_execution_verified=False)


class DualWatch:
    def __init__(self):
        self.watches = {v: Watch(venue=v) for v in ('BINANCE_SPOT', 'BYBIT_SPOT')}
        self.unroutable = []

    def reconcile(self, portfolio, sha, now):
        self.unroutable = []
        references = []
        for p in portfolio['open_positions']:
            for market in p.get('verified_spot_markets', []):
                if market.get('base_asset') == p['asset'] and market.get('quote_asset') == 'USDT' and market.get('verified') is True:
                    references.append(market)
        for venue, watch in self.watches.items():
            subset = dict(portfolio, verified_reference_markets=references, open_positions=[p for p in portfolio['open_positions']
                if p.get('execution_venue') == venue and p.get('market_symbol') and p.get('market_type') == 'spot'])
            watch.reconcile(subset, sha, now)
        self.unroutable = [dict(shadow_id=p['shadow_id'], asset=p['asset'], status='PRIMARY_VENUE_IDENTITY_MISSING')
            for p in portfolio['open_positions'] if p.get('execution_venue') not in self.watches or
            not p.get('market_symbol') or p.get('market_type') != 'spot']

    def snapshot(self, now):
        venues = {v: w.snapshot(now) for v, w in self.watches.items()}
        cross = []
        for symbol in set(self.watches['BINANCE_SPOT'].symbols) & set(self.watches['BYBIT_SPOT'].symbols):
            rows = [self.watches[v].symbols[symbol] for v in self.watches]
            if all(r.get('last_valid_event_at') is not None and now-r['last_valid_event_at'] <= Config().stale_seconds for r in rows):
                mids = [(r['last_bid']+r['last_ask'])/2 for r in rows]
                divergence = abs(mids[0]/mids[1]-1)*10000
                cross.append(dict(symbol=symbol, cross_venue_mid_divergence_bps=divergence,
                    status='CROSS_VENUE_PRICE_DIVERGENCE' if divergence > Config().conflict_bps else 'CONSISTENT', execution_substitution_allowed=False))
        return dict(schema='hunter_dual_fast_watch_v1', mode='OBSERVATION_ONLY',
            generated_at=utc(now), venues=venues, unroutable_positions=self.unroutable,
            status='PRIMARY_VENUE_IDENTITY_MISSING' if self.unroutable else
                'FAST_PATH_HEALTHY' if all(v['status'] in ('FAST_PATH_HEALTHY','NOT_REQUIRED') for v in venues.values()) else 'PARTIAL_FAST_PATH_DEGRADED',
            cross_venue=cross, real_order_count=0, real_trading_enabled=False,
            capital_authority='NONE_SHADOW_ONLY', formal_writer=False)
