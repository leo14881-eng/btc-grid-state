"""Deterministic clock expiry at real Monitor ADD/SELL boundaries; no live IO."""
import copy
import datetime as dt
import io
import unittest
from contextlib import ExitStack, redirect_stdout
from unittest.mock import patch

from research import hunter_position_monitor as monitor
from research import hunter_shadow_trader_v2 as eng


class DecisionClockTests(unittest.TestCase):
    def setUp(self):
        monitor.configure_lane(False)
        self.addCleanup(monitor.configure_lane, False)

    def bybit_fixture(self):
        # Existing public-response fixture: arm, set a higher peak, then give back.
        from test_hunter_bybit_management import BybitManagementTests
        f = BybitManagementTests(); f.setUp()
        scan, _ = f.scan('arm'); f.manage(scan)
        f.now += dt.timedelta(minutes=5); f.mark = 106
        scan, _ = f.scan('peak'); f.manage(scan)
        f.now += dt.timedelta(minutes=25); f.mark = 103.5
        scan, failures = f.scan('current-generation')
        self.assertEqual(failures, {})
        packet = scan['venue_management']['BYBIT_SPOT']['XUSDT']
        source = int((f.now-dt.timedelta(seconds=590)).timestamp()*1000)
        packet['market_source_timestamp'] = source
        packet['liquidity']['raw_book_evidence']['source_timestamp'] = source
        return f, scan

    def main_sell(self, delay=None, multiple=False, fixed_replay=False):
        f, scan = self.bybit_fixture()
        clock = [f.now]
        packets = scan['venue_management']['BYBIT_SPOT']
        if multiple:
            other = copy.deepcopy(f.state['open_positions'][0])
            other.update(asset='Y', shadow_id='Y1', market_symbol='YUSDT')
            f.state['open_positions'].append(other)
            packet = copy.deepcopy(packets['XUSDT'])
            packet['market_symbol'] = 'YUSDT'
            packet['candidate']['asset'] = 'Y'
            packet['candidate']['signal_evidence']['market_symbol'] = 'YUSDT'
            packet['liquidity']['raw_book_evidence']['symbol'] = 'YUSDT'
            packets['YUSDT'] = packet
            # First holding remains valid after delay; the second crosses expiry.
            packets['XUSDT']['market_source_timestamp'] += 490000
            packets['XUSDT']['liquidity']['raw_book_evidence']['source_timestamp'] += 490000
        original_packets = copy.deepcopy(packets)
        empty = dict(mode='SIMULATION_ONLY_NO_REAL_ORDERS', open_positions=[], closed_positions=[], events=[], decisions=[])
        docs = {monitor.V1: empty, monitor.V2: f.state, monitor.REVIEW: f.review,
                monitor.LIQ: f.liq, monitor.SUPPLY: f.supply}
        writes = {}; reads = [0]; histories = [0]; delayed = [False]
        def advance(phase):
            if delay == phase and not delayed[0]:
                clock[0] += dt.timedelta(seconds=20); delayed[0] = True
        def load(path):
            if path == monitor.V2:
                reads[0] += 1
                if reads[0] == 3: advance('load')
            return copy.deepcopy(docs.get(path, {}))
        def write(path, doc):
            writes[path] = copy.deepcopy(doc); docs[path] = copy.deepcopy(doc)
        def history(*args, **kwargs):
            histories[0] += 1
            if histories[0] == 1: advance('v1_history')
        def wrap(phase, fn):
            def call(*args, **kwargs):
                result = fn(*args, **kwargs); advance(phase); return result
            return call
        with ExitStack() as stack:
            def mock(obj, name, **kwargs): stack.enter_context(patch.object(obj, name, **kwargs))
            mock(monitor, 'load', side_effect=load)
            mock(monitor, 'batch_market', return_value=f.market)
            mock(monitor, 'crypto_exclusions', return_value={'BSTOCK'})
            mock(monitor, 'refresh_management_evidence', return_value=(f.review, f.liq, {'generation_id':scan['generation_id'], 'evidence_refresh_cursor':0}))
            stack.enter_context(patch('research.hunter_bybit_management.collect', return_value=(packets, {})))
            mock(eng.tail, 'collect_systemic_evidence', side_effect=wrap('acquisition', lambda *a:{}))
            mock(eng, 'update_risk_controls', return_value=None)
            mock(monitor.leading, 'collect_evidence', return_value={})
            mock(monitor.leading, 'update_state', return_value=({'level':'NORMAL'}, False))
            mock(monitor.leading, 'update_history', return_value=[])
            mock(eng.tail, 'tail_budget_snapshot', return_value={})
            mock(monitor.books, 'live_fetch', side_effect=AssertionError('NETWORK_FORBIDDEN'))
            stack.enter_context(patch('urllib.request.urlopen', side_effect=AssertionError('NETWORK_FORBIDDEN')))
            stack.enter_context(patch('research.hunter_fast_reconciliation.observe', return_value={}))
            mock(eng, 'atomic_json_write', side_effect=write)
            mock(eng, 'refresh_closed_observations', side_effect=history)
            mock(eng, 'backfill_opportunity_history', side_effect=lambda p,n:p)
            mock(eng, 'decision', side_effect=wrap('decision', eng.decision))
            mock(eng, 'admit_execution_identity', side_effect=wrap('identity', eng.admit_execution_identity))
            mock(eng.lifecycle, 'protect', side_effect=wrap('protection', eng.lifecycle.protect))
            stack.enter_context(redirect_stdout(io.StringIO()))
            monitor.main(evaluation_clock=(lambda:f.now) if fixed_replay else (lambda:clock[0]))
        self.assertEqual(writes[monitor.OUT]['as_of_utc'], f.now.isoformat())
        for path in (monitor.V1, monitor.V2):
            self.assertEqual(writes[path]['last_cycle_generation_id'], scan['generation_id'])
        self.assertEqual(packets, original_packets)  # source acquisition is never restamped
        return writes[monitor.V2], writes[monitor.OUT], clock[0]

    def test_no_delay_keeps_existing_profit_protection_sell(self):
        state, out, _ = self.main_sell()
        self.assertEqual(state['open_positions'], [])
        self.assertEqual([e['type'] for e in state['events']], ['SHADOW_V2_SELL'])
        self.assertEqual(state['closed_positions'][0]['exit_reason'], 'PROFIT_PROTECTION')
        self.assertFalse(out['real_trading_enabled'])
        self.assertEqual(out['capital_authority'], 'NONE_SHADOW_ONLY')

    def test_acquisition_lane_load_and_internal_delays_cannot_sell_expired_source(self):
        for phase in ('acquisition', 'v1_history', 'load', 'decision', 'identity', 'protection'):
            with self.subTest(phase=phase):
                state, _, now = self.main_sell(phase)
                self.assertEqual(state['events'], [])
                self.assertEqual(len(state['open_positions']), 1)
                self.assertEqual(state['closed_positions'], [])
                self.assertEqual(state['updated_at_utc'], now.isoformat())

    def test_each_holding_gets_current_clock_without_changing_generation(self):
        state, _, _ = self.main_sell('identity', multiple=True)
        self.assertEqual([e['asset'] for e in state['events']], ['X'])
        self.assertEqual([p['asset'] for p in state['open_positions']], ['Y'])
        self.assertTrue(any('PRIMARY_VENUE_MARK_STALE' in d.get('reasons', []) for d in state['decisions']))

    def test_frozen_lane_start_reproduces_old_sell_but_live_clock_rejects(self):
        for phase in ('load','identity','protection'):
            with self.subTest(phase=phase):
                old, _, _ = self.main_sell(phase, fixed_replay=True)
                new, _, _ = self.main_sell(phase)
                self.assertEqual([e['type'] for e in old['events']], ['SHADOW_V2_SELL'])
                self.assertEqual(new['events'], [])

    def add_cycle(self, v1_mode, delay=None, fixed_replay=False, clock_enabled=True):
        from tests.test_hunter_position_monitor import PositionMonitorTests
        state, market, review, liq, supply = PositionMonitorTests().fixture()
        start = dt.datetime(2026,10,4,tzinfo=dt.timezone.utc); clock = [start]
        state['open_positions'][0]['tranches'][0]['at'] = (start-dt.timedelta(hours=2)).isoformat()
        observed = start-dt.timedelta(seconds=monitor.C['MAX_EVIDENCE_AGE_SECONDS']-10)
        review['candidates'][0]['signal_evidence'] = eng.stamp('X', 'older-permitted-signal', observed.isoformat())
        liq['snapshots']['X']['as_of_utc'] = observed.isoformat()
        target = monitor.V1 if v1_mode else monitor.V2; saved = {}
        def load(path):
            if path == target:
                if delay == 'load': clock[0] += dt.timedelta(seconds=20)
                return copy.deepcopy(state)
            return {}
        def wrap(phase, fn):
            def call(*args, **kwargs):
                result = fn(*args, **kwargs)
                if phase == delay: clock[0] += dt.timedelta(seconds=20)
                return result
            return call
        with patch.object(monitor,'load',side_effect=load), \
             patch.object(eng,'atomic_json_write',side_effect=lambda p,d:saved.update({p:copy.deepcopy(d)})), \
             patch.object(eng,'refresh_closed_observations',return_value=None), \
             patch.object(eng,'backfill_opportunity_history',side_effect=lambda p,n:p), \
             patch.object(eng,'decision',side_effect=wrap('decision',eng.decision)), \
             patch.object(eng,'capital_available',side_effect=wrap('capacity',eng.capital_available)), \
             patch('urllib.request.urlopen',side_effect=AssertionError('NETWORK_FORBIDDEN')), redirect_stdout(io.StringIO()):
            out = monitor.run_lane(target,'SHADOW_V1' if v1_mode else 'SHADOW_V2',market,review,liq,supply,start,v1_mode,
                regime_scan={'as_of_utc':start.isoformat(),'generation_id':'monitor-generation'},
                evaluation_clock=((lambda:start) if fixed_replay else (lambda:clock[0])) if clock_enabled else None)
        return saved[target], out

    def test_v1_add_rechecks_after_load_decision_and_capacity(self):
        for phase in ('load','decision','capacity'):
            with self.subTest(phase=phase):
                state, out = self.add_cycle(True, phase)
                self.assertEqual(out['added'], [])
                self.assertEqual(state['events'], [])
                self.assertEqual(len(state['open_positions'][0]['tranches']), 1)

    def test_no_delay_retains_v1_add_and_v2_defer_with_older_signal_generation(self):
        state, out = self.add_cycle(True)
        self.assertEqual(out['added'], ['X'])
        self.assertEqual([e['type'] for e in state['events']], ['SHADOW_V1_ADD'])
        state, out = self.add_cycle(False)
        self.assertEqual(out['added'], [])
        self.assertEqual(out['deferred_adds'], ['X'])
        self.assertEqual(state['events'], [])

    def test_v2_expired_evidence_does_not_create_add_proposal(self):
        for phase in ('load','decision'):
            with self.subTest(phase=phase):
                state, out = self.add_cycle(False, phase)
                self.assertEqual(out['added'], [])
                self.assertEqual(out['deferred_adds'], [])
                self.assertEqual(state['events'], [])

    def test_offline_replay_can_inject_frozen_clock(self):
        baseline, expected = self.add_cycle(True)
        replay, actual = self.add_cycle(True, 'capacity', fixed_replay=True)
        self.assertEqual(expected, actual)
        self.assertEqual(baseline['events'], replay['events'])
        self.assertEqual(baseline['decisions'], replay['decisions'])

    def test_opt_in_clock_preserves_default_fixed_time_caller_without_delay(self):
        for v1 in (True,False):
            with self.subTest(v1=v1):
                default, expected = self.add_cycle(v1, clock_enabled=False)
                dynamic, actual = self.add_cycle(v1)
                self.assertEqual(expected, actual)
                self.assertEqual(default, dynamic)


if __name__ == '__main__': unittest.main()
