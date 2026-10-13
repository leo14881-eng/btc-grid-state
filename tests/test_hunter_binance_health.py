"""Health/data separation, identity, validity and durable shutdown: offline SQLite."""
import copy
import contextlib
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import json
from pathlib import Path
import queue
import tempfile
from threading import Event, Lock
import time
import unittest
from unittest.mock import patch
import urllib.parse

from research import hunter_binance_health as health
from research import hunter_binance_stream as stream
from research.hunter_binance_health_store import Store,proof,valid_proof,identity,CONTRACT
from research.hunter_binance_stream_store import topic,split_topic,STEPS,LIMITS,Budget,content_hash
from research.hunter_binance_stream_consumer import Frozen
from research.hunter_binance_http_child import allowed
from scripts.hunter_binance_health_check import Window,read_sample
from test_hunter_binance_cache import rows as old_rows,message as old_message

NOW=1791858000000
HEAD='b'*40


def rows(interval,stamp):
    value=old_rows(interval,stamp)
    for row in value:row[4]='101'
    return value


def message(symbol,interval,stamp,**kwargs):
    value=old_message(symbol,interval,stamp,**kwargs);value['k']['c']='101'
    return value


def directory(symbols,status='TRADING'):
    return dict(symbols=[dict(symbol=s,baseAsset=s[:-4],quoteAsset='USDT',status=status,
                              isSpotTradingAllowed=True) for s in symbols])


class StoreTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.store=Store(Path(temp.name)/'state.sqlite3');self.store.enable_health()
        self.names=sorted([topic('BTCUSDT','5m'),topic('KGSTUSDT','1h'),topic('XUSDUSDT','1h')])
        self.protocol=health.Protocol(self.store,self.names,'a'*64,NOW)
        self.epoch=self.protocol.epoch;self.store.ack(self.epoch,self.names)
        with self.store.db() as db:db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('health_source_head',HEAD))

    def status(self,names=None,stamp=NOW,status='TRADING'):
        names=names or self.names
        self.store.instrument_batch(self.epoch,names,directory(sorted({split_topic(n)[0] for n in names}),status),stamp,stamp)

    def seed(self,names=None,stamp=NOW+1):
        for name in names or self.names:
            _,interval=split_topic(name)
            self.store.ingest_rest(self.epoch,name,rows(interval,stamp),stamp,stamp)

    def frame(self,name,stamp=NOW+2,msg=None):
        symbol,interval=split_topic(name);value=msg or message(symbol,interval,stamp)
        shard=self.protocol.topic_shards[name]
        self.protocol.arrived(shard,json.dumps(value),stamp)
        self.protocol.receive_batch([(shard,value,stamp)])
        return value

    def fake_health(self,stamp=NOW+2):
        self.protocol.alive(0,stamp,'MATCHING_PONG')
        jobs=type('Work',(),dict(active={},seeded=set(self.names),completed=4,failures={},
            diagnostics=lambda _,now:dict(work_progress={},exhausted_work=[],oldest_active_ms=0)))()
        with patch.object(stream,'emit'):
            return health.sample(self.store,self.protocol,self.names,jobs,stamp,0)

    def test_quiet_shard_health_never_grants_data_ready(self):
        self.status();self.seed()
        value=self.fake_health()
        self.assertEqual((value['service_status'],value['data_status']),('DEGRADED','UNKNOWN'))
        self.assertEqual(self.store.freeze(self.names,NOW+2,NOW+2)['status'],'UNKNOWN')

    def test_qualified_monitor_subset_and_full_discovery_unknown(self):
        self.status();self.seed();btc=topic('BTCUSDT','5m');self.frame(btc)
        one=self.store.freeze([btc],NOW+2,NOW+2)
        self.assertEqual(one['status'],'READY');Frozen(one,checked_at=NOW+2)
        self.assertEqual(self.store.freeze(self.names,NOW+2,NOW+2)['status'],'UNKNOWN')

    def test_matching_quiet_rest_is_label_only_not_current_ws(self):
        self.status();self.seed();name=self.names[1]
        result=self.store.verify_quiet(self.epoch,name,rows('1h',NOW+10),NOW+10,NOW+11)
        self.assertTrue(valid_proof(result,'quiet',name,self.store.metadata(),NOW+12))
        self.assertEqual(self.store.freeze([name],NOW+12,NOW+12)['status'],'UNKNOWN')

    def test_quiet_mismatch_repairs_only_one_topic_and_needs_reverification(self):
        self.status();self.seed();name=self.names[1]
        changed=rows('1h',NOW+10);changed[-2][4]='102'
        value=self.store.verify_quiet(self.epoch,name,changed,NOW+10,NOW+11)
        self.assertEqual(value['reason'],'CLOSED_BAR_MISMATCH')
        self.assertFalse(valid_proof(value,'quiet',name,self.store.metadata(),NOW+11))
        other=self.store.get_proof('quiet',self.names[2]);self.assertIsNone(other)
        value=self.store.verify_quiet(self.epoch,name,changed,NOW+12,NOW+13)
        self.assertTrue(valid_proof(value,'quiet',name,self.store.metadata(),NOW+13))

    def test_quiet_compares_closed_bar_not_changing_current_bar(self):
        self.status();self.seed();name=self.names[1]
        changed=rows('1h',NOW+10);changed[-1][4]='102'
        value=self.store.verify_quiet(self.epoch,name,changed,NOW+10,NOW+11)
        self.assertEqual(value['result'],'VALID')

    def test_expiry_and_binding_changes_reject_old_proofs(self):
        self.status();self.seed();name=self.names[1]
        value=self.store.verify_quiet(self.epoch,name,rows('1h',NOW+10),NOW+10,NOW+11)
        meta=self.store.metadata()
        self.assertFalse(valid_proof(value,'quiet',name,meta,NOW+90011))
        for field in ('epoch','roster_hash','scope_hash','health_epoch_started'):
            altered=dict(meta);altered[field]=NOW+20 if field=='health_epoch_started' else 'f'*64
            self.assertFalse(valid_proof(value,'quiet',name,altered,NOW+12),field)
        for field,new in [('market','perpetual'),('exchange','BYBIT'),('interval','15m'),
                          ('symbol','BTCUSDT'),('kind','kline'),('source','OFFICIAL_BINANCE_SPOT_WS')]:
            altered=dict(value);altered[field]=new
            self.assertFalse(valid_proof(altered,'quiet',name,meta,NOW+12),field)

    def test_metadata_nontrading_is_invalid_and_required_symbol_is_retained(self):
        self.status(status='BREAK');self.seed()
        for name in self.names:self.frame(name)
        result=self.store.freeze(self.names,NOW+2,NOW+2)
        self.assertEqual(result['reason'],'BINANCE_TRADING_STATUS_UNKNOWN_OR_INVALID')
        self.assertEqual({s['topic'] for s in self.store.states()},set(self.names))
        value=self.store.verify_quiet(self.epoch,self.names[1],rows('1h',NOW+10),NOW+10,NOW+11)
        self.assertEqual(value['reason'],'TRADING_STATUS_UNVERIFIED')

    def test_cross_boundary_without_ws_never_infers_closed_or_current(self):
        self.status();self.seed();name=topic('BTCUSDT','5m');self.frame(name)
        boundary=(NOW//STEPS['5m']+1)*STEPS['5m']
        self.status(stamp=boundary)
        self.assertEqual(self.store.freeze([name],boundary,boundary)['status'],'UNKNOWN')
        self.store.ingest_rest(self.epoch,name,rows('5m',boundary),boundary,boundary)
        self.assertEqual(self.store.freeze([name],boundary,boundary)['status'],'UNKNOWN')

    def test_reconnect_retains_bodies_not_old_validity_or_ws_proof(self):
        self.status();self.seed();name=self.names[1];self.frame(name)
        old=self.store.get_proof('instrument',name)
        with self.store.db(readonly=True) as db:count=db.execute('SELECT COUNT(*) FROM bars').fetchone()[0]
        second=health.Protocol(self.store,self.names,'a'*64,NOW+10)
        with self.store.db(readonly=True) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM bars').fetchone()[0],count)
        self.assertIsNone(self.store.get_proof('instrument',name))
        self.assertFalse(valid_proof(old,'instrument',name,self.store.metadata(),NOW+10))
        self.assertEqual(self.store.freeze([name],NOW+10,NOW+10)['status'],'UNKNOWN')
        with self.assertRaises(ValueError):self.store.instrument_batch(second.epoch,self.names,
            directory(['BTCUSDT','KGSTUSDT','XUSDUSDT']),NOW,NOW+11)

    def test_duplicate_conflict_new_event_and_invalid_have_distinct_counts(self):
        self.status();self.seed();name=self.names[1]
        original=self.frame(name);self.frame(name,msg=original)
        conflict=copy.deepcopy(original);conflict['k']['x']=True;self.frame(name,msg=conflict)
        self.frame(name,stamp=NOW+3)  # Same OHLC but a new event must commit.
        invalid=message('KGSTUSDT','1h',NOW+4);invalid['k']['h']='0'
        self.frame(name,stamp=NOW+4,msg=invalid)
        row=self.store.counts(self.epoch)[name]
        self.assertEqual((row['committed'],row['duplicate'],row['invalid']),(2,1,2))
        with patch.object(stream,'emit'):final=self.protocol.final('test')
        self.assertTrue(final['complete']);self.assertEqual(final['received'],5)

    def test_shutdown_drain_commits_queued_frames_before_final(self):
        self.status();self.seed();messages=queue.Queue()
        for name in self.names:
            symbol,interval=split_topic(name);m=message(symbol,interval,NOW+2)
            self.protocol.arrived(0,json.dumps(m),NOW+2);messages.put((0,m,NOW+2))
        health.drain(self.protocol,messages,5,steady=lambda:0)
        with patch.object(stream,'emit'):final=self.protocol.final('test')
        self.assertEqual((final['committed'],final['unprocessed_recorded']),(3,0));self.assertTrue(final['complete'])

    def test_shutdown_deadline_persists_every_receipt_and_scoped_repair_resolves(self):
        self.status();self.seed();messages=queue.Queue()
        for name in self.names:
            symbol,interval=split_topic(name);m=message(symbol,interval,NOW+2)
            self.protocol.arrived(0,json.dumps(m),NOW+2);messages.put((0,m,NOW+2))
        health.drain(self.protocol,messages,0,steady=lambda:0)
        with patch.object(stream,'emit'):final=self.protocol.final('test')
        self.assertEqual((final['received'],final['unprocessed_recorded']),(3,3));self.assertTrue(final['complete'])
        with self.store.db(readonly=True) as db:
            saved=[dict(r) for r in db.execute('SELECT * FROM bn_unprocessed')]
        self.assertEqual(len(saved),3)
        self.assertTrue(all(len(r['payload_sha256'])==64 and r['received_at']==NOW+2 and r['resolved_at'] is None for r in saved))
        self.protocol=health.Protocol(self.store,self.names,'a'*64,NOW+3)
        self.epoch=self.protocol.epoch;self.store.ack(self.epoch,self.names);self.status(stamp=NOW+3)
        name=self.names[1];self.frame(name,stamp=NOW+3)
        self.assertEqual(self.store.freeze([name],NOW+3,NOW+3)['reason'],'BINANCE_UNRECONCILED_RECEIPT')
        self.store.ingest_rest(self.epoch,name,rows('1h',NOW+4),NOW+4,NOW+4)
        self.frame(name,stamp=NOW+5)
        self.assertEqual(self.store.freeze([name],NOW+5,NOW+5)['status'],'READY')
        self.assertEqual(self.store.freeze([self.names[2]],NOW+5,NOW+5)['reason'],'BINANCE_UNRECONCILED_RECEIPT')

    def test_non_native_receipt_never_resolves_by_range_or_loses_event_key(self):
        self.status();self.seed();name=self.names[1]
        invalid=message('KGSTUSDT','1h',NOW+2);invalid['k']['t']-=3599999
        self.frame(name,msg=invalid)
        self.store.ingest_rest(self.epoch,name,rows('1h',NOW+4),NOW+4,NOW+4)
        with self.store.db(readonly=True) as db:r=db.execute('SELECT * FROM bn_unprocessed').fetchone()
        self.assertEqual(r['event_time'],NOW+2);self.assertIsNone(r['resolved_at'])

    def test_draining_immediately_revokes_ready_and_acceptance_but_preserves_receipts(self):
        self.status();self.seed();name=self.names[1];self.frame(name);self.fake_health()
        self.assertEqual(self.store.freeze([name],NOW+2,NOW+2)['status'],'READY')
        self.store.start_draining(self.epoch)
        self.assertEqual(self.store.freeze([name],NOW+2,NOW+2)['status'],'UNKNOWN')
        self.assertFalse(read_sample(self.store,self.names,'a'*64,HEAD,NOW+2).get('health_verified',False))
        self.frame(name,stamp=NOW+3)
        self.store.disconnect(self.epoch)
        with patch.object(stream,'emit'):self.assertTrue(self.protocol.final('test')['complete'])
        with self.assertRaises(ValueError):self.fake_health(NOW+4)

    def test_reconnect_during_read_and_expired_finish_cannot_verify_old_health(self):
        self.status();self.seed();self.fake_health();original=self.store.freeze
        def reconnect(*args,**kwargs):
            health.Protocol(self.store,self.names,'a'*64,NOW+3)
            return original(*args,**kwargs)
        with patch.object(self.store,'freeze',side_effect=reconnect):
            result=read_sample(self.store,self.names,'a'*64,HEAD,NOW+3)
        self.assertFalse(result.get('health_verified',False));self.assertEqual(result['reason'],'HEALTH_FREEZE_BINDING_CHANGED')
        self.protocol=health.Protocol(self.store,self.names,'a'*64,NOW+4);self.epoch=self.protocol.epoch
        self.store.ack(self.epoch,self.names);self.fake_health(NOW+4)
        result=read_sample(self.store,self.names,'a'*64,HEAD,NOW+4,clock=lambda:NOW+31005)
        self.assertFalse(result.get('health_verified',False))

    def test_quiet_mismatch_blocks_even_new_ws_until_later_clean_verification(self):
        self.status();self.seed();name=self.names[1]
        changed=rows('1h',NOW+10);changed[-2][4]='102'
        self.store.verify_quiet(self.epoch,name,changed,NOW+10,NOW+11)
        self.frame(name,stamp=NOW+12)
        self.assertEqual(self.store.freeze([name],NOW+12,NOW+12)['reason'],'BINANCE_QUIET_REVERIFICATION_REQUIRED')
        self.store.verify_quiet(self.epoch,name,changed,NOW+13,NOW+14)
        self.assertEqual(self.store.freeze([name],NOW+14,NOW+14)['status'],'READY')

    def test_control_and_unrouteable_frames_have_separate_conservation(self):
        for raw in ('[]','not-json',json.dumps(dict(e='kline',s='WRONGUSDT',k=dict(i='1h')))):
            with self.assertRaises((ValueError,KeyError)):self.protocol.arrived(0,raw,NOW+1)
        with patch.object(stream,'emit'):final=self.protocol.final('test')
        self.assertEqual((final['received'],final['unattributed_invalid']),(0,3));self.assertTrue(final['complete'])

    def test_handshake_denial_is_durable_and_never_retried(self):
        boot=patch('research.hunter_bybit_stream_store.boot_id',return_value='offline-boot');boot.start();self.addCleanup(boot.stop)
        for status in (403,418):
            with tempfile.TemporaryDirectory() as folder:
                store=Store(Path(folder)/'cache.sqlite3')
                error=RuntimeError('handshake rejected');error.response=type('Response',(),dict(status_code=status))()
                with patch.object(health,'session',side_effect=error) as session,patch.object(stream,'emit'):
                    with self.assertRaises(health.DurableStopError):
                        health.collect(store,self.names,'a'*64,HEAD,sleep=lambda _:None)
                self.assertEqual(session.call_count,1);self.assertIsNotNone(Budget(store).status()['halt'])

    def test_handshake_rate_limit_uses_shared_cooldown(self):
        boot=patch('research.hunter_bybit_stream_store.boot_id',return_value='offline-boot');boot.start();self.addCleanup(boot.stop)
        error=RuntimeError('throttled');error.response=type('Response',(),dict(status_code=429))()
        with patch.object(health,'session',side_effect=error) as session,patch.object(stream,'emit'):
            with self.assertRaises(health.DurableStopError):
                health.collect(self.store,self.names,'a'*64,HEAD,sleep=lambda _:None)
        self.assertEqual(session.call_count,1);self.assertGreater(Budget(self.store).status()['cooldown_ms'],NOW)

    def test_instrument_expiry_during_consumer_read_rejects_ready(self):
        self.status();self.seed();name=self.names[1];self.frame(name,stamp=NOW+80000)
        generation=self.store.freeze([name],NOW+89999,NOW+89999)
        self.assertEqual(generation['status'],'READY')
        with self.assertRaises(ValueError):Frozen(generation,checked_at=NOW+90001)

    def test_quiet_proof_cannot_cross_native_boundary(self):
        name=topic('BTCUSDT','5m');stamp=NOW+STEPS['5m']-1000
        self.status(stamp=stamp);self.seed(stamp=stamp)
        value=self.store.verify_quiet(self.epoch,name,rows('5m',stamp),stamp,stamp)
        self.assertTrue(valid_proof(value,'quiet',name,self.store.metadata(),stamp))
        self.assertFalse(valid_proof(value,'quiet',name,self.store.metadata(),stamp+1001))

    def test_quiet_http_crossing_boundary_cannot_clear_mismatch_obligation(self):
        name=topic('BTCUSDT','5m');stamp=NOW+STEPS['5m']-1000
        self.status(stamp=stamp);self.seed(stamp=stamp)
        changed=rows('5m',stamp);changed[-2][4]='102'
        self.store.verify_quiet(self.epoch,name,changed,stamp,stamp)
        value=self.store.verify_quiet(self.epoch,name,changed,stamp+999,stamp+1001)
        self.assertEqual(value['result'],'UNKNOWN')
        self.assertEqual(value['reason'],'VERIFICATION_EXPIRED_OR_NATIVE_BOUNDARY')
        with self.store.db(readonly=True) as db:
            self.assertIsNotNone(db.execute('SELECT 1 FROM metadata WHERE kind=?',('quiet_reverify:'+name,)).fetchone())

    def test_kline_validity_expiry_and_wrong_kind_rejected_by_consumer(self):
        self.status();self.seed();name=self.names[1];self.frame(name)
        generation=self.store.freeze([name],NOW+2,NOW+2)
        Frozen(generation,checked_at=NOW+2)
        with self.assertRaises(ValueError):Frozen(generation,checked_at=NOW+90003)
        bad=copy.deepcopy(generation);bad['windows'][name]['validity']['kind']='quiet'
        with self.assertRaises(ValueError):Frozen(bad,checked_at=NOW+2)

    def test_unicode_and_multiplier_identity_full_path(self):
        for symbol in ('币安人生USDT','牛来USDT','1000CATUSDT'):
            name=topic(symbol,'15m');self.assertEqual(split_topic(name),(symbol,'15m'))
            args=dict(symbol=symbol,interval='15m',limit='25',endTime=str(NOW))
            parsed=allowed('https://data-api.binance.vision/api/v3/klines?'+urllib.parse.urlencode(args))
            self.assertEqual(dict(urllib.parse.parse_qsl(parsed.query))['symbol'],symbol)
            p=health.Protocol(self.store,[name],'a'*64,NOW);self.store.ack(p.epoch,[name])
            self.store.instrument_batch(p.epoch,[name],directory([symbol]),NOW,NOW)
            self.store.ingest_rest(p.epoch,name,rows('15m',NOW+1),NOW+1,NOW+1)
            value=message(symbol,'15m',NOW+2);p.arrived(0,json.dumps(value),NOW+2);p.receive_batch([(0,value,NOW+2)])
            g=self.store.freeze([name],NOW+2,NOW+2);self.assertEqual(g['status'],'READY')
            self.assertEqual(g['windows'][name]['validity']['symbol'],symbol)
            with self.assertRaises(ValueError):identity(symbol,'15m',market='perpetual')

    def test_directory_request_scope_and_weight_share_governor(self):
        params=dict(symbols=json.dumps(['币安人生USDT','1000CATUSDT'],ensure_ascii=False),showPermissionSets='false')
        allowed('https://data-api.binance.vision/api/v3/exchangeInfo?'+urllib.parse.urlencode(params))
        with self.assertRaises(ValueError):allowed('https://data-api.binance.vision/api/v3/exchangeInfo?symbol=BTCUSDT')
        budget=Budget(self.store);token=budget.acquire(NOW,weight=20)
        budget.release(token)
        self.assertIsNone(budget.acquire(NOW+1999))
        token=budget.acquire(NOW+2000);self.assertIsNotNone(token);budget.release(token)

    def test_supervisor_accepts_healthy_degraded_300s_without_false_ready(self):
        self.status();self.seed();window=Window();last=None
        for elapsed in range(0,311,10):
            now=NOW+elapsed*1000
            self.fake_health(now)
            value=read_sample(self.store,self.names,'a'*64,HEAD,now)
            last=window.observe(value,elapsed)
        self.assertTrue(last['collector_accepted']);self.assertEqual(last['data_status'],'UNKNOWN')
        self.assertFalse(last['publication_verified'])

    def test_supervisor_replay_old_epoch_silent_shard_and_clock_jump_fail(self):
        self.status();self.seed();self.fake_health(NOW+2)
        value=read_sample(self.store,self.names,'a'*64,HEAD,NOW+2);window=Window()
        for elapsed in range(0,311,10):
            self.assertFalse(window.observe(value,elapsed)['collector_accepted'])
        self.assertEqual(read_sample(self.store,self.names,'a'*64,HEAD,NOW+91000)['service_status'],'UNKNOWN')
        self.assertEqual(read_sample(self.store,self.names,'a'*64,'c'*40,NOW+2)['service_status'],'UNKNOWN')
        window=Window()
        for elapsed in range(0,311,10):
            v=dict(value,observed_at=NOW+elapsed*1000)
            self.assertFalse(window.observe(v,elapsed/1000)['collector_accepted'])


if __name__=='__main__':unittest.main()
