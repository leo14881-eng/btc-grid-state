import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research import hunter_binance_stream as stream
from research import hunter_binance_stream_consumer as consumer
from research import hunter_early_signals as early
from research import hunter_forward_research as forward
from research import hunter_tail_risk as risk
from research import hunter_signal_provenance as provenance
from research.hunter_binance_stream_store import Store,LIMITS,STEPS,topic,roster,content_hash
from research.hunter_binance_http_child import allowed

NOW=1791670694000
DATE=dt.datetime.fromtimestamp((NOW+1)/1000,dt.timezone.utc)


def rows(interval,end=NOW,count=None):
    step=STEPS[interval]
    return [[(end//step-i)*step,'100','103','99',str(101-i*.01),'10',
             (end//step-i+1)*step-1,'1000',7,'5','500','0']
            for i in reversed(range(count or LIMITS[interval]))]


def message(symbol='BTCUSDT',interval='15m',stamp=NOW+1,start=None,closed=False):
    row=rows(interval,stamp,1)[0]
    if start is not None: row[0]=start; row[6]=start+STEPS[interval]-1
    return dict(e='kline',E=stamp,s=symbol,k=dict(s=symbol,i=interval,x=closed,
        **dict(zip(('t','o','h','l','c','v','T','q','n','V','Q','B'),row))))


def scan():
    return dict(binance_complete=True,generation_id='fixture',as_of_utc=DATE.isoformat(),
        coins={s:dict(pairs=[dict(venue='binance',base=s,pair=s+'USDT')]) for s in ('BTC','ETH')})


class CacheTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.store=Store(Path(temp.name)/'state.sqlite3')
        self.names=roster(scan(),DATE)
        self.epoch=self.store.begin_session(self.names,'a'*64)
        self.store.ack(self.epoch,self.names)

    def seed(self):
        for name in self.names:
            symbol,interval=stream.split_topic(name)
            self.store.ingest_rest(self.epoch,name,rows(interval),NOW,NOW)
            self.store.ingest_ws(self.epoch,message(symbol,interval),NOW+1)
        return self.freeze()

    def freeze(self,names=None,end=NOW+1,read_at=None):
        return self.store.freeze(names or self.names,end,end if read_at is None else read_at)

    def test_all_actual_intervals_and_no_4h(self):
        self.assertEqual(len(self.names),7)
        self.assertEqual({stream.split_topic(n)[1] for n in self.names},set(LIMITS))
        self.assertEqual(sum(n.endswith('_5m') for n in self.names),1)
        self.assertEqual(self.seed()['status'],'READY')

    def test_manifest_requires_complete_fresh_unique_authority(self):
        for edit in (lambda s:s.update(binance_complete=False),lambda s:s.update(generation_id=None),
                     lambda s:s.update(as_of_utc=(DATE-dt.timedelta(hours=1)).isoformat()),
                     lambda s:s['coins']['BTC']['pairs'][0].update(base='ETH')):
            s=scan();edit(s)
            with self.assertRaises(ValueError):roster(s,DATE)

    def test_native_rows_unchanged_for_all_periods(self):
        g=self.seed()
        for name in self.names:
            _,interval=stream.split_topic(name)
            self.assertEqual(g['windows'][name]['rows'],rows(interval))

    def test_rest_alone_cannot_substitute_for_current_ws(self):
        name=self.names[0];_,interval=stream.split_topic(name)
        self.store.ingest_rest(self.epoch,name,rows(interval),NOW,NOW)
        self.assertEqual(self.freeze([name])['status'],'UNKNOWN')

    def test_disconnect_and_restart_require_new_ack_and_data(self):
        self.seed();self.store.disconnect(self.epoch)
        self.assertEqual(self.freeze()['status'],'UNKNOWN')
        self.epoch=self.store.begin_session(self.names,'a'*64)
        self.assertEqual(self.freeze()['status'],'UNKNOWN')
        self.store.ack(self.epoch,self.names)
        self.assertEqual(self.freeze()['status'],'UNKNOWN')
        self.assertEqual(self.seed()['status'],'UNKNOWN') # equal old E cannot acquire new epoch

    def test_fresh_restart_event_requalifies_epoch(self):
        self.seed();self.store.disconnect(self.epoch)
        self.epoch=self.store.begin_session(self.names,'a'*64);self.store.ack(self.epoch,self.names)
        for name in self.names:
            symbol,interval=stream.split_topic(name)
            self.store.ingest_rest(self.epoch,name,rows(interval,NOW+2),NOW+2,NOW+2)
            self.store.ingest_ws(self.epoch,message(symbol,interval,NOW+3),NOW+3)
        self.assertEqual(self.freeze(end=NOW+3)['status'],'READY')

    def test_duplicate_and_out_of_order_do_not_refresh(self):
        self.seed()
        self.store.ingest_ws(self.epoch,message(),NOW+60000)
        self.store.ingest_ws(self.epoch,message(stamp=NOW),NOW+60000)
        state=next(s for s in self.store.states() if s['topic']==topic('BTCUSDT','15m'))
        self.assertEqual(state['ws_rx'],NOW+1)
        self.assertEqual(self.freeze(end=NOW+90002)['status'],'UNKNOWN')

    def test_future_event_and_early_close_rejected(self):
        for m in (message(stamp=NOW+2),message(closed=True)):
            with self.assertRaises(ValueError):self.store.ingest_ws(self.epoch,m,NOW+1)

    def test_missing_bar_and_corrupt_boundary_are_unknown(self):
        self.seed();name=topic('BTCUSDT','15m')
        with self.store.db() as db:db.execute('DELETE FROM revisions WHERE topic=? AND start=?',(name,rows('15m')[3][0]))
        self.assertEqual(self.freeze([name])['status'],'UNKNOWN')
        self.seed()
        with self.store.db() as db:db.execute('UPDATE revisions SET row_json=? WHERE topic=?',(json.dumps(rows('15m')[0]),name))
        self.assertEqual(self.freeze([name])['status'],'UNKNOWN')

    def test_unconfirmed_closed_bar_never_served(self):
        self.seed();name=topic('BTCUSDT','15m')
        with self.store.db() as db:db.execute('UPDATE revisions SET confirmed=0 WHERE topic=? AND start=?',(name,rows('15m')[-2][0]))
        self.assertEqual(self.freeze([name])['status'],'UNKNOWN')

    def test_later_repair_does_not_retroactively_fill_cut(self):
        name=topic('BTCUSDT','15m')
        self.store.ingest_rest(self.epoch,name,rows('15m'),NOW,NOW+10)
        self.store.ingest_ws(self.epoch,message(),NOW+1)
        self.assertEqual(self.freeze([name])['status'],'UNKNOWN')

    def test_gap_requires_repair_and_new_ws(self):
        self.seed();name=topic('BTCUSDT','15m');later=NOW+2*STEPS['15m']
        self.store.ingest_ws(self.epoch,message(stamp=later),later)
        self.assertEqual(self.freeze([name],later)['status'],'UNKNOWN')
        self.store.ingest_rest(self.epoch,name,rows('15m',later+1),later+1,later+1)
        self.assertEqual(self.freeze([name],later+1)['status'],'UNKNOWN')
        self.store.ingest_ws(self.epoch,message(stamp=later+2),later+2)
        self.assertEqual(self.freeze([name],later+2)['status'],'READY')

    def test_daily_15_to_35_semantics_retained(self):
        name=topic('BTCUSDT','1d')
        self.store.ingest_rest(self.epoch,name,rows('1d',count=15),NOW,NOW)
        self.store.ingest_ws(self.epoch,message(interval='1d'),NOW+1)
        g=self.freeze([name]);self.assertEqual(g['status'],'READY')
        self.assertEqual(len(g['windows'][name]['rows']),15)
        self.assertEqual(consumer.Frozen(g).packet('BTCUSDT','1d')['source'],consumer.CACHE_SOURCE)

    def test_insufficient_daily_stays_unknown(self):
        name=topic('BTCUSDT','1d')
        self.store.ingest_rest(self.epoch,name,rows('1d',count=14),NOW,NOW)
        self.store.ingest_ws(self.epoch,message(interval='1d'),NOW+1)
        self.assertEqual(self.freeze([name])['status'],'UNKNOWN')

    def test_manifest_hash_mismatch_fails(self):
        self.seed()
        self.assertEqual(self.store.freeze(self.names,NOW+1,NOW+1,'b'*64)['status'],'UNKNOWN')

    def test_read_exit_freshness_and_rollback(self):
        self.seed()
        for end in (NOW+90002,NOW):
            clock=iter([NOW+1,end])
            with self.assertRaises(ValueError):consumer.read(self.names,DATE,self.store,lambda:next(clock))

    def test_signal_formulas_identical_and_same_binding(self):
        cached=consumer.Frozen(self.seed());symbols=['BTCUSDT','ETHUSDT']
        def rest(url,*args):
            from urllib.parse import urlsplit,parse_qs
            q=parse_qs(urlsplit(url).query);return rows(q['interval'][0])[-int(q['limit'][0]):]
        with patch.object(early,'get',rest):
            old=(early.rolling(symbols,'1h'),early.rolling(symbols,'4h'),early.micro(symbols))
        with patch.object(early,'get',side_effect=AssertionError('NETWORK')):
            new=(early.rolling(symbols,'1h',cached),early.rolling(symbols,'4h',cached),early.micro(symbols,cached))
        for before,after in zip(old,new):
            for sym in symbols:
                self.assertEqual({k:v for k,v in before[sym].items() if k!='source_provenance'},
                                 {k:v for k,v in after[sym].items() if k!='source_provenance'})
        r1,r4,micro=new;signal=early.score_row('ETHUSDT','ETH',r1,r4,r1['BTCUSDT'],r4['BTCUSDT'],micro)
        provenance.signal_sources(signal,'ETH',DATE+dt.timedelta(seconds=1),DATE-dt.timedelta(hours=2),lambda *_:True)
        bad=copy.deepcopy(signal);bad['source_provenance']['btc_1h']['cache']['epoch']='b'*32
        with self.assertRaises(ValueError):provenance.signal_sources(bad,'ETH',DATE,DATE-dt.timedelta(hours=2),lambda *_:True)

    def test_daily_formula_and_audit_identical(self):
        cached=consumer.Frozen(self.seed())
        with patch.object(consumer,'window',return_value=cached),patch.object(forward,'get',side_effect=AssertionError('NETWORK')):
            result=forward.research_one('BTC',scan()['coins']['BTC'],{},{},DATE)
        self.assertEqual(result['observations']['market_structure'],forward.candle_features(rows('1d'),None)[0])
        self.assertEqual(result['observations']['market_structure_source']['source'],consumer.CACHE_SOURCE)

    def test_btc_closed_return_formula_and_audit_identical(self):
        cached=consumer.Frozen(self.seed())
        with patch.object(consumer,'window',return_value=None):
            original=risk._btc_short_returns(DATE,'fixture',lambda _:rows('5m'))
        with patch.object(consumer,'window',return_value=cached):
            result=risk._btc_short_returns(DATE,'fixture',lambda _:self.fail('NETWORK'))
        packet=result.pop('source_provenance');self.assertEqual(original,result)
        self.assertEqual(packet['source'],consumer.CACHE_SOURCE)

    def test_no_cache_error_fallback_for_daily_or_risk(self):
        with patch.object(consumer,'window',side_effect=ValueError('BINANCE_CACHE_UNKNOWN')), \
             patch.object(forward,'get',side_effect=AssertionError('NETWORK')):
            result=forward.research_one('BTC',scan()['coins']['BTC'],{},{},DATE)
            self.assertNotIn('market_structure',result['observations'])
            with self.assertRaises(ValueError):risk._btc_short_returns(DATE,'fixture',lambda _:self.fail('NETWORK'))

    def test_transport_receipts_cannot_relabel_local_as_exchange(self):
        packet=consumer.Frozen(self.seed()).packet('BTCUSDT','15m')
        self.assertEqual(packet['transport'][0]['source_clock'],'LOCAL_REQUEST_CUTOFF')
        self.assertEqual(packet['transport'][-1]['source_clock'],'EXCHANGE_EVENT')
        packet['transport'][0]['source_clock']='EXCHANGE_EVENT'
        with self.assertRaises(ValueError):provenance.validate_cache_packet(packet,'BTCUSDT','15m',25)

    def test_ack_is_bound_to_shard_and_request(self):
        p=stream.Protocol(self.store,self.names,'a'*64,NOW)
        with self.assertRaises(ValueError):p.receive(1,dict(id=1,result=None),NOW)
        with self.assertRaises(ValueError):p.receive(0,dict(id=99,result=None),NOW)

    def test_subscription_shards_and_control_budget(self):
        names=[topic('X'+str(i)+'USDT','1h') for i in range(1025)]
        p=stream.Protocol(self.store,names,'a'*64,NOW)
        self.assertEqual(len(p.requests),3)
        self.assertEqual([sum(len(r['params']) for r in s) for s in p.requests],[500,500,25])
        self.assertTrue(all(len(r['params'])<=100 for s in p.requests for r in s))

    def test_ack_and_24h_deadlines(self):
        p=stream.Protocol(self.store,self.names,'a'*64,NOW)
        with self.assertRaises(TimeoutError):p.tick(NOW+30001)
        p.receive(0,dict(id=1,result=None),NOW)
        with self.assertRaisesRegex(RuntimeError,'24H'):p.tick(NOW+85800000)

    def test_backoff_restores_only_after_continuous_full_health(self):
        b=stream.RecoveryBudget();self.assertEqual([b.failed() for _ in range(5)],[5,10,20,40,80])
        for t in range(0,300,30):self.assertFalse(b.observe(True,t))
        self.assertTrue(b.observe(True,300));self.assertEqual(b.failures,0)
        b.failed();b.observe(True,400);b.observe(False,430)
        self.assertFalse(b.observe(True,700));self.assertEqual(b.failures,1)

    def test_fixed_endpoint_and_exact_windows(self):
        url='https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=5m&limit=16&endTime=1'
        allowed(url)
        for bad in (url.replace('binance.vision','evil.test'),url+'&foo=bar',url.replace('limit=16','limit=1000'),url.replace('5m','4h')):
            with self.assertRaises(ValueError):allowed(bad)

    def test_selector_default_and_explicit_gate(self):
        with patch.dict('os.environ',{},clear=True):self.assertEqual(consumer.selected(),'REST')
        for values in ({'HUNTER_BINANCE_KLINE_SOURCE':'bogus'},
                       {'HUNTER_BINANCE_KLINE_SOURCE':'SPOT_CACHE'},
                       {'HUNTER_BINANCE_KLINE_SOURCE':'SPOT_CACHE','HUNTER_SERVER_ISOLATED_CHECKOUT':'1','GITHUB_ACTIONS':'true'}):
            with patch.dict('os.environ',values,clear=True),self.assertRaises(ValueError):consumer.selected()

    def test_unicode_symbols_are_retained_without_aliasing(self):
        from urllib.parse import urlencode
        symbol='\u725b\u718aUSDT'
        self.assertEqual(stream.split_topic(topic(symbol,'1h')),(symbol,'1h'))
        allowed('https://data-api.binance.vision/api/v3/klines?'+urlencode(dict(
            symbol=symbol,interval='1h',limit=5,endTime=NOW)))
        s=scan();s['coins'][symbol[:-4]]=dict(pairs=[dict(venue='binance',base=symbol[:-4],pair=symbol)])
        self.assertEqual(len(roster(s,DATE)),10)

    def test_collector_stops_after_five_failures_without_automatic_restart(self):
        with patch.object(stream,'session',side_effect=OSError('fixture')) as run:
            delays=[]
            with self.assertRaisesRegex(RuntimeError,'CIRCUIT_OPEN'):
                stream.collect(self.store,self.names,'a'*64,'b'*40,clock=lambda:NOW,sleep=delays.append)
        self.assertEqual(run.call_count,5)
        self.assertAlmostEqual(sum(delays),75)

    def test_failed_open_disconnects_session(self):
        def fail():raise OSError('fixture closed')
        with self.assertRaises(OSError):stream.session(self.store,self.names,'a'*64,lambda:False,connect=fail)
        self.assertEqual(self.freeze()['status'],'UNKNOWN')
        self.assertTrue(all(not s['ack'] and s['gap'] for s in self.store.states()))

    def test_repair_uses_native_limit_and_rejects_late_cancelled_reply(self):
        class Client:
            def request(inner,path,params,**kwargs):
                self.assertEqual(path,'/api/v3/klines')
                self.assertEqual(params,dict(symbol='BTCUSDT',interval='1d',limit=35,endTime=NOW))
                return rows('1d')
        name=topic('BTCUSDT','1d')
        clock=iter([NOW,NOW+1])
        stream.repair(self.store,self.epoch,name,NOW+100,clock=lambda:next(clock),client=Client())
        calls=iter([False,True])
        with self.assertRaisesRegex(RuntimeError,'CANCELLED'):
            stream.repair(self.store,self.epoch,name,NOW+100,cancelled=lambda:next(calls),clock=lambda:NOW,client=Client())
        clock=iter([NOW,NOW+101])
        with self.assertRaises(TimeoutError):
            stream.repair(self.store,self.epoch,name,NOW+100,clock=lambda:next(clock),client=Client())

    def test_manifest_receipt_binds_full_scan_and_reviewed_head(self):
        from scripts.hunter_binance_stream_ops import prepare
        receipt=prepare(scan(),'b'*40,DATE)
        self.assertEqual(receipt['snapshot_sha256'],content_hash(scan()))
        self.assertEqual(receipt['topics'],self.names)
        self.assertFalse(receipt['real_trading_enabled'])
        with self.assertRaises(ValueError):prepare(scan(),'main',DATE)

    def test_acceptance_is_not_publication_or_migration_claim(self):
        from scripts.hunter_binance_stream_ops import acceptance
        self.seed()
        result=acceptance(scan(),self.store,DATE,lambda:DATE)
        self.assertEqual(result['status'],'READY')
        self.assertEqual(result['windows'],7)
        self.assertFalse(result['publication_verified'])

    def test_batch_keeps_order_receipts_and_rolls_back_invalid_observation(self):
        first=message();bad=message(stamp=NOW+90002)
        with self.assertRaises(ValueError):
            self.store.ingest_ws_batch(self.epoch,[(first,NOW+1),(bad,NOW+1)])
        self.assertTrue(all(s['ws_rx']==0 for s in self.store.states()))
        self.store.ingest_ws_batch(self.epoch,[(first,NOW+1),(message(stamp=NOW+2),NOW+3)])
        state=next(s for s in self.store.states() if s['topic']==topic('BTCUSDT','15m'))
        self.assertEqual((state['ws_ts'],state['ws_rx']),(NOW+2,NOW+3))
        with self.assertRaises(ValueError):self.store.ingest_ws_batch(self.epoch,[(first,NOW+1)]*101)

    def test_batched_protocol_applies_ack_before_market_data(self):
        p=stream.Protocol(self.store,self.names,'a'*64,NOW)
        p.receive_batch([(0,dict(id=1,result=None),NOW),(0,message(),NOW+1)])
        self.assertTrue(all(s['ack'] for s in self.store.states()))
        self.assertEqual(next(s for s in self.store.states() if s['topic']==topic('BTCUSDT','15m'))['ws_ts'],NOW+1)

    def test_cache_audit_does_not_change_systemic_observation_dedup_identity(self):
        from research.hunter_policy import C
        cached=consumer.Frozen(self.seed())
        def fetch(url):return rows('5m') if '/klines?' in url else {'price':'1'}
        with patch.object(consumer,'window',return_value=None):
            original=risk.collect_systemic_evidence(scan(),{},DATE,C,fetch_json=fetch)
        with patch.object(consumer,'window',return_value=cached):
            result=risk.collect_systemic_evidence(scan(),{},DATE,C,fetch_json=fetch)
        self.assertEqual(result['observation_id'],original['observation_id'])
        self.assertIn('source_provenance',result['btc_short'])

    def test_disconnect_revokes_repairs_before_waiting_for_socket_close(self):
        import contextlib
        from threading import Event
        started=Event();closed=Event();cancel_seen=Event();sent=[];reads=[0];ticks=[NOW]
        def clock():ticks[0]+=1000;return ticks[0]
        class WS:
            def send(inner,payload):sent.append(json.loads(payload))
            def recv(inner,timeout):
                reads[0]+=1
                if reads[0]==1:return json.dumps(dict(id=sent[0]['id'],result=None))
                if not started.wait(3):raise AssertionError('repair never started')
                raise OSError('fixture disconnect')
        @contextlib.contextmanager
        def connect():
            try:yield WS()
            finally:
                # The close handshake must not be required for cancellation.
                self.assertTrue(cancel_seen.wait(3),'repair not cancelled before socket close')
                closed.set()
        def repairer(store,epoch,name,deadline,*,cancelled):
            started.set()
            for _ in range(300):
                if cancelled():cancel_seen.set();return
                closed.wait(.01)
            raise AssertionError('repair cancellation missing')
        with self.assertRaisesRegex(OSError,'fixture disconnect'):
            stream.session(self.store,[topic('BTCUSDT','15m')],'a'*64,lambda:False,
                connect=connect,clock=clock,sleep=lambda _:None,repairer=repairer)
        self.assertTrue(closed.is_set())
        self.assertEqual(self.freeze()['status'],'UNKNOWN')


if __name__=='__main__':unittest.main()
