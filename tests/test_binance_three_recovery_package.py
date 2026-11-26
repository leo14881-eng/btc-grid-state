"""Exact runtime fixtures for three-record UNKNOWN archival and a single public probe."""
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from scripts import hunter_binance_recover_three_once as recovery
from research import hunter_binance_public as public
from research.hunter_binance_stream_store import Store,Budget


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        import fcntl
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'state.sqlite3'; self.store=Store(self.path)
        self.tokens=[f'{i+1:032x}' for i in range(3)]
        self.boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        for token in self.tokens:
            handle=(self.path.parent/('request-'+token+'.lock')).open('xb')
            self.addCleanup(handle.close);fcntl.flock(handle,fcntl.LOCK_EX | fcntl.LOCK_NB)
        with self.store.db() as db:
            db.execute('INSERT INTO budget_clock VALUES(1,?)',(self.boot,))
            db.executemany('INSERT INTO requests(token) VALUES(?)',[(token,) for token in self.tokens])
            db.executemany('INSERT INTO leases VALUES(?,?)',zip(self.tokens,recovery.ACQUIRED))
            db.executemany('INSERT OR REPLACE INTO meta VALUES(?,?)',[('epoch',recovery.EPOCH),('connected','0')])
        self.before=recovery.read_state(self.path)
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

    def test_fresh_scan_failure_preserves_original_diagnostic_and_head(self):
        output=io.StringIO(); head='a'*40
        results=[subprocess.CompletedProcess([],0,b'',b''),
            subprocess.CompletedProcess([],0,(head+'\n').encode(),b''),
            subprocess.CompletedProcess([],0,b'{"as_of_utc":"2026-10-12T16:17:11Z"}',b''),
            subprocess.CompletedProcess([],1,b'age evidence\n',b'ValueError: BINANCE_SCAN_NOT_FRESH_COMPLETE\n')]
        with patch.object(recovery,'command',side_effect=results) as command,\
             contextlib.redirect_stdout(output),self.assertRaisesRegex(RuntimeError,'BEFORE_MUTATION_OR_PROBE'):
            recovery.fresh_scan(None)
        receipt=json.loads(output.getvalue())
        self.assertEqual((receipt['source_head'],receipt['returncode']),(head,1))
        self.assertIn('BINANCE_SCAN_NOT_FRESH_COMPLETE',receipt['stderr'])
        self.assertIn('age evidence',receipt['stdout'])
        self.assertFalse(command.call_args.kwargs['check'])

    def test_fresh_scan_success_retains_source_binding(self):
        head='b'*40; snapshot={'as_of_utc':'2026-10-12T16:17:11Z'}
        results=[subprocess.CompletedProcess([],0,b'',b''),
            subprocess.CompletedProcess([],0,head.encode(),b''),
            subprocess.CompletedProcess([],0,json.dumps(snapshot).encode(),b''),
            subprocess.CompletedProcess([],0,b'FRESH_SCOPE_OK\n',b'')]
        with patch.object(recovery,'command',side_effect=results),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(recovery.fresh_scan(None),dict(source_head=head,snapshot=snapshot))

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
        self.assertEqual((len(after['requests']),len(after['leases'])),(4,4))
        self.assertIn('ORPHANED',Budget(self.store).status()['halt'])

    def test_invalid_200_does_not_authorize_disposition(self):
        status,receipt=self.probe(lambda *args:(200,b'[]',1))
        self.assertEqual((status,len(self.calls)),(1,1)); self.assertFalse(receipt['success'])
        self.assertEqual(recovery.read_state(self.path)['requests'],self.before['requests'])

    def test_disposition_removes_only_original_rows_and_preserves_budget(self):
        self.assertEqual(self.probe(self.response)[0],0)
        before_disposition=recovery.read_state(self.path); output=io.StringIO()
        with patch.object(Store,'reader',return_value=self.store),\
             patch.object(sys,'stdin',io.StringIO(json.dumps(dict(before=self.before,evidence_sha256="a"*64)))),contextlib.redirect_stdout(output):
            recovery.archive_three()
        after=recovery.read_state(self.path)
        self.assertEqual((after['requests'],after['leases']),([],[]))
        self.assertEqual(after['budget'],before_disposition['budget'])
        self.assertEqual(after['meta'],self.before['meta'])
        self.assertIn('ARCHIVED_UNKNOWN',output.getvalue())
        with self.store.db(readonly=True) as db:
            archived=[dict(x) for x in db.execute('SELECT * FROM manual_http_dispositions ORDER BY token')]
        self.assertEqual([x['token'] for x in archived],self.tokens)
        self.assertTrue(all(x['historical_outcome']=='UNKNOWN' and x['evidence_sha256']=='a'*64 for x in archived))

    def test_disposition_changed_record_is_rolled_back(self):
        with self.store.db() as db:db.execute('UPDATE requests SET denied=1')
        with patch.object(Store,'reader',return_value=self.store),\
             patch.object(sys,'stdin',io.StringIO(json.dumps(dict(before=self.before,evidence_sha256="a"*64)))),self.assertRaisesRegex(RuntimeError,'COMPARE_AND_SWAP'):
            recovery.archive_three()
        after=recovery.read_state(self.path)
        self.assertEqual((len(after['requests']),len(after['leases'])),(3,3))
        self.assertEqual(after['requests'][0]['denied'],1)

    def test_probe_uses_only_the_fourth_slot_without_retiring_the_old_three(self):
        def response(*args):
            value=recovery.read_state(self.path)
            self.assertEqual((len(value['requests']),len(value['leases'])),(4,4))
            self.assertTrue(set(self.tokens)<={x['token'] for x in value['requests']})
            return self.response(*args)
        self.assertEqual(self.probe(response)[0],0)
        self.assertEqual(recovery.read_state(self.path)['requests'],self.before['requests'])

    def test_middle_delete_failure_rolls_back_archive_and_all_three_records(self):
        with self.store.db() as db:
            db.execute("CREATE TRIGGER reject_second BEFORE DELETE ON requests WHEN OLD.token='"+self.tokens[1]+"' BEGIN SELECT RAISE(IGNORE); END")
        with patch.object(Store,'reader',return_value=self.store),\
             patch.object(sys,'stdin',io.StringIO(json.dumps(dict(before=self.before,evidence_sha256='a'*64)))),\
             self.assertRaisesRegex(RuntimeError,'DELETE_COUNT'):
            recovery.archive_three()
        self.assertEqual(recovery.read_state(self.path),self.before)
        with self.store.db(readonly=True) as db:
            self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='manual_http_dispositions'").fetchone())

    def test_repeated_disposition_cannot_archive_or_retire_any_new_record(self):
        data=json.dumps(dict(before=self.before,evidence_sha256='a'*64))
        with patch.object(Store,'reader',return_value=self.store),patch.object(sys,'stdin',io.StringIO(data)),contextlib.redirect_stdout(io.StringIO()):
            recovery.archive_three()
        with patch.object(Store,'reader',return_value=self.store),patch.object(sys,'stdin',io.StringIO(data)),self.assertRaisesRegex(RuntimeError,'COMPARE_AND_SWAP'):
            recovery.archive_three()
        with self.store.db(readonly=True) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM manual_http_dispositions').fetchone()[0],3)


if __name__=='__main__':unittest.main()
