"""Recovery supervision with a simulated clock; no process, service or network IO."""
import contextlib
import io
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock,patch

from scripts import hunter_binance_recover_three_once as recovery


class SupervisionTests(unittest.TestCase):
    def run_case(self,status='READY',*,halt=None,changed_epoch=False,journal_failed=False,write_failed=False):
        clock=[0];samples=[0];output=io.StringIO();saved={};commands=[]
        upgrade=Mock();upgrade.write_new.side_effect=lambda path,data,*args:saved.setdefault(Path(path).as_posix(),json.loads(data))
        if write_failed:upgrade.write_new.side_effect=OSError('disk')
        def sample(*args,**kwargs):
            samples[0]+=1
            value=dict(status=status,epoch=('b' if changed_epoch and samples[0]>10 else 'a')*32,
                archived_unknown=3,halt=halt,cooldown_active=False)
            return subprocess.CompletedProcess([],0,json.dumps(value).encode(),b'')
        def command(args,**kwargs):
            commands.append(args);return subprocess.CompletedProcess(args,0,b'',b'')
        def journal(*args):
            if journal_failed:raise RuntimeError('journal unavailable')
            return dict(at_limit=False,records=[dict(evidence=dict(event='BINANCE_SESSION_METRICS',audit_output_failures=0))])
        with patch.object(recovery.time,'monotonic',side_effect=lambda:clock[0]),\
             patch.object(recovery.time,'sleep',side_effect=lambda seconds:clock.__setitem__(0,clock[0]+seconds)),\
             patch.object(recovery,'service_state',return_value=dict(ActiveState='active',SubState='running',MainPID='9')),\
             patch.object(recovery,'command',side_effect=command),patch.object(recovery,'child',side_effect=sample),\
             patch.object(recovery,'journal_evidence',side_effect=journal),\
             patch.object(Path,'read_bytes',return_value=b'{}'),contextlib.redirect_stdout(output):
            if write_failed:
                with self.assertRaises(OSError):recovery.supervise(upgrade,Path('fixture'),{})
                result=1
            else:result=recovery.supervise(upgrade,Path('fixture'),{})
        return result,commands,saved,clock[0]

    def test_one_start_and_300_seconds_ready_leaves_service_running(self):
        result,commands,saved,elapsed=self.run_case()
        self.assertEqual((result,elapsed),(0,300))
        self.assertEqual(commands,[['systemctl','start',recovery.SERVICE]])
        report=saved['fixture/recovery-report.json']
        self.assertEqual(report['historical_outcome'],'UNKNOWN');self.assertFalse(report['publication_verified'])

    def test_never_ready_stops_at_initialization_deadline_without_restart(self):
        result,commands,saved,elapsed=self.run_case('UNKNOWN')
        self.assertEqual((result,elapsed),(1,900))
        self.assertEqual(commands,[['systemctl','start',recovery.SERVICE],['systemctl','stop',recovery.SERVICE]])
        self.assertEqual(saved['fixture/recovery-report.json']['error_code'],'INITIALIZATION_DEADLINE_STOPPED')

    def test_epoch_change_restarts_the_continuous_ready_window(self):
        result,commands,saved,elapsed=self.run_case(changed_epoch=True)
        self.assertEqual((result,elapsed),(0,450));self.assertEqual(len(commands),1)

    def test_durable_halt_stops_immediately_and_never_claims_success(self):
        result,commands,saved,elapsed=self.run_case(halt='ORPHANED_HTTP_REQUEST_MANUAL_REVIEW')
        self.assertEqual((result,elapsed),(1,0));self.assertEqual(commands[-1][1],'stop')
        self.assertEqual(saved['fixture/recovery-report.json']['status'],'FAILED_STOPPED')

    def test_missing_observation_does_not_pass_acceptance(self):
        result,commands,saved,elapsed=self.run_case(journal_failed=True)
        self.assertEqual(result,1);self.assertEqual(commands[-1][1],'stop')
        self.assertIn('journal_error_type',saved['fixture/recovery-report.json'])

    def test_cannot_keep_running_if_acceptance_report_is_not_durable(self):
        result,commands,_,_=self.run_case(write_failed=True)
        self.assertEqual(result,1);self.assertEqual(commands[-1][1],'stop')

    def test_source_binding_rejects_changed_db_or_an_added_wal_before_sqlite(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as folder:
            root=Path(folder);db=root/'state.sqlite3';db.write_bytes(b'fixture')
            report=dict(status='COMPLETE',errors=[],cache=dict(epoch=recovery.EPOCH,
                source_sha256={'state.sqlite3':hashlib.sha256(db.read_bytes()).hexdigest()}))
            data=json.dumps(report).encode();report_path=root/'report.json';report_path.write_bytes(data)
            with patch.object(recovery,'STATE',root),patch.object(recovery,'INCIDENT_REPORT',report_path),\
                 patch.object(recovery,'REPORT_HASH',hashlib.sha256(data).hexdigest()),\
                 patch.object(recovery.sqlite3,'connect',side_effect=AssertionError('SQLite forbidden')):
                self.assertEqual(recovery.verify_report_source(Mock()),report)
                (root/'state.sqlite3-wal').write_bytes(b'new')
                with self.assertRaisesRegex(RuntimeError,'SOURCE_BYTES_CHANGED'):recovery.verify_report_source(Mock())
                (root/'state.sqlite3-wal').unlink();db.write_bytes(b'changed')
                with self.assertRaisesRegex(RuntimeError,'SOURCE_BYTES_CHANGED'):recovery.verify_report_source(Mock())


if __name__=='__main__':unittest.main()
