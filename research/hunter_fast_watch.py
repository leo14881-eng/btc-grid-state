"""Phase 1 market observation only. Never admits mutations to Hunter's SSOT.

BookTicker has NO exchange timestamp. Receipt freshness and update-id ordering
are explicit; aggTrade timestamps are independent evidence, not borrowed for books.
"""
import base64
import copy
import datetime as dt
import gzip
import hashlib
import json
import math
import statistics
from dataclasses import dataclass

from research.hunter_lifecycle_state import liquidation, protect
from research.hunter_execution_identity import identity_fields
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
    archive_seconds: float = 60
    rotate_seconds: float = 23 * 3600 + 50 * 60
    timeout_seconds: float = 4
    concurrency: int = 3
    weight_budget_per_minute: int = 300
    circuit_failures: int = 3
    max_backoff_seconds: float = 60
    jitter_seconds: float = 1
    max_symbols: int = 64  # fail closed; never silently truncate open holdings
    model_identity_seconds: float = 600


def utc(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat()


def in_live_window(at, started_at, now):
    try:
        observed=dt.datetime.fromisoformat(at)
        started=dt.datetime.fromisoformat(started_at)
        return (observed.tzinfo is not None and started.tzinfo is not None and
                started.timestamp() <= observed.timestamp() <= now)
    except (TypeError, ValueError, OverflowError):
        return False


def finite(value):
    n = float(value)
    if not math.isfinite(n) or n <= 0:
        raise ValueError('INVALID_PRICE')
    return n


def bind_observed_model_routes(portfolio, monitor, source_sha, now, *, research_scan=None, research_liquidity=None):
    """Read-only legacy admission from actual current model liquidation receipts.

    This assigns a FORWARD_SHADOW_MODEL route on a copy, never historical entry
    venue and never an availability label. Failed/mismatched proof stays unknown.
    Identity freshness is a Monitor-window bound, not permission to reuse quotes.
    """
    result = copy.deepcopy(portfolio)
    evidence = monitor.get('evidence_refresh', {})
    generation = evidence.get('generation_id')
    active = portfolio.get('active_observation_generation_id')
    source_kind = 'POSITION_MONITOR'
    books = evidence.get('observed_exit_raw_books', {})
    source_valid = (generation == active and monitor.get('batch_endpoint') == '/api/v3/ticker/24hr'
                    and monitor.get('capital_authority') == 'NONE_SHADOW_ONLY')
    if not source_valid and research_scan is not None and research_liquidity is not None:
        generation = research_scan.get('generation_id')
        source_kind = 'HOURLY_RESEARCH'
        source_valid = (generation == active and research_liquidity.get('scan_generation_id') == generation
            and research_liquidity.get('capital_authority') == 'NONE__OFFICIAL_FACTS_AND_PORTFOLIO_GATES_SEPARATE')
        books = {asset: snapshot.get('raw_book_evidence', {}) for asset, snapshot
                 in research_liquidity.get('snapshots', {}).items()}
    admitted = []
    for p in result.get('open_positions', []):
        if p.get('execution_venue') or p.get('market_symbol') or p.get('market_type'):
            continue  # partial or explicit identity must never be overwritten
        try:
            if not source_sha or not generation or not source_valid:
                raise ValueError('MODEL_GENERATION_MISMATCH')
            book = books[p['asset']]
            if book.get('exchange') != 'binance' or book.get('market') != 'spot':
                raise ValueError('MODEL_VENUE_UNSUPPORTED')
            # Validate receipt's symbol, rather than using asset to invent a route.
            symbol = book['symbol']
            if not symbol.endswith('USDT') or symbol[:-4] != p['asset']:
                raise ValueError('MODEL_SYMBOL_IDENTITY_MISMATCH')
            if source_kind == 'HOURLY_RESEARCH':
                coin = research_scan.get('coins', {}).get(p['asset'], {})
                if coin.get('reference_venue') != 'binance' or not any(
                    pair.get('venue') == 'binance' and pair.get('pair') == symbol
                    and pair.get('base') == p['asset'] for pair in coin.get('pairs', [])):
                    raise ValueError('RESEARCH_MODEL_IDENTITY_UNKNOWN')
            fields = identity_fields(p, book, p['last_exit_estimate'],
                dt.datetime.fromtimestamp(now, dt.timezone.utc), generation, source_sha, FEE_BPS,
                source_kind=source_kind, max_age=Config().model_identity_seconds)
            if not fields:
                continue
            p.update(fields)
            admitted.append(p['shadow_id'])
        except (KeyError, ValueError, TypeError, OverflowError):
            continue
    return result, admitted


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
            fingerprint=hashlib.sha256(json.dumps(p['tranches'],sort_keys=True).encode()).hexdigest()
            previous=self.ab.get(key)
            if previous is None or previous.get('tranche_fingerprint') != fingerprint:
                if previous is not None:
                    self.record('AB_CASHFLOW_REBASE',now,position_id=key,previous_ab=copy.deepcopy(previous))
                self.ab[key]={'measurement_schema':'LIVE_WINDOW_ONLY_V1','measurement_started_at':utc(now),
                    'tranche_fingerprint':fingerprint,'asset':p['asset'],'ws_first_arm_seen_at':None,
                    'monitor_first_arm_seen_at':None,'ws_exit_review_at':None,'monitor_exit_review_at':None,
                    'ws_peak':None,'monitor_sampled_peak':None,'ws_theoretical_net_exit_pnl':None,
                    'monitor_theoretical_net_exit_pnl':None,'potential_missed_profit_window':None,
                    'false_fast_trigger':0,'baseline_monitor_armed_at_utc':p.get('protection_lifecycle',{}).get('armed_at_utc'),
                    'baseline_holding_peak_price':p.get('holding_peak_price')}
            a=self.ab[key]
            life=p.get('protection_lifecycle',{})
            if str(portfolio.get('active_observation_generation_id','')).startswith('MONITOR_'):
                if (str(life.get('armed_generation_id','')).startswith('MONITOR_') and
                    in_live_window(life.get('armed_at_utc'),a['measurement_started_at'],now)):
                    a['monitor_first_arm_seen_at']=life['armed_at_utc']
                if (life.get('state')=='EXIT_TRIGGERED' and
                    life.get('last_generation_id')==portfolio.get('active_observation_generation_id') and
                    in_live_window(life.get('last_observed_at_utc'),a['measurement_started_at'],now)):
                    a['monitor_exit_review_at']=life['last_observed_at_utc']
                    a['monitor_theoretical_net_exit_pnl']=p.get('last_exit_estimate',{}).get('net_pnl_usdt')
                if in_live_window(p.get('last_marked_at_utc'),a['measurement_started_at'],now):
                    try:
                        price=finite(p['last_price'])
                        a['monitor_sampled_peak']=max(a['monitor_sampled_peak'] or price,price)
                    except (KeyError, ValueError, TypeError):pass
        # Only subsequent Monitor closes with the same cashflow form live pairs.
        if str(portfolio.get('active_observation_generation_id','')).startswith('MONITOR_'):
            for p in portfolio.get('closed_positions',[]):
                if p['shadow_id'] in self.ab:
                    a=self.ab[p['shadow_id']]
                    fingerprint=hashlib.sha256(json.dumps(p.get('tranches'),sort_keys=True).encode()).hexdigest()
                    at=p.get('closed_at_utc',p.get('exit_at_utc'))
                    monitor_sell=any(e.get('type')=='SHADOW_V2_SELL' and
                        e.get('shadow_id')==p['shadow_id'] and e.get('at')==at and
                        e.get('reason')=='PROFIT_PROTECTION' and
                        str(e.get('generation_id','')).startswith('MONITOR_')
                        for e in portfolio.get('events',[]))
                    if (monitor_sell and fingerprint==a['tranche_fingerprint'] and
                        in_live_window(at,a['measurement_started_at'],now)):
                        a['monitor_exit_review_at']=at
                        a['monitor_theoretical_net_exit_pnl']=p.get('net_pnl_usdt')
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
                    row['last_stale_detected_at'] = row.pop('stale_detected_at', None)
            else:
                row['state'] = 'FAST_PATH_HEALTHY'
                row['last_stale_detected_at'] = row.pop('stale_detected_at', None)
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
            if not row['fallback'] and row['last_valid_event_at'] is not None and row['last_valid_event_at'] >= started:
                row.update(last_rest_at=completed, rest_bid=bid, rest_ask=ask)
                return 'REST_PROBE_COMPLETED_WS_RECOVERED'
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
            stale_since=row['last_valid_event_at'] if row['last_valid_event_at'] is not None else row['first_seen']
            row.update(state='MARKET_DATA_UNAVAILABLE' if completed - stale_since >= self.config.unavailable_seconds else 'FAST_MARKET_DATA_DEGRADED', rest_error=str(exc), recovery_count=0)
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
            prior = self.pending.get(key)
            if prior is not None and now < prior['received_at']:
                continue
            if not armed and (price / entry - 1) * 100 < PROTECT_ARM_PCT:
                self.pending.pop(key, None)
                continue
            if now - p.get('_last_review_at', -1e30) < self.config.review_seconds:
                continue
            event_id = hashlib.sha256(f'{key}:{source}:{sequence}'.encode()).hexdigest()
            # Coalesce queued work to the newest validated quote. setdefault kept
            # an expired trigger throughout slow REST probes, discarding a live
            # review window even while fresh ticks continued arriving. Replace
            # the object, so a depth review already in flight retains its own
            # event/time identity and must still pass the freshness gates.
            self.pending[key] = dict(position_id=key, symbol=symbol, price=price,
                received_at=now, source=source, event_id=event_id,
                execution_venue=self.venue,
                tranche_fingerprint=hashlib.sha256(json.dumps(p['tranches'], sort_keys=True).encode()).hexdigest(),
                kind='FAST_EXIT_OR_PEAK_REVIEW' if armed else 'FAST_ARM_REVIEW')

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
        previous_state=p.get('protection_lifecycle',{}).get('state','UNARMED')
        result = protect(p, trigger['price'], estimate['net_pnl_usdt'], estimate,
            dt.datetime.fromtimestamp(now, dt.timezone.utc), 'FAST_OBSERVATION_' + trigger['event_id'],
            utc(trigger['received_at']), PROTECT_ARM_PCT, GIVEBACK_MAX_PCT, MIN_PROTECTED_NET_PCT)
        a = self.ab[key]
        if previous_state=='UNARMED' and result['armed'] and a['ws_first_arm_seen_at'] is None:
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
            depth_receipt=depth_receipt(book, self.venue, estimate),
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


def depth_receipt(book, venue, estimate):
    """Private, lossless evidence for a hypothetical review, never a fill receipt.

    Retain the native venue payload rather than the calculation's normalized copy.
    Compression bounds runtime/history memory without discarding depth levels.
    """
    raw = json.dumps(book, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    return dict(schema='hunter_fast_depth_receipt_v1', execution_venue=venue,
        symbol=book['symbol'], fetched_at=book['fetched_at'],
        source_timestamp=book.get('source_timestamp'),
        timestamp_quality='EXCHANGE_TIMESTAMP' if book.get('source_timestamp') is not None else 'RECEIPT_BOUND',
        encoding='GZIP_BASE64_JSON', payload_sha256=hashlib.sha256(raw).hexdigest(),
        payload=base64.b64encode(gzip.compress(raw, mtime=0)).decode(),
        execution_estimate=copy.deepcopy(estimate),
        formal_execution=False, historical_execution_verified=False)


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
        # Use the same full-quantity cashflow model as the authoritative lifecycle.
        # Fast Watch retains its tighter event/depth freshness above.
        return liquidation(pos, book, now, fee)
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
