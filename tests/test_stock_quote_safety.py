"""Quote data admissibility regressions; V3 thresholds are not retuned."""
import copy
import json
from datetime import datetime, time, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from research.stock_shadow import state_safety as safety
from research.stock_shadow import stock_position_monitor as monitor
from research.stock_shadow import stock_shadow_v1 as full

STAMP = '2026-10-08T17:00:00+00:00'
CHECKED = datetime.fromisoformat(STAMP)
FRESH = {'price': 104.0, 'price_asof': '2026-10-08T16:35:00Z', 'source': 'test'}


def book(engine, monkeypatch):
    position = {'symbol': 'QRVO', 'opened_at': '2026-10-05T16:09:39+00:00',
                'tranches': [{'at': '2026-10-05T16:09:39+00:00', 'price': 100.0,
                             'notional': 1000.0, 'reason': 'SELECTIVE_ENTRY_V1'}],
                'last_price': 104.0, 'last_at': '2026-10-05T19:56:00Z',
                'net_pnl_usdt': 35.92, 'net_return_pct': 3.592,
                'mfe_net_pct': 10.0, 'mae_net_pct': -0.4,
                'rebound_exit_pending_v3': {'armed_at': 'old', 'lowest_net_return_pct': -5.0}}
    store = {engine.STATE: {'positions': {'QRVO': position}, 'closed': []},
             engine.EVENTS: [{'type': 'BUY', 'symbol': 'QRVO', **copy.deepcopy(position['tranches'][0])}]}
    monkeypatch.setattr(engine, 'load', lambda path, default: copy.deepcopy(store.get(path, default)))
    monkeypatch.setattr(engine, 'save', lambda path, data: store.__setitem__(path, copy.deepcopy(data)))
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return CHECKED.astimezone(tz or timezone.utc)
    monkeypatch.setattr(engine, 'datetime', Clock)
    monkeypatch.setattr(engine, 'now', lambda: STAMP)
    monkeypatch.setattr(engine, '_alpaca_exchange_session', lambda ts=None:
                        {'date': '2026-10-08', 'open': time(9, 30), 'close': time(16)})
    return store


@pytest.mark.parametrize('observation,kind,reason', [
    (None, '5Min', 'MISSING_QUOTE'),
    ({'price': 104}, '5Min', 'MISSING_OR_INVALID_TIMESTAMP'),
    ({**FRESH, 'price': float('nan')}, '5Min', 'INVALID_PRICE'),
    ({**FRESH, 'price': float('inf')}, '5Min', 'INVALID_PRICE'),
    ({**FRESH, 'price': 0}, '5Min', 'INVALID_PRICE'),
    ({**FRESH, 'price': -10}, '5Min', 'INVALID_PRICE'),
    ({**FRESH, 'price': True}, '5Min', 'INVALID_PRICE'),
    ({**FRESH, 'price_asof': '2026-10-02T19:55:00Z'}, '5Min', 'STALE_SESSION'),
    ({**FRESH, 'price_asof': '2026-10-08T16:00:00Z'}, '5Min', 'STALE_BAR'),
    ({**FRESH, 'price_asof': '2026-10-08T17:05:00Z'}, '5Min', 'FUTURE_TIMESTAMP'),
    ({**FRESH, 'price_asof': '2026-10-08T16:35:00'}, '5Min', 'MISSING_OR_INVALID_TIMESTAMP'),
    ({**FRESH, 'price_asof': '2026-10-02T04:00:00Z', 'refresh_received': True}, '1Day', 'STALE_SESSION'),
    ({**FRESH, 'price_asof': '2026-10-08T04:00:00Z', 'refresh_received': False}, '1Day', 'DAILY_REFRESH_UNCONFIRMED'),
    (FRESH, '5Min', 'VALID'),
    ({**FRESH, 'price_asof': '2026-10-08T04:00:00Z', 'refresh_received': True}, '1Day', 'VALID'),
])
def test_admissibility(observation, kind, reason):
    assert safety.quote_validity('AAPL', observation, CHECKED, kind) == reason


def test_corporate_action_effective_date_and_no_symbol_alias():
    before = datetime.fromisoformat('2026-10-02T17:00:00+00:00')
    assert safety.security_block('QRVO', before) is None
    assert safety.security_block('QRVO', CHECKED)['successor_symbol'] == 'SWKS'
    assert safety.quote_validity('QRVO', FRESH, CHECKED, '5Min') == 'CORPORATE_ACTION_HALTED'
    assert safety.quote_validity('SWKS', FRESH, CHECKED, '5Min') == 'VALID'


@pytest.mark.parametrize('observation', [None, FRESH])
def test_monitor_preserves_qrvo_and_ledger_even_if_provider_returns_price(monkeypatch, observation):
    store = book(monitor, monkeypatch)
    before = copy.deepcopy(store)
    monkeypatch.setattr(monitor, 'alpaca_snapshot_quotes', lambda symbols:
                        ({'QRVO': observation} if observation else {}, [], 1, 1))
    monitor.main(force=True)
    result = store[monitor.STATE]['positions']['QRVO']
    for key, value in before[monitor.STATE]['positions']['QRVO'].items():
        assert result[key] == value
    assert result['quote_actions_allowed'] is False
    assert result['valuation_status'] == 'UNAVAILABLE_LAST_KNOWN_ONLY'
    assert store[monitor.EVENTS] == before[monitor.EVENTS]
    assert store[monitor.STATE]['closed'] == []
    assert store[monitor.HEALTH]['status'] == 'DATA_UNAVAILABLE'
    assert store[monitor.HEALTH]['invalid_quotes']['QRVO'] == 'CORPORATE_ACTION_HALTED'
    summary = store[monitor.ROOT/'summary-v1.json']
    assert summary['unrealized_net_pnl_usdt'] is None
    assert summary['unvalued_symbols'] == ['QRVO']


@pytest.mark.parametrize('observation,reason', [
    (None, 'MISSING_QUOTE'),
    ({**FRESH, 'price_asof': '2026-10-02T19:55:00Z'}, 'STALE_SESSION'),
    ({**FRESH, 'price_asof': '2026-10-08T16:00:00Z'}, 'STALE_BAR'),
    ({**FRESH, 'price': float('nan')}, 'INVALID_PRICE'),
])
def test_generic_bad_quote_never_sells_or_updates_extrema(monkeypatch, observation, reason):
    store = book(monitor, monkeypatch)
    position = store[monitor.STATE]['positions'].pop('QRVO')
    position['symbol'] = 'AAPL'
    store[monitor.STATE]['positions']['AAPL'] = position
    before = copy.deepcopy(store)
    monkeypatch.setattr(monitor, 'alpaca_snapshot_quotes', lambda symbols:
                        ({'AAPL': observation} if observation else {}, [], 1, 1))
    monitor.main(force=True)
    result = store[monitor.STATE]['positions']['AAPL']
    assert result['quote_status'] == reason
    for key, value in position.items():
        assert result[key] == value
    assert store[monitor.EVENTS] == before[monitor.EVENTS]


@pytest.mark.parametrize('symbol,status', [('QRVO','CORPORATE_ACTION_HALTED'), ('AAPL','STALE_SESSION')])
def test_full_scan_stale_cache_cannot_buy_add_sell_or_rewrite_book(monkeypatch, symbol, status):
    store = book(full, monkeypatch)
    position = store[full.STATE]['positions'].pop('QRVO')
    position['symbol'] = symbol
    store[full.STATE]['positions'][symbol] = position
    before = copy.deepcopy(store)
    stale = {'base': symbol, 'price': 114.17, 'price_asof': '2026-10-02T04:00:00Z', 'refresh_received': True}
    monkeypatch.setattr(full, 'stock_universe', lambda: ({symbol: stale, 'NEW': {**stale, 'base': 'NEW'}}, [],
                        {'discovered': 2, 'source_errors': []}))
    # A valid gate must reject data before any V3 engine decisions are evaluated.
    monkeypatch.setattr(full, 'entry_decision', lambda *args: pytest.fail('stale decision evaluated'))
    full.main()
    result = store[full.STATE]['positions'][symbol]
    for key, value in position.items():
        assert result[key] == value
    assert result['quote_status'] == status
    assert store[full.EVENTS] == before[full.EVENTS]
    assert store[full.STATE]['closed'] == []
    assert list(store[full.STATE]['positions']) == [symbol]


def test_transport_pagination_keeps_bar_time_and_newest_price(monkeypatch):
    monkeypatch.setenv('APCA_API_KEY_ID', 'test')
    monkeypatch.setenv('APCA_API_SECRET_KEY', 'test')
    pages = iter([
        {'bars': {'AAPL': [{'c': 103.0, 't': '2026-10-08T16:30:00Z'}]}, 'next_page_token': 'next'},
        {'bars': {'AAPL': [{'c': 104.0, 't': '2026-10-08T16:35:00Z'}]}, 'next_page_token': None}])
    urls = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return json.dumps(next(pages)).encode()
    def get(request, **kwargs):
        urls.append(request.full_url)
        return Response()
    monkeypatch.setattr(monitor.urllib.request, 'urlopen', get)
    quotes, errors, requests, batches = monitor.alpaca_snapshot_quotes(['AAPL', 'QRVO'])
    assert quotes['AAPL']['price'] == 104
    assert quotes['AAPL']['price_asof'] == '2026-10-08T16:35:00Z'
    assert 'QRVO' not in quotes
    assert requests == 2 and batches == 1 and errors == []
    assert parse_qs(urlparse(urls[1]).query)['page_token'] == ['next']


def test_valid_quote_recovers_coverage_without_rewriting_historical_cost(monkeypatch):
    store = book(monitor, monkeypatch)
    qrvo = copy.deepcopy(store[monitor.STATE]['positions']['QRVO'])
    apple = copy.deepcopy(qrvo)
    apple.update(symbol='AAPL', quote_status='MISSING_QUOTE', mfe_net_pct=0.0)
    store[monitor.STATE]['positions']['AAPL'] = apple
    before = copy.deepcopy(store)
    monkeypatch.setattr(monitor, 'alpaca_snapshot_quotes', lambda symbols: ({'AAPL': FRESH}, [], 1, 1))
    monitor.main(force=True)
    result = store[monitor.STATE]['positions']['AAPL']
    assert result['quote_status'] == 'VALID'
    assert result['price_asof'] == FRESH['price_asof']
    assert result['tranches'] == apple['tranches']
    assert store[monitor.EVENTS] == before[monitor.EVENTS]
    assert store[monitor.HEALTH]['status'] == 'PARTIAL'
    assert store[monitor.HEALTH]['positions_updated'] == 1
    summary = store[monitor.ROOT/'summary-v1.json']
    assert summary['unrealized_net_pnl_usdt'] is None
    assert summary['unrealized_net_pnl_valued_subset_usdt'] == result['net_pnl_usdt']
    assert summary['realized_net_pnl_usdt'] == 0
