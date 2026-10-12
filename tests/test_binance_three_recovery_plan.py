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
    def run_case(self,status='READY',*,halt=None,changed_epoch=False,journal_failed=False,write_failed=False,
                 stop_failed=False,quiet_failed=False,first_ready_at=0,slow_sample=False,slow_journal=False,
                 runtime_gap_at=0):
        clock=[0];samples=[0];output=io.StringIO();saved={};commands=[]
        upgrade=Mock();upgrade.write_new.side_effect=lambda path,data,*args:saved.setdefault(Path(path).as_posix(),json.loads(data))
        if write_failed:upgrade.write_new.side_effect=OSError('disk')
        if quiet_failed:upgrade.quiet.side_effect=RuntimeError('still running')
        def sample(*args,**kwargs):
            samples[0]+=1
            if slow_sample and clock[0]>=1185:
                self.assertLessEqual(kwargs['timeout'],15);clock[0]+=49
            value=dict(status=status if clock[0]>=first_ready_at else 'UNKNOWN',epoch=('b' if changed_epoch and samples[0]>10 else 'a')*32,
                archived_unknown=3,halt=halt,cooldown_active=False)
            return subprocess.CompletedProcess([],0,json.dumps(value).encode(),b'')
        def command(args,**kwargs):
            commands.append(args)
            if stop_failed and args[1]=='stop':raise subprocess.TimeoutExpired(args,60)
            return subprocess.CompletedProcess(args,0,b'',b'')
        prepared=dict(snapshot_sha256='f'*64,roster_hash='e'*64)
        def journal(*args,**kwargs):
            if journal_failed:raise RuntimeError('journal unavailable')
            if slow_journal:clock[0]+=901
            observed=10**12+int(clock[0]*1000)
            origin=150 if changed_epoch and samples[0]>10 else max(first_ready_at,runtime_gap_at)
            audit=dict(event='BINANCE_CACHE_AUDIT',observed_at_ms=observed,
                ready_window_seconds=max(0,int(clock[0]-origin)),source_head=recovery.NEW,
                manifest_sha256=prepared['snapshot_sha256'],roster_hash=prepared['roster_hash'],
                shadow_only=True,real_trading_enabled=False,capital_authority='NONE_SHADOW_ONLY',
                epoch=('b' if changed_epoch and samples[0]>10 else 'a')*32,status='READY')
            return dict(at_limit=False,records=[dict(evidence=dict(event='BINANCE_SESSION_METRICS',audit_output_failures=0)),
                dict(journal_us=str(observed*1000),evidence=audit)])
        with patch.object(recovery.time,'monotonic',side_effect=lambda:clock[0]),\
             patch.object(recovery.time,'time_ns',side_effect=lambda:(10**12+int(clock[0]*1000))*1000000),\
             patch.object(recovery.time,'sleep',side_effect=lambda seconds:clock.__setitem__(0,clock[0]+seconds)),\
             patch.object(recovery,'service_state',return_value=dict(ActiveState='active',SubState='running',MainPID='9')),\
             patch.object(recovery,'command',side_effect=command),patch.object(recovery,'child',side_effect=sample),\
             patch.object(recovery,'journal_evidence',side_effect=journal),\
             patch.object(recovery,'write_report',side_effect=lambda path,value:hashlib.sha256(json.dumps(value).encode()).hexdigest()),\
             contextlib.redirect_stdout(output):
            result=recovery.supervise(upgrade,Path('fixture'),prepared)
        saved['_quiet_calls']=upgrade.quiet.call_count
        saved['_console']=output.getvalue()
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
        result,commands,saved,_=self.run_case(write_failed=True,stop_failed=True)
        self.assertEqual(result,1);self.assertEqual(commands[-1][1],'stop')
        self.assertEqual(saved['_quiet_calls'],1)
        self.assertIn('REPORT_PERSISTENCE_FAILED_STOPPED',saved['_console'])
        self.assertIn('"report_persisted": false',saved['_console'])

    def test_stop_timeout_still_verifies_quiet_and_saves_report(self):
        result,_,saved,_=self.run_case(halt='halt',stop_failed=True)
        report=saved['fixture/recovery-report.json']
        self.assertEqual(result,1);self.assertEqual(saved['_quiet_calls'],1)
        self.assertTrue(report['stopped_verified']);self.assertEqual(report['stop_error_type'],'TimeoutExpired')

    def test_stop_timeout_and_live_processes_report_unconfirmed(self):
        result,_,saved,_=self.run_case(halt='halt',stop_failed=True,quiet_failed=True)
        report=saved['fixture/recovery-report.json']
        self.assertEqual(result,1);self.assertEqual(saved['_quiet_calls'],1)
        self.assertFalse(report['stopped_verified']);self.assertEqual(report['status'],'STOP_UNCONFIRMED_MANUAL_REVIEW')

    def test_slow_last_sample_cannot_accept_after_deadline(self):
        result,commands,saved,elapsed=self.run_case(first_ready_at=885,slow_sample=True)
        self.assertEqual((result,elapsed),(1,1234));self.assertEqual(commands[-1][1],'stop')
        self.assertEqual(saved['fixture/recovery-report.json']['error_code'],'ACCEPTANCE_WINDOW_EXPIRED')

    def test_slow_acceptance_journal_cannot_accept_after_deadline(self):
        result,commands,saved,_=self.run_case(slow_journal=True)
        self.assertEqual(result,1);self.assertEqual(commands[-1][1],'stop')
        self.assertEqual(saved['fixture/recovery-report.json']['error_code'],'ACCEPTANCE_WINDOW_EXPIRED')

    def test_nonready_between_samples_requires_a_new_runtime_300_second_window(self):
        result,commands,_,elapsed=self.run_case(runtime_gap_at=290)
        self.assertEqual((result,elapsed),(0,600));self.assertEqual(len(commands),1)

    def test_ready_audit_rejects_stale_time_and_wrong_scope_source_or_safety_flags(self):
        prepared=dict(snapshot_sha256='f'*64,roster_hash='e'*64);sample=dict(epoch='a'*32)
        audit=dict(event='BINANCE_CACHE_AUDIT',observed_at_ms=400000,ready_window_seconds=300,
            source_head=recovery.NEW,manifest_sha256='f'*64,roster_hash='e'*64,epoch='a'*32,
            status='READY',shadow_only=True,real_trading_enabled=False,capital_authority='NONE_SHADOW_ONLY')
        def evidence(value,extra=None):
            return dict(at_limit=False,records=[dict(evidence=dict(event='BINANCE_SESSION_METRICS',audit_output_failures=0)),
                dict(journal_us='400000000',evidence=value)]+([] if extra is None else [dict(journal_us='400000000',evidence=extra)]))
        with patch.object(recovery.time,'time_ns',return_value=400000*1000000):
            self.assertTrue(recovery.continuous_ready(evidence(audit),sample,prepared,0))
            for key,value in [('source_head','b'*40),('manifest_sha256','b'*64),('roster_hash','b'*64),
                              ('shadow_only',False),('real_trading_enabled',True),('capital_authority','LIVE'),
                              ('observed_at_ms',339999),('ready_window_seconds',401)]:
                with self.subTest(key=key),self.assertRaises(RuntimeError):
                    recovery.continuous_ready(evidence(dict(audit,**{key:value})),sample,prepared,0)
            for value in [dict(audit,epoch='b'*32),dict(audit,status='UNKNOWN'),dict(audit,ready_window_seconds=299)]:
                self.assertFalse(recovery.continuous_ready(evidence(value),sample,prepared,0))
            self.assertFalse(recovery.continuous_ready(evidence(audit,dict(event='BINANCE_RECOVERY_WINDOW_RESET')),sample,prepared,0))
            self.assertFalse(recovery.continuous_ready(evidence(audit,dict(audit,status='UNKNOWN')),sample,prepared,0))

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
