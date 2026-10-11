"""Default path equivalence and direct public metadata, no external IO."""
import contextlib
import copy
import datetime as dt
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from research import hunter_bybit_public as public
from research import hunter_bybit_direct as direct
from research import hunter_bybit_worker as worker
from research import hunter_bybit_availability as availability
from research import hunter_bybit_stream_store as storage
from research import hunter_bybit_stream_consumer as consumer
from research import hunter_bybit_stream as stream
from research import hunter_cex_scan as scan
from test_hunter_stream import NOW,rest,message


class PublicTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=storage.Store(Path(self.temp.name)/'state.sqlite3')
        self.now=dt.datetime.fromtimestamp((NOW+1)/1000,dt.timezone.utc)
        self.bases=['BTC','ETH','GRASS','TA'];self.calls=[]
        self.list_bases=self.bases[:];self.tick_bases=self.bases[:]
        self.client=public.Client(self.store,self.transport,clock=lambda:self.now)

    def body(self,path):
        if path==direct.LIST_PATH:
            rows=[dict(symbol=b+'USDT',baseCoin=b,quoteCoin='USDT',status='Trading') for b in self.list_bases]
        else:
            rows=[dict(symbol=b+'USDT',lastPrice='101',turnover24h='10000',price24hPcnt='.01') for b in self.tick_bases]
        return dict(retCode=0,time=int(self.now.timestamp()*1000),result=dict(category='spot',list=rows))

    def transport(self,url,deadline,cancelled,on_denial,before_go):
        before_go();path=urlsplit(url).path;self.calls.append(path)
        return 200,json.dumps(self.body(path)).encode(),1

    def ready(self):
        names=consumer.topics_for(['GRASS','TA'])
        epoch=self.store.begin_session(names,'a'*64);self.store.ack(epoch,names)
        for name in names:
            symbol,interval=storage.split_topic(name)
            self.store.ingest_rest(epoch,name,rest(symbol,interval),NOW,NOW)
            self.store.ingest_ws(epoch,message(symbol,interval,NOW+1),NOW+1)
        return names

    def snapshot(self):
        with patch.object(worker,'DEFAULT',Path(self.temp.name)/'snapshot.json'):
            rows,status=direct.collect({'BTC','ETH'},self.now,self.client,self.store,lambda:NOW+1)
            return rows,status,json.loads(worker.DEFAULT.read_text())

    def test_unset_default_calls_exact_existing_worker_adapter(self):
        with patch.dict('os.environ',{'HUNTER_BYBIT_WORKER_MARKET_ENABLED':'1'},clear=True),\
             patch.object(worker,'collect',return_value='legacy') as old,\
             patch.object(direct,'collect',side_effect=AssertionError('direct forbidden')):
            self.assertEqual(scan.bybit_from_authorized_region({'BTC'}),'legacy')
            old.assert_called_once_with({'BTC'})

    def test_direct_selector_dispatches_without_worker_or_legacy_fallback(self):
        with patch.dict('os.environ',{'HUNTER_BYBIT_SPOT_SOURCE':'SERVER_DIRECT'},clear=True),\
             patch.object(direct,'collect',return_value='direct') as selected,\
             patch.object(worker,'collect',side_effect=AssertionError('worker forbidden')),\
             patch.object(scan,'bybit_with_fallback',side_effect=AssertionError('fallback forbidden')):
            self.assertEqual(scan.bybit_from_authorized_region({'BTC'}),'direct')
            selected.assert_called_once_with({'BTC'})

    def test_invalid_conflicting_or_disabled_selectors_fail_before_io(self):
        for env in ({'HUNTER_BYBIT_SPOT_SOURCE':'unknown'},
                    {'HUNTER_BYBIT_SPOT_SOURCE':'SERVER_DIRECT','HUNTER_BYBIT_DIRECT_DISABLED':'1'},
                    {'HUNTER_BYBIT_SPOT_SOURCE':'SERVER_DIRECT','HUNTER_BYBIT_KLINE_SOURCE':'WORKER'}):
            with patch.dict('os.environ',env,clear=True),self.assertRaises(ValueError):
                scan.bybit_from_authorized_region()

    def test_full_list_cache_shared_across_adapter_instances(self):
        first=direct.Adapter(self.now,self.client).listings()
        second=direct.Adapter(self.now,public.Client(storage.Store.reader(self.store.path),self.transport)).listings()
        self.assertEqual(first[0],self.bases);self.assertEqual(second[0],self.bases)
        self.assertFalse(first[1]['cache_hit']);self.assertTrue(second[1]['cache_hit'])
        self.assertEqual(self.calls,[direct.LIST_PATH])

    def test_list_expires_at_24h_and_future_cache_is_rejected(self):
        direct.Adapter(self.now,self.client).listings()
        with self.assertRaisesRegex(ValueError,'FUTURE'):
            direct.Adapter(self.now-dt.timedelta(seconds=1),self.client).listings()
        self.now+=dt.timedelta(days=1)
        self.assertFalse(direct.Adapter(self.now,self.client).listings()[1]['cache_hit'])
        self.assertEqual(len(self.calls),2)

    def test_bulk_ticker_is_fresh_per_generation_without_per_symbol_requests(self):
        for _ in range(2):direct.Adapter(self.now,self.client).fetch('/bybit/tickers')
        self.assertEqual(self.calls,[direct.TICK_PATH,direct.TICK_PATH])
        with self.assertRaises(ValueError):direct.Adapter(self.now,self.client).fetch('/bybit/early-klines',{})

    def test_ready_direct_snapshot_preserves_signals_and_truthful_sources(self):
        self.ready();rows,status,snapshot=self.snapshot()
        self.assertEqual(snapshot['source'],consumer.DIRECT_SOURCE)
        self.assertEqual(status['listing_source'],public.SOURCE)
        self.assertEqual(status['ticker_source'],public.SOURCE)
        self.assertEqual(status['kline_source'],consumer.SOURCE)
        self.assertEqual(set(snapshot['early_signals']),{'GRASS','TA'})
        self.assertEqual(self.calls,[direct.LIST_PATH,direct.TICK_PATH])
        worker.validate(snapshot,self.now)

    def test_direct_cold_bootstrap_is_unknown_and_valid_manifest_without_worker(self):
        rows,status,snapshot=self.snapshot()
        self.assertEqual(snapshot['cache_generation']['status'],'UNKNOWN')
        self.assertEqual(snapshot['early_signals'],{})
        self.assertEqual(stream.roster(snapshot,self.now),consumer.topics_for(['GRASS','TA']))

    def test_single_character_listing_matches_roster_and_http_contract(self):
        self.list_bases=self.tick_bases=['BTC','ETH','A']
        _,_,snapshot=self.snapshot()
        self.assertEqual(snapshot['cache_generation']['status'],'UNKNOWN')
        self.assertEqual(stream.roster(snapshot,self.now),consumer.topics_for(['A']))
        public.allowed('https://api.bybit.com/v5/market/kline?category=spot&symbol=AUSDT&interval=15&limit=25&end=1')

    def test_legacy_clock_blocks_cached_list_freeze_and_ws_before_io(self):
        names=self.ready()
        direct.Adapter(self.now,self.client).listings();self.calls=[]
        with self.store.db() as db:
            db.execute('DELETE FROM budget_clock')
        self.assertIn('MIGRATION',self.client.budget.status()['halt'])
        with self.assertRaisesRegex(RuntimeError,'GLOBAL_STOP'):
            direct.Adapter(self.now,self.client).listings()
        self.assertEqual(self.store.freeze(names,NOW+1,NOW+1)['status'],'UNKNOWN')
        with patch.object(stream,'open_ws',side_effect=AssertionError('WS forbidden')) as connect:
            with self.assertRaisesRegex(RuntimeError,'MIGRATION'):
                stream.collect(self.store,names,'a'*64,connect=connect)
            connect.assert_not_called()
        self.assertEqual(self.calls,[])

    def test_corrupt_request_marker_stops_before_opening_owner_path(self):
        with self.store.db() as db:
            db.execute("INSERT INTO requests(token) VALUES('../outside')")
        with patch.object(Path,'open',side_effect=AssertionError('owner path forbidden')):
            self.assertIn('MARKER_CORRUPT',self.client.budget.status()['halt'])
        with self.assertRaisesRegex(RuntimeError,'MARKER_CORRUPT'):
            self.client.request(direct.TICK_PATH)

    def test_cached_list_contradiction_refreshes_full_list_and_bulk_ticker_once(self):
        direct.Adapter(self.now,self.client).listings();self.calls=[]
        self.list_bases=self.tick_bases=['BTC','ETH','GRASS']
        rows,status,snapshot=self.snapshot()
        self.assertEqual(self.calls,[direct.TICK_PATH,direct.LIST_PATH,direct.TICK_PATH])
        self.assertEqual(status['listing_refresh']['removed_bases'],['TA'])
        self.assertEqual(status['ticker_requests'],2)

    def test_fresh_list_incomplete_ticker_never_uses_fallback(self):
        self.tick_bases=['BTC','ETH','GRASS']
        with self.assertRaisesRegex(ValueError,'TICKERS_INCOMPLETE'):self.snapshot()
        self.assertEqual(self.calls,[direct.LIST_PATH,direct.TICK_PATH])

    def test_public_list_partial_duplicate_empty_and_stale_are_rejected(self):
        for mode in ('partial','duplicate','empty','stale'):
            original=self.body
            def body(path):
                value=original(path)
                if mode=='partial':value['result']['nextPageCursor']='unexpected'
                if mode=='duplicate':value['result']['list'].append(value['result']['list'][0])
                if mode=='empty':value['result']['list']=[]
                if mode=='stale':value['time']-=120001
                return value
            with patch.object(self,'body',body),self.assertRaises((ValueError,RuntimeError)):
                direct.Adapter(self.now,self.client).listings(force_refresh=True)

    def test_corrupt_listing_cache_never_refreshes_around_corruption(self):
        direct.Adapter(self.now,self.client).listings();self.calls=[]
        with self.store.db() as db:db.execute("UPDATE metadata SET payload='{}'")
        with self.assertRaisesRegex(ValueError,'CORRUPT'):direct.Adapter(self.now,self.client).listings()
        self.assertEqual(self.calls,[])

    def test_rehashed_ticker_content_mismatch_is_rejected(self):
        self.ready();_,_,snapshot=self.snapshot()
        receipt=snapshot['public_metadata']['tickers'];receipt['body']['result']['list'][0]['lastPrice']='999'
        receipt['sha256']=storage.content_hash({k:v for k,v in receipt.items() if k!='sha256'})
        snapshot['snapshot_sha256']=worker.digest({k:v for k,v in snapshot.items() if k!='snapshot_sha256'})
        with self.assertRaisesRegex(ValueError,'TICKER_CONTENT'):worker.validate(snapshot,self.now)

    def test_availability_direct_spot_retains_alpha_call(self):
        adapter=direct.Adapter(self.now,self.client)
        with patch.dict('os.environ',{'HUNTER_BYBIT_SPOT_SOURCE':'SERVER_DIRECT'},clear=True),\
             patch.object(direct,'Adapter',return_value=adapter),\
             patch.object(availability,'spot_proxy_universe',side_effect=AssertionError('Worker forbidden')),\
             patch.object(availability,'alpha',return_value=(None,'CREDENTIALS_NOT_CONFIGURED')) as alpha,\
             patch.object(availability,'OUT',Path(self.temp.name)/'availability.json'),contextlib.redirect_stdout(io.StringIO()):
            availability.main();value=json.loads(availability.OUT.read_text())
        self.assertEqual(value['spot']['status'],'OK_FULL_UNIVERSE_BYBIT_DIRECT')
        self.assertNotIn('proxy',value['spot']);alpha.assert_called_once_with()

    def test_known_403_pending_halt_precedes_body_and_survives_restart(self):
        def denied(url,deadline,cancelled,on_denial,before_go):
            before_go();on_denial()
            self.assertIn('403_PENDING',self.client.budget.status()['halt'])
            return 403,b'country',1
        with self.assertRaisesRegex(RuntimeError,'COUNTRY_403'):
            public.Client(self.store,denied).request(direct.TICK_PATH)
        with self.assertRaisesRegex(RuntimeError,'COUNTRY_403'):
            public.Client(storage.Store.reader(self.store.path),self.transport).request(direct.LIST_PATH)
        self.assertEqual(self.calls,[])

    def test_uncertain_timeout_retains_marker_lease_and_blocks_restart(self):
        def uncertain(*args):raise public.UncertainRequestError('fixture')
        with self.assertRaises(public.UncertainRequestError):public.Client(self.store,uncertain).request(direct.TICK_PATH)
        self.assertIn('ORPHANED',self.client.budget.status()['halt'])
        with self.store.db(readonly=True) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM leases').fetchone()[0],1)
        with self.assertRaisesRegex(RuntimeError,'ORPHANED'):self.client.request(direct.TICK_PATH)
        self.assertEqual(self.calls,[])

    def test_frequency_403_cooldown_is_monotonic_despite_wall_clock_jump(self):
        def denied(url,deadline,cancelled,on_denial,before_go):
            before_go();on_denial();return 403,b'access too frequent',1
        with self.assertRaisesRegex(RuntimeError,'COOLDOWN'):
            public.Client(self.store,denied).request(direct.TICK_PATH)
        future=time.time_ns()+3600*10**9
        with patch.object(storage.time,'time_ns',return_value=future):
            self.assertIsNone(self.client.budget.acquire())
            self.assertGreater(self.client.budget.status()['cooldown_ms']-future//10**6,599000)

    def test_boot_change_and_legacy_budget_need_manual_review(self):
        token=self.client.budget.acquire();self.client.budget.release(token)
        with patch.object(storage,'boot_id',return_value='other-boot'):
            with self.assertRaisesRegex(RuntimeError,'BOOT_CHANGED'):self.client.budget.acquire()
            self.assertIn('BOOT_CHANGED',self.client.budget.status()['halt'])
        other=storage.Budget(storage.Store(Path(self.temp.name)/'legacy.db'))
        token=other.acquire(1000);other.release(token)
        with self.assertRaisesRegex(RuntimeError,'MIGRATION'):other.acquire()

    def test_failed_denial_persistence_holds_orphan_and_never_retries(self):
        real=self.store.db
        @contextlib.contextmanager
        def fail(*args,**kwargs):
            if kwargs.get('timeout')==0 and kwargs.get('readonly') is not True:
                with real(readonly=True) as db:
                    exists=db.execute('SELECT COUNT(*) FROM requests').fetchone()[0]
                if exists:raise sqlite3.OperationalError('fixture disk full')
            with real(*args,**kwargs) as db:yield db
        def denied(url,deadline,cancelled,on_denial,before_go):
            before_go()
            try:on_denial()
            except public.DurableStopError:
                # Observe before exchange unwinds or begins kill/wait/drain.
                other=storage.Budget(storage.Store.reader(self.store.path))
                self.assertIn('ORPHANED',other.status()['halt'])
                with self.assertRaisesRegex(RuntimeError,'ORPHANED'):other.acquire()
                raise
        with patch.object(self.store,'db',fail),self.assertRaises(public.DurableStopError):
            public.Client(self.store,denied).request(direct.TICK_PATH)
        self.assertIn('ORPHANED',self.client.budget.status()['halt'])

    def test_not_sent_cancellation_cleans_only_proven_unsent_request(self):
        def unsent(*args):raise public.NotSentError('before GO')
        with self.assertRaises(public.NotSentError):public.Client(self.store,unsent).request(direct.TICK_PATH)
        with self.store.db(readonly=True) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM leases').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM requests').fetchone()[0],0)


if __name__=='__main__':unittest.main()
