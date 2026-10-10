import copy
import datetime as dt
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from research import hunter_bybit_management as bybit
from research import hunter_shadow_trader_v2 as engine
from research import hunter_position_monitor as monitor
import test_hunter_position_monitor as fixtures


NOW = dt.datetime(2026,10,8,tzinfo=dt.timezone.utc)


class BybitManagementTests(unittest.TestCase):
    def setUp(self):
        self.now=NOW;self.mark=103;self.calls=[]
        self.state,self.market,self.review,self.liq,self.supply=fixtures.PositionMonitorTests().fixture()
        self.position=self.state['open_positions'][0]
        self.position.update(execution_venue='BYBIT_SPOT',market_symbol='XUSDT',market_type='spot',execution_fee_bps=8)
        monitor.configure_lane(False)

    def fetch(self,path,params):
        self.calls.append((path,copy.deepcopy(params)))
        symbol=params['symbol'];at=int(self.now.timestamp()*1000)
        self.assertEqual(params['category'],'spot')
        if path.endswith('tickers'):
            return dict(retCode=0,time=at,result=dict(category='spot',list=[dict(symbol=symbol,lastPrice=str(self.mark))]))
        if path.endswith('orderbook'):
            return dict(retCode=0,time=at,result=dict(s=symbol,ts=at,u=1,b=[[str(self.mark),'10000']],a=[[str(self.mark+.01),'10000']]))
        period=int(params['interval'])*60000;end=at//period*period;rows=[]
        for i in range(params['limit']):
            price=100 if symbol=='BTCUSDT' else 90+i
            rows.append([str(end-(params['limit']-1-i)*period),str(price),str(price+2),str(price-1),str(price+1),'10','1000'])
        return dict(retCode=0,time=at,result=dict(category='spot',symbol=symbol,list=list(reversed(rows))))

    def scan(self,generation='monitor-g1',fetcher=None):
        packets,failures=bybit.collect([self.position],self.review,generation,fetcher or self.fetch,lambda:self.now)
        scan=dict(generation_id=generation,as_of_utc=self.now.isoformat(),coins={'X':dict(reference_price=200),'BTC':dict(reference_price=100)},venue_management={'BYBIT_SPOT':packets})
        return scan,failures

    def manage(self,scan):
        with patch.object(engine,'backfill_opportunity_history',side_effect=lambda p,n:p):
            engine.manage_existing_positions(self.state,scan,self.review,self.liq,self.supply,self.now,capital_proposals=[])

    def test_collect_only_actual_primary_bybit_holdings(self):
        scan,failures=self.scan()
        self.assertEqual(failures,{})
        self.assertEqual(set(p['symbol'] for _,p in self.calls),{'XUSDT','BTCUSDT'})
        self.assertTrue(all(path in bybit.PUBLIC_PATHS for path,_ in self.calls))
        self.assertEqual(scan['venue_management']['BYBIT_SPOT']['XUSDT']['fee_bps'],8)

    def test_no_bybit_position_makes_no_request(self):
        self.position['execution_venue']='BINANCE_SPOT'
        self.assertEqual(bybit.collect([self.position],self.review,'g',self.fetch,lambda:self.now),({},{}))
        self.assertEqual(self.calls,[])

    def test_primary_mark_arms_without_binance_reference_sell(self):
        scan,_=self.scan();self.manage(scan)
        self.assertEqual(self.position['last_price'],103)
        self.assertEqual(self.position['protection_lifecycle']['state'],'ARMED')
        self.assertEqual(self.position['protection_lifecycle']['armed_generation_id'],'monitor-g1')
        self.assertEqual(self.state['events'],[])
        self.assertEqual(self.position['last_exit_estimate']['venue'],'BYBIT_SPOT')

    def test_new_high_then_positive_giveback_sell_once(self):
        scan,_=self.scan();self.manage(scan)
        self.now+=dt.timedelta(minutes=5);self.mark=106
        scan,_=self.scan('monitor-g2');self.manage(scan)
        self.assertEqual(self.state['events'],[])
        self.now+=dt.timedelta(minutes=5);self.mark=103.5
        scan,_=self.scan('monitor-g3');self.manage(scan)
        self.assertEqual(self.state['open_positions'],[])
        self.assertEqual(len(self.state['events']),1)
        event=self.state['events'][0]
        self.assertEqual(event['execution_venue'],'BYBIT_SPOT')
        self.assertEqual(self.state['closed_positions'][0]['exit_reason'],'PROFIT_PROTECTION')
        self.assertGreater(self.state['closed_positions'][0]['net_pnl_usdt'],0)
        self.manage(scan);self.assertEqual(len(self.state['events']),1)

    def test_gap_to_negative_never_fakes_positive_exit(self):
        scan,_=self.scan();self.manage(scan)
        self.now+=dt.timedelta(minutes=5);self.mark=98
        scan,_=self.scan('monitor-g2');self.manage(scan)
        self.assertEqual(self.state['events'],[])
        self.assertEqual(len(self.state['open_positions']),1)
        self.assertEqual(self.position['protection_lifecycle']['incident'],'GAPPED_THROUGH_PROTECTION_WINDOW')

    def test_duplicate_and_old_generation_do_not_recount_health(self):
        scan,_=self.scan();self.manage(scan)
        before=copy.deepcopy(self.position['health_transitions'])
        self.manage(scan)
        self.assertEqual(self.position['health_transitions'],before)
        old=copy.deepcopy(scan);old['generation_id']='old'
        self.manage(old)
        self.assertEqual(self.position['health_transitions'],before)

    def test_missing_wrong_venue_or_stale_depth_keeps_state(self):
        scan,_=self.scan()
        for mutation in ('absent','venue','symbol','stale','fee','generation','price','liquidity'):
            with self.subTest(mutation=mutation):
                packet_scan=copy.deepcopy(scan);packets=packet_scan['venue_management']['BYBIT_SPOT'];packet=packets['XUSDT']
                if mutation=='absent':packets.clear()
                elif mutation=='venue':packet['liquidity']['raw_book_evidence']['exchange']='binance'
                elif mutation=='symbol':packet['candidate']['signal_evidence']['market_symbol']='YUSDT'
                elif mutation=='stale':packet['liquidity']['raw_book_evidence']['source_timestamp']-=601000
                elif mutation=='fee':packet['fee_bps']=10
                elif mutation=='price':packet['market']['reference_price']=float('nan')
                elif mutation=='liquidity':packet['liquidity']['as_of_utc']=(self.now-dt.timedelta(seconds=601)).isoformat()
                else:packet['generation_id']='older'
                before=copy.deepcopy(self.position)
                self.manage(packet_scan)
                self.assertEqual({k:v for k,v in self.position.items() if k!='last_monitor_decision'},
                                 {k:v for k,v in before.items() if k!='last_monitor_decision'})
                self.assertEqual(self.position['last_monitor_decision']['action'],'HOLD')
                self.assertEqual(self.state['events'],[])

    def test_binance_management_continues_when_bybit_failed(self):
        other=copy.deepcopy(self.position);other.update(asset='Y',shadow_id='Y1',execution_venue='BINANCE_SPOT',market_symbol='YUSDT',execution_fee_bps=10)
        self.state['open_positions'].append(other)
        scan=dict(generation_id='g',as_of_utc=self.now.isoformat(),coins={'X':dict(reference_price=200),'Y':dict(reference_price=99)})
        self.manage(scan)
        self.assertNotIn('last_price',self.position)
        self.assertEqual(other['last_price'],99)
        self.assertEqual(self.state['events'],[])

    def test_wrong_or_stale_kline_fails_closed(self):
        for kind in ('category','time','gap'):
            with self.subTest(kind=kind):
                def bad(path,params):
                    body=self.fetch(path,params)
                    if path.endswith('kline'):
                        if kind=='category':body['result']['category']='linear'
                        elif kind=='time':body['time']-=601000
                        else:body['result']['list'][0][0]=body['result']['list'][1][0]
                    return body
                packets,failures=bybit.collect([self.position],self.review,'g',bad,lambda:self.now)
                self.assertEqual(packets,{})
                self.assertIn('XUSDT',failures)

    def test_bybit_history_and_closed_marks_never_use_binance(self):
        with patch.object(engine,'binance_kline_bars',side_effect=AssertionError('Binance history forbidden')):
            engine.backfill_opportunity_history(self.position,self.now)
        self.position.update(closed_at_utc=self.now.isoformat(),exit_reference_price=103,net_return_pct=2,holding_mfe_pct=3,post_exit_observation={},observation_complete=False)
        before=self.position.get('full_opportunity_peak_price')
        with patch.object(engine,'backfill_opportunity_history',side_effect=lambda p,n:p):
            engine.refresh_closed_observations({'closed_positions':[self.position]},self.now,lambda asset:200)
        self.assertEqual(self.position.get('full_opportunity_peak_price'),before)

    def test_old_primary_timestamp_cannot_overwrite_fresh_mark(self):
        scan,_=self.scan();self.manage(scan)
        previous=copy.deepcopy(self.position)
        self.now+=dt.timedelta(minutes=5)
        scan,_=self.scan('monitor-g2')
        scan['venue_management']['BYBIT_SPOT']['XUSDT']['market_source_timestamp']=int(NOW.timestamp()*1000)
        self.manage(scan)
        self.assertEqual({k:v for k,v in self.position.items() if k!='last_monitor_decision'},
                         {k:v for k,v in previous.items() if k!='last_monitor_decision'})
        self.assertEqual(self.position['last_monitor_decision']['thesis_status'],'EVIDENCE_PENDING')

    def test_monitor_persists_and_restart_preserves_armed_and_evidence(self):
        scan,_=self.scan()
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'portfolio.json';engine.atomic_json_write(path,self.state)
            monitor.run_lane(path,'SHADOW_V2',self.market,self.review,self.liq,self.supply,self.now,False,regime_scan=scan)
            saved=monitor.load(path);position=saved['open_positions'][0]
            self.assertEqual(position['protection_lifecycle']['state'],'ARMED')
            self.assertEqual(position['last_cycle_generation_id'] if 'last_cycle_generation_id' in position else saved['last_cycle_generation_id'],'monitor-g1')
            self.assertEqual(position['last_primary_venue_management_evidence']['raw_book_evidence']['exchange'],'bybit')
            self.now+=dt.timedelta(minutes=5);self.mark=104
            scan,_=self.scan('monitor-g2')
            monitor.run_lane(path,'SHADOW_V2',self.market,self.review,self.liq,self.supply,self.now,False,regime_scan=scan)
            restarted=monitor.load(path)['open_positions'][0]
            self.assertEqual(restarted['protection_lifecycle']['armed_generation_id'],'monitor-g1')
            # A new five-minute wrapper around the same closed source window cannot
            # count as another health confirmation; its final receipt is still new.
            self.assertEqual(restarted['last_health_generation_id'],'monitor-g1')
            self.assertEqual(restarted['last_monitor_decision']['generation_id'],'monitor-g2')
            self.assertIn('THESIS_SOURCE_WINDOW_ALREADY_CONSUMED',restarted['last_monitor_decision']['reasons'])
            self.assertEqual(len(restarted['tranches']),1)
            self.assertEqual(monitor.load(path)['events'],[])


if __name__=='__main__':unittest.main()
