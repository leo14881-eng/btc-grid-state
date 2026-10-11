"""Synthetic exchange candles on the real hourly cadence; no profitable replay claim."""
import copy
import datetime as dt
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs,urlparse
from tests import test_hunter_reentry as fixtures
from research import hunter_shadow_trader_v2 as eng
from research import hunter_early_signals as signals
from research import hunter_bybit_signal_capture as bybit


class HourlyReentryTests(unittest.TestCase):
    setUp = fixtures.ReentryTests.setUp
    inputs = fixtures.ReentryTests.inputs
    run_observation = fixtures.ReentryTests.run_observation
    cycle = fixtures.ReentryTests.cycle

    def market_inputs(self,n,venue='binance',offset=0,recovery=False):
        now = self.start+dt.timedelta(minutes=n)
        def value(ms,symbol):
            if recovery and symbol!='BTCUSDT':
                boundary=int(now.timestamp()*1000)//900000*900000
                return 99 if ms<boundary else 101
            h=(ms/1000-self.start.timestamp())/3600+6
            return 100 if symbol=='BTCUSDT' else 95+.2*h*h+offset
        def rows(symbol,interval,count):
            period=3600000 if interval in ('1h','60') else 900000
            last=int(now.timestamp()*1000)//period*period
            result=[]
            for i in range(count):
                start=last-(count-1-i)*period
                end=min(start+period-1,int(now.timestamp()*1000))
                o,c=value(start,symbol),value(end,symbol)
                quote=10000*(end-start+1)/period
                result.append([start,o,max(o,c)+.01,min(o,c)-.01,c,quote/c,start+period-1,quote])
            return result
        class Clock(dt.datetime):
            @classmethod
            def now(cls,tz=None):return now
        def fetch(url,timeout):
            q=parse_qs(urlparse(url).query)
            return rows(q['symbol'][0],q['interval'][0],int(q['limit'][0]))
        with patch.object(signals,'get',side_effect=fetch),patch.object(signals.dt,'datetime',Clock):
            if venue=='binance':
                r1=signals.rolling(['XUSDT','BTCUSDT'],'1h')
                r4=signals.rolling(['XUSDT','BTCUSDT'],'4h')
                micro=signals.micro(['XUSDT','BTCUSDT'])
                signal=signals.score_row('XUSDT','X',r1,r4,r1['BTCUSDT'],r4['BTCUSDT'],micro)
            else:
                def bybit_fetch(symbol,interval,count):
                    return [r[:6]+[r[7]] for r in rows(symbol,interval,count)]
                captured,failures=bybit.capture([{'base':'X','pair':'XUSDT'}],bybit_fetch,observed_at=now)
                self.assertFalse(failures)
                signal=captured['X']
        price=value(now.timestamp()*1000,'XUSDT')
        data=self.inputs(n,price)
        data[0]['signal']=signal
        # Real Discovery precedes Research by five minutes. This timestamp made
        # the former prior-setup freshness rule impossible at the next hour.
        data[1]['as_of_utc']=(now-dt.timedelta(minutes=5)).isoformat()
        if venue=='bybit':
            book=data[2]['snapshots']['X']['raw_book_evidence']
            book.update(exchange='bybit',source_timestamp=now.timestamp()*1000)
        return price,data

    def test_hourly_expired_setup_reconstructed_current_packet_reaches_both_buy_loops(self):
        initial=copy.deepcopy(self.state)
        for lane in ('V1','V2'):
            self.state=copy.deepcopy(initial)
            old=self.inputs(22,100.1)
            old[1]['as_of_utc']=(self.start+dt.timedelta(minutes=17)).isoformat()
            self.run_observation(22,100.1,inputs=old)
            price,data=self.market_inputs(82)
            closed=copy.deepcopy(self.state['closed_positions'])
            with patch.object(self,'inputs',return_value=data):self.cycle(lane,82,price)
            self.assertEqual(len(self.state['open_positions']),1,lane)
            proof=self.state['reentry_registry']['X']['reentry_historical_trend']
            self.assertEqual(proof['kind'],'RECONSTRUCTED_MARKET_TREND_NOT_HISTORICAL_HEALTH')
            self.assertTrue(proof['relative_trend_positive'])
            self.assertEqual(self.state['closed_positions'],closed)

    def test_reconstructed_24_rows_equal_original_25_row_formula_at_every_quarter_phase(self):
        for n in (60,75,90,105):
            price,data=self.market_inputs(n)
            signal=data[0]['signal']
            proof=eng.reentry.provenance.reconstruct_trend(signal,'X',self.start)
            point=dt.datetime.fromisoformat(proof['market_at_utc'])
            # Independent producer calls at the historical instant. These are
            # synthetic fixtures, NOT a claim that those calls happened live.
            _,historical=self.market_inputs((point-self.start).total_seconds()/60)
            expected=historical[0]['signal']
            for field in eng.reentry.SIGNAL_FIELDS:
                self.assertEqual(proof['signal'].get(field),expected.get(field),(n,field))

    def test_current_partial_future_prices_cannot_change_reconstructed_history(self):
        _,data=self.market_inputs(82)
        s=data[0]['signal']
        before=eng.reentry.provenance.reconstruct_trend(s,'X',self.start)
        for p in s['source_provenance'].values():
            p['bars'][-1]['ohlcv_quote']=[999,1000,998,999,99,999]
            p['content_hash']=eng.reentry.provenance.hash_value(p['bars'])
        after=eng.reentry.provenance.reconstruct_trend(s,'X',self.start)
        self.assertEqual(before['signal'],after['signal'])
        self.assertEqual(before['price'],after['price'])

    def test_missing_btc_history_or_pre_exit_cutoff_never_uses_old_strong(self):
        for kind in ('btc_missing','cutoff'):
            self.state['reentry_registry']['X'].pop('reentry_setup',None)
            self.state['reentry_registry']['X'].pop('reentry_last_observation',None)
            self.state['reentry_registry']['X']['reentry_seen_generations']=[]
            self.state['reentry_registry']['X']['reentry_seen_evidence_ids']=[]
            self.state['reentry_registry']['X']['reentry_seen_markets']=[]
            price,data=self.market_inputs(82)
            if kind=='btc_missing':data[0]['signal']['source_provenance'].pop('btc_micro')
            else:self.state['reentry_registry']['X']['reentry_confirmation_after_utc']=(self.start+dt.timedelta(minutes=80)).isoformat()
            self.assertFalse(self.run_observation(82,price,inputs=data)[0])
            self.assertEqual(self.state['reentry_registry']['X']['reentry_historical_trend']['status'],'UNKNOWN')

    def test_false_breakout_current_decline_and_original_protection_still_reject(self):
        initial=copy.deepcopy(self.state)
        for kind in ('decline','floor','buy_gate','capital'):
            self.state=copy.deepcopy(initial)
            price,data=self.market_inputs(82)
            if kind=='decline':price=eng.reentry.provenance.reconstruct_trend(data[0]['signal'],'X',self.start)['price']-.1
            if kind=='floor':self.state['reentry_registry']['X']['reentry_context']['position']['protection_lifecycle']['protected_floor_usdt']=9999
            if kind in ('buy_gate','capital'):
                if kind=='buy_gate':data[0]['trade_action']='WAIT'
                else:self.state['open_positions']=[{'asset':'Y','tranches':[{'notional_usdt':20000,'price':1}]}]
                with patch.object(self,'inputs',return_value=data):self.cycle('V2',82,price)
                self.assertNotIn('X',[p['asset'] for p in self.state['open_positions']])
            else:self.assertFalse(self.run_observation(82,price,inputs=data)[0])

    def test_bybit_real_producer_contract_reaches_reentry_and_rejects_mixed_venue(self):
        initial=copy.deepcopy(self.state)
        for wrong in (False,True):
            self.state=copy.deepcopy(initial)
            pos=self.state['reentry_registry']['X']['reentry_context']['position']
            pos.update(execution_venue='BYBIT_SPOT',market_symbol='XUSDT',market_type='spot',execution_fee_bps=8)
            price,data=self.market_inputs(82,'bybit')
            if wrong:data[0]['signal']['source_provenance']['btc_micro']['venue']='binance'
            self.assertEqual(self.run_observation(82,price,inputs=data)[0],not wrong)

    def test_new_window_replay_and_restart_cannot_buy_twice(self):
        price,data=self.market_inputs(82)
        self.assertTrue(self.run_observation(82,price,inputs=data)[0])
        self.state=copy.deepcopy(self.state)
        self.assertFalse(self.run_observation(82,price,inputs=data)[0])

    def test_historical_weak_point_then_current_recovery_uses_existing_path(self):
        price,data=self.market_inputs(82,recovery=True)
        ok,reasons=self.run_observation(82,price,inputs=data)
        self.assertTrue(ok,reasons)
        self.assertIn('PULLBACK_RECOVERY',reasons)
        self.assertFalse(self.state['reentry_registry']['X']['reentry_historical_trend']['relative_trend_positive'])

    def test_new_history_does_not_clear_original_loss_or_unknown_hard_cause(self):
        for reason in ('LOSS_EXIT','HARD_INVALIDATION'):
            self.state['reentry_registry']['X'].update(last_exit_reason=reason,risk_lock=True)
            price,data=self.market_inputs(82)
            self.state['systemic_risk']={'level':'NORMAL','last_observation_id':'new-risk',
                                         'last_observed_at_utc':data[0]['signal_evidence']['observed_at_utc']}
            ok,reasons=self.run_observation(82,price,inputs=data)
            self.assertFalse(ok)
            self.assertIn('REENTRY_REASON_UNRESOLVED',reasons)
            self.state['reentry_registry']['X'].pop('reentry_confirmation_after_utc',None)
            self.state['reentry_registry']['X'].pop('reentry_last_observation',None)
            self.state['reentry_registry']['X'].update(reentry_seen_generations=[],reentry_seen_evidence_ids=[],reentry_seen_markets=[])

    @unittest.skipUnless(hasattr(eng,'loss_freeze'),'requires exact combined PR102 integration checkout')
    def test_empty_peer_monitor_skip_then_hourly_v1_v2_buys_all_have_release_note(self):
        from research import hunter_strategy_release as release
        from research import hunter_position_monitor as monitor
        from tests.test_hunter_loss_freeze import market
        states={lane:{'mode':'SIMULATION_ONLY_NO_REAL_ORDERS','open_positions':[],
                      'closed_positions':[],'events':[],'decisions':[]} for lane in ('V1','V2')}
        for lane,state in states.items():
            eng.loss_freeze.update_risk_controls(state,market(self.start,oid=lane),self.start,eng.C)
        manifest={'schema':'hunter_common_release_v1','release_id':'synthetic-reviewed-release',
                  'enabled':True,'strategy_version':eng.reentry.STRATEGY_VERSION,'components':release.component_receipt(eng),
                  'activated_at_utc':self.start.isoformat(),'real_trading_enabled':False,'real_order_count':0,
                  'capital_authority':'NONE_SHADOW_ONLY',
                  'lane_activation_ids':{lane:state['loss_control']['activated_at_utc'] for lane,state in states.items()}}
        before=copy.deepcopy(states)
        with patch.object(monitor,'load',side_effect=lambda p:states['V1' if p==monitor.V1 else 'V2']),patch.object(eng,'atomic_json_write'),patch('builtins.print'):
            monitor.main()
        self.assertEqual(states,before)  # Empty monitor did not refresh peer risk.
        for lane,n in (('V1',82),('V2',83)):
            self.state=states[lane]
            self.release_peer=states['V2' if lane=='V1' else 'V1']
            self.release_manifest=manifest
            price,data=self.market_inputs(n)
            with patch.object(self,'inputs',return_value=data):self.cycle(lane,n,price,ready=None)
            self.assertEqual(len(self.state['open_positions']),1,lane)
            self.assertEqual(self.state['events'][-1]['strategy_note'],eng.reentry.STRATEGY_NOTE)

