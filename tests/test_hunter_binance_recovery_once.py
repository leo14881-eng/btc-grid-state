"""Small real-SQLite/flock checks; exchange transport is injected, no market IO."""
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from scripts import hunter_binance_recover_once as recovery
from research import hunter_binance_public as public
from research.hunter_binance_stream_store import Store,Budget


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        import fcntl
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'state.sqlite3'; self.store=Store(self.path)
        self.token=Budget(self.store).acquire()
        handle=(self.path.parent/('request-'+self.token+'.lock')).open('xb')
        self.addCleanup(handle.close); fcntl.flock(handle,fcntl.LOCK_EX | fcntl.LOCK_NB)
        with self.store.db() as db:
            db.execute('INSERT INTO requests(token) VALUES(?)',(self.token,))
            db.executemany('INSERT OR REPLACE INTO meta VALUES(?,?)',[('epoch',recovery.EPOCH),('connected','0')])
        self.before=recovery.read_state(self.path)
        self.boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        self.calls=[]

    def probe(self,transport):
        client=public.Client; output=io.StringIO()
        def exchange(*args): self.calls.append(1); return transport(*args)
        with patch.object(public,'exchange',exchange),\
             patch.object(public,'Client',side_effect=lambda **kwargs:client(self.store,**kwargs)),\
             contextlib.redirect_stdout(output):
            status=recovery.probe_once()
        return status,json.loads(output.getvalue())

    def response(self,url,*args):
        from urllib.parse import urlsplit,parse_qs
        end=int(parse_qs(urlsplit(url).query)['endTime'][0]); step=300000
        rows=[[(end//step-i)*step,'100','102','99','101','10',(end//step-i+1)*step-1,'1000',1,'5','500','0']
              for i in reversed(range(16))]
        return 200,json.dumps(rows).encode(),1

    def test_default_plan_does_not_load_helper_or_execute(self):
        with patch.object(sys,'argv',['recovery']),patch.object(recovery,'helper') as helper,\
             patch.object(recovery,'command') as command,contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(recovery.main(),0)
        helper.assert_not_called(); command.assert_not_called()

    def test_consistent_backup_preserves_source_rows(self):
        receipt=recovery.consistent_backup(self.path)
        self.assertEqual(receipt['integrity'],'ok')
        self.assertEqual(recovery.read_state(Path(receipt['path'])),self.before)
        self.assertEqual(recovery.read_state(self.path),self.before)

    def test_incident_changes_are_rejected(self):
        for field in ('denial','halt','cooldown','epoch','clock','extra'):
            value=copy.deepcopy(self.before)
            if field=='denial':value['requests'][0]['denied']=1
            if field=='halt':value['budget']['halt']='UNKNOWN_403_MANUAL_REVIEW'
            if field=='cooldown':value['budget']['cooldown_ms']=time.monotonic_ns()//1000000+60000
            if field=='epoch':value['meta']['epoch']='other'
            if field=='clock':value['clock'][0]['boot']='other'
            if field=='extra':value['leases'].append(dict(value['leases'][0]))
            with self.subTest(field=field),self.assertRaises(RuntimeError):
                recovery.validate_incident(value,self.boot,time.monotonic_ns()//1000000)

    def test_success_is_one_get_and_retains_original_marker(self):
        status,receipt=self.probe(self.response)
        self.assertEqual(status,0); self.assertTrue(receipt['success'])
        self.assertEqual((len(self.calls),receipt['http_attempts'],receipt['rows']),(1,1,16))
        after=recovery.read_state(self.path)
        self.assertEqual(after['requests'],self.before['requests'])
        self.assertEqual(after['leases'],self.before['leases'])

    def test_429_never_retries_and_keeps_cooldown(self):
        status,receipt=self.probe(lambda *args:(429,b'[]',120))
        self.assertEqual((status,receipt['http_status'],len(self.calls)),(1,429,1))
        state=Budget(self.store).status()
        self.assertGreater(state['cooldown_ms'],time.time_ns()//1000000+110000)
        self.assertEqual(recovery.read_state(self.path)['requests'],self.before['requests'])

    def test_403_remains_persistent(self):
        def denied(url,deadline,cancel,on_denial,before_go):
            on_denial(); return 403,b'country',1
        status,receipt=self.probe(denied)
        self.assertEqual((status,len(self.calls)),(1,1))
        self.assertIn('403',Budget(self.store).status()['halt'])

    def test_418_remains_persistent(self):
        def denied(url,deadline,cancel,on_denial,before_go):
            on_denial(); return 418,b'ban',600
        status,receipt=self.probe(denied)
        self.assertEqual((status,receipt['http_status'],len(self.calls)),(1,418,1))
        self.assertIn('403',Budget(self.store).status()['halt'])

    def test_unknown_outcome_retains_both_requests(self):
        def unknown(*args):raise public.UncertainRequestError('PUBLIC_HTTP_OUTCOME_UNKNOWN_MANUAL_REVIEW')
        status,receipt=self.probe(unknown)
        self.assertEqual((status,len(self.calls)),(1,1)); self.assertFalse(receipt['success'])
        after=recovery.read_state(self.path)
        self.assertEqual((len(after['requests']),len(after['leases'])),(2,2))
        self.assertIn('ORPHANED',Budget(self.store).status()['halt'])

    def test_invalid_200_does_not_authorize_disposition(self):
        status,receipt=self.probe(lambda *args:(200,b'[]',1))
        self.assertEqual((status,len(self.calls)),(1,1)); self.assertFalse(receipt['success'])
        self.assertEqual(recovery.read_state(self.path)['requests'],self.before['requests'])

    def test_disposition_removes_only_original_rows_and_preserves_budget(self):
        self.assertEqual(self.probe(self.response)[0],0)
        before_disposition=recovery.read_state(self.path); output=io.StringIO()
        with patch.object(Store,'reader',return_value=self.store),\
             patch.object(sys,'stdin',io.StringIO(json.dumps(self.before))),contextlib.redirect_stdout(output):
            recovery.dispose_one()
        after=recovery.read_state(self.path)
        self.assertEqual((after['requests'],after['leases']),([],[]))
        self.assertEqual(after['budget'],before_disposition['budget'])
        self.assertEqual(after['meta'],self.before['meta'])
        self.assertIn('HISTORICAL_HTTP_OUTCOME_UNKNOWN',output.getvalue())

    def test_disposition_changed_record_is_rolled_back(self):
        with self.store.db() as db:db.execute('UPDATE requests SET denied=1')
        with patch.object(Store,'reader',return_value=self.store),\
             patch.object(sys,'stdin',io.StringIO(json.dumps(self.before))),self.assertRaisesRegex(RuntimeError,'COMPARE_AND_SWAP'):
            recovery.dispose_one()
        after=recovery.read_state(self.path)
        self.assertEqual((len(after['requests']),len(after['leases'])),(1,1))
        self.assertEqual(after['requests'][0]['denied'],1)


if __name__=='__main__':unittest.main()
