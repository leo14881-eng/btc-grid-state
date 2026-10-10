import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research import hunter_bybit_stream as stream
from research.hunter_bybit_stream_store import Store, Budget, LIMITS, topic
from research.hunter_bybit_worker import candles, digest
from research.hunter_bybit_signal_capture import features

NOW = 1_791_670_694_000


def rest(symbol='BTCUSDT', interval='15', end=NOW):
    step = int(interval)*60_000
    rows = [[str((end//step-i)*step), '100', '103', '99', str(101-i*.01), '10', '1000']
            for i in range(LIMITS[interval])]
    return dict(retCode=0, time=end, result=dict(category='spot', symbol=symbol, list=rows))


def message(symbol='BTCUSDT', interval='15', ts=NOW, start=None, confirm=False):
    step = int(interval)*60_000
    start = ts//step*step if start is None else start
    return dict(topic=topic(symbol, interval), type='snapshot', ts=ts, data=[dict(
        start=start, end=start+step-1, interval=interval, open='100', high='103', low='99',
        close='101.0', volume='10', turnover='1000', confirm=confirm, timestamp=min(ts,start+step-1))])


class StreamTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name)/'cache.sqlite3')
        self.topics = [topic('BTCUSDT', i) for i in LIMITS]
        self.protocol = stream.Protocol(self.store, self.topics, 'fixture-roster', NOW)
        self.epoch = self.protocol.epoch
        for request in self.protocol.requests:
            self.protocol.receive(dict(op='subscribe', req_id=request['req_id'], success=True), NOW)

    def seed(self, interval='15'):
        self.store.ingest_rest(self.epoch, topic('BTCUSDT', interval), rest(interval=interval), NOW, NOW)
        self.store.ingest_ws(self.epoch, message(interval=interval, ts=NOW+1), NOW+1)

    def read(self, interval='15', end=NOW+1, now=NOW+1):
        return Store.reader(self.store.path).window('BTCUSDT', interval, end, now)

    def test_current_bar_rows_and_features_match_existing_consumer(self):
        for interval in LIMITS:
            self.seed(interval)
            got = self.read(interval)
            self.assertEqual(got['status'], 'READY')
            self.assertEqual(got['rows'], candles(rest(interval=interval), 'BTCUSDT', interval, LIMITS[interval], NOW))
            self.assertFalse(got['provenance'][-1]['confirm'])
            self.assertEqual(got['provenance'][-1]['source'], 'OFFICIAL_BYBIT_V5_SPOT_WS')
        expected = features(candles(rest(interval='60'),'BTCUSDT','60',5,NOW),
                            candles(rest(),'BTCUSDT','15',25,NOW))
        self.assertEqual(features(self.read('60')['rows'], self.read('15')['rows']), expected)

    def test_heartbeat_does_not_make_topic_fresh(self):
        self.seed()
        self.protocol.receive({'op':'pong'}, NOW+90_001)
        self.assertEqual(self.read(end=NOW+90_002, now=NOW+90_002)['status'], 'UNKNOWN')

    def test_duplicates_do_not_refresh_topic_time(self):
        self.seed()
        self.store.ingest_ws(self.epoch, message(ts=NOW+1), NOW+50_000)
        self.assertEqual(self.store.states()[0]['ws_rx'], NOW+1)

    def test_delayed_update_cannot_unclose_or_replace_newer_bar(self):
        step=900_000; start=NOW//step*step; closed=start+step
        self.store.ingest_ws(self.epoch,message(ts=closed,start=start,confirm=True),closed)
        self.store.ingest_ws(self.epoch,message(ts=closed+1,start=start,confirm=False),closed+1)
        with self.store.db(readonly=True) as db:
            row=db.execute('SELECT * FROM bars WHERE start=?',(start,)).fetchone()
            self.assertEqual(row['confirmed'],1)
            self.assertEqual(row['source_time'],closed)

    def test_future_data_never_backfills_old_generation(self):
        self.seed()
        self.store.ingest_ws(self.epoch,message(ts=NOW+2),NOW+2)
        self.assertEqual(self.read()['reason'], 'TOPIC_NOT_READY_OR_STALE')
        self.assertEqual(self.read(end=NOW+2,now=NOW+2)['status'],'READY')

    def test_rest_received_after_generation_cannot_retrofill_it(self):
        self.store.ingest_rest(self.epoch,self.topics[0],rest(),NOW,NOW+10)
        self.store.ingest_ws(self.epoch,message(ts=NOW+1),NOW+1)
        self.assertEqual(self.read()['reason'],'FUTURE_OBSERVATION_FOR_GENERATION')

    def test_missing_window_needs_rest_repair(self):
        self.seed()
        later=NOW+2*900_000
        self.store.ingest_ws(self.epoch,message(ts=later),later)
        self.assertEqual(self.read(end=later,now=later)['status'],'UNKNOWN')
        self.store.ingest_rest(self.epoch,self.topics[0],rest(end=later),later,later)
        self.assertEqual(self.read(end=later,now=later)['status'],'READY')

    def test_restart_and_reconnect_require_ack_repair_and_new_topic_message(self):
        self.seed()
        self.store.disconnect(self.epoch)
        self.assertEqual(self.read()['status'],'UNKNOWN')
        other=Store(self.store.path)
        epoch=other.begin_session(self.topics,'same-roster')
        other.ack(epoch,self.topics)
        other.ingest_rest(epoch,self.topics[0],rest(end=NOW+2),NOW+2,NOW+2)
        self.assertEqual(self.read(end=NOW+2,now=NOW+2)['status'],'UNKNOWN')
        other.ingest_ws(epoch,message(ts=NOW+3),NOW+3)
        self.assertEqual(self.read(end=NOW+3,now=NOW+3)['status'],'READY')
        with self.assertRaisesRegex(ValueError,'STALE_SESSION'):
            self.store.ingest_ws(self.epoch,message(),NOW)

    def test_bad_ack_and_unsubscribed_topic_rejected(self):
        with self.assertRaises(ValueError): self.protocol.receive({'op':'subscribe','req_id':'other','success':True},NOW)
        with self.assertRaises(ValueError): self.store.ingest_ws(self.epoch,message('GRASSUSDT'),NOW)
        new=stream.Protocol(self.store,self.topics,'scope',NOW)
        with self.assertRaises(ValueError): new.receive(message(),NOW)
        with self.assertRaises(ValueError): new.receive({'op':'subscribe','req_id':new.requests[0]['req_id'],'success':False},NOW)

    def test_ping_20_seconds_and_ack_timeout(self):
        self.assertIsNone(self.protocol.tick(NOW+19_999))
        self.assertEqual(self.protocol.tick(NOW+20_000)['op'],'ping')
        with self.assertRaises(TimeoutError): self.protocol.tick(NOW+60_001)
        new=stream.Protocol(self.store,self.topics,'scope',NOW)
        with self.assertRaises(TimeoutError): new.tick(NOW+30_001)

    def test_invalid_envelopes_and_bars_fail_closed(self):
        for field,value in [('ts',NOW+1),('type','delta')]:
            bad=message();bad[field]=value
            with self.assertRaises(ValueError): self.store.ingest_ws(self.epoch,bad,NOW)
        for field,value in [('confirm','false'),('interval','60'),('end',NOW),('high','1'),('close','nan')]:
            bad=message();bad['data'][0][field]=value
            with self.assertRaises(ValueError): self.store.ingest_ws(self.epoch,bad,NOW)

    def test_incomplete_or_wrong_rest_does_not_clear_gap(self):
        for change in ('missing','duplicate','symbol','future'):
            bad=rest()
            if change=='missing': bad['result']['list'].pop()
            if change=='duplicate': bad['result']['list'][-1]=bad['result']['list'][0]
            if change=='symbol': bad['result']['symbol']='GRASSUSDT'
            if change=='future': bad['time']=NOW+1
            with self.assertRaises(ValueError): self.store.ingest_rest(self.epoch,self.topics[0],bad,NOW,NOW)
        self.assertTrue(self.store.states()[0]['gap'])

    def test_scope_and_subscription_limits_141_assets_plus_btc(self):
        names=[topic(f'ASSET{n:03}USDT',i) for n in range(141) for i in LIMITS]+self.topics
        requests=stream.subscriptions(names,'epoch')
        self.assertEqual(len(requests),29)
        self.assertTrue(all(len(r['args'])<=10 for r in requests))
        self.assertEqual([t for r in requests for t in r['args']],names)
        self.assertAlmostEqual(stream.plan(names)['admission_floor_seconds'],56.6)
        with self.assertRaises(ValueError): stream.subscriptions(names*10,'epoch')

    def test_roster_is_only_captured_scope_plus_btc(self):
        snap=dict(schema='hunter_bybit_worker_v1',source='OFFICIAL_BYBIT_V5_VIA_WORKER',
                  captured_at_utc=dt.datetime.fromtimestamp(NOW/1000,dt.timezone.utc).isoformat(),
                  rows=[dict(base='GRASS',pair='GRASSUSDT',venue='bybit',price=1,volume_24h_usdt=1,change_24h_pct=1)],
                  venue_status=dict(valid_pairs=1,active_pairs=1,missing_or_invalid=[],signal_expected_bases=['GRASS']),
                  early_signals={})
        snap['snapshot_sha256']=digest(snap)
        got=stream.roster(snap,dt.datetime.fromtimestamp(NOW/1000,dt.timezone.utc))
        self.assertEqual(set(got),set(self.topics+[topic('GRASSUSDT',i) for i in LIMITS]))
        with self.assertRaises(ValueError): stream.roster(snap,dt.datetime.fromtimestamp(NOW/1000+901,dt.timezone.utc))

    def test_repair_exact_url_and_unified_retry_budget(self):
        clock=[NOW];urls=[]
        def sleep(s): clock[0]+=int(s*1000)
        def get(url):
            urls.append(url)
            if len(urls)<3:return 503,b'fixture',1
            end=int(stream.urllib.parse.parse_qs(stream.urllib.parse.urlparse(url).query)['end'][0])
            return 200,json.dumps(rest(end=end)).encode(),1
        stream.repair(self.store,self.epoch,self.topics[0],NOW+20_000,get,lambda:clock[0],sleep)
        self.assertEqual(len(urls),3)
        self.assertTrue(all(u.startswith(stream.REST_URL+'?category=spot&symbol=BTCUSDT&interval=15&limit=25&end=') for u in urls))
        self.assertEqual(Budget(self.store).status()['next_ms'],clock[0]+200)

    def test_403_has_no_retry_or_worker_fallback(self):
        for payload,expected in [(b'access too frequent','GLOBAL_COOLDOWN'),(b'country','COUNTRY_403'),(b'unknown','UNKNOWN_403')]:
            store=Store(Path(self.temp.name)/(expected+'.db'));epoch=store.begin_session(self.topics,'x')
            calls=[]
            def get(url): calls.append(url);return 403,payload,1
            with self.assertRaisesRegex(RuntimeError,expected):
                stream.repair(store,epoch,self.topics[0],NOW+1000,get,lambda:NOW,lambda s:None)
            self.assertEqual(len(calls),1)

    def test_retry_exhaustion_and_nonretryable_api_error(self):
        clock=[NOW];calls=[]
        def sleep(s):clock[0]+=int(s*1000)
        def get(url):calls.append(url);return 429,b'',1
        with self.assertRaisesRegex(RuntimeError,'EXHAUSTED'):
            stream.repair(self.store,self.epoch,self.topics[0],NOW+10000,get,lambda:clock[0],sleep)
        self.assertEqual(len(calls),3)
        clock[0]+=10000
        with self.assertRaisesRegex(ValueError,'BYBIT_RET_REJECTED'):
            stream.repair(self.store,self.epoch,self.topics[0],clock[0]+1000,
                          lambda u:(200,b'{"retCode":10001}',1),lambda:clock[0],sleep)

    def test_fixed_host_transport_and_no_redirect(self):
        with self.assertRaises(ValueError):stream.direct_get('https://other.invalid/')
        with self.assertRaises(ValueError):stream.NoRedirect().redirect_request(None,None,None,None,None,None)

    def test_reconnect_budget_is_bounded(self):
        attempts=[]
        def connect():attempts.append(1);raise OSError('fixture')
        with self.assertRaisesRegex(RuntimeError,'CONNECTION_BUDGET_EXHAUSTED'):
            stream.collect(self.store,self.topics,'scope',connect,lambda:NOW,lambda s:None)
        self.assertEqual(len(attempts),5)

    def test_real_session_loop_acks_pings_repairs_then_disconnects(self):
        clock=[NOW];received=[0];sent=[]
        class WS:
            def send(_,payload):sent.append(json.loads(payload))
            def recv(_,timeout):
                received[0]+=1
                if received[0]==1:
                    return json.dumps(dict(op='subscribe',req_id=sent[0]['req_id'],success=True))
                clock[0]+=20_000
                return json.dumps({'op':'pong'})
        repairs=[]
        def repairer(store,epoch,name,deadline):
            repairs.append(name)
            symbol,interval=stream.split_topic(name)
            store.ingest_rest(epoch,name,rest(symbol,interval,clock[0]),clock[0],clock[0])
        stream.session(self.store,self.topics,'x',WS(),lambda:received[0]>=3,
                       lambda:clock[0],lambda s:None,repairer)
        self.assertTrue(any(x['op']=='ping' for x in sent))
        self.assertEqual(self.read()['status'],'UNKNOWN')


if __name__ == '__main__':
    unittest.main()
