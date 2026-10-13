"""Full CLI admission/report flow with real exclusive files and fake external IO."""
import contextlib
import copy
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock,patch

from scripts import hunter_binance_recover_three_once as recovery


class ReentryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]);self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name);self.prefix=root/'prefix';self.state=root/'state';self.reports=root/'reports'
        for path in (self.prefix,self.state,self.reports):path.mkdir()
        self.db=self.state/'state.sqlite3';self.db.write_bytes(b'original incident bytes')
        self.incident=root/'incident.json'
        self.incident.write_text(json.dumps(dict(status='COMPLETE',errors=[],cache=dict(epoch=recovery.EPOCH,
            source_sha256={'state.sqlite3':hashlib.sha256(self.db.read_bytes()).hexdigest()}))))
        self.marker=self.prefix/('recovery-once-'+recovery.EPOCH+'.json')
        self.legacy=self.reports/('hunter-binance-recovery-'+recovery.EPOCH+'.report.json')
        self.legacy.write_bytes(b'old fixed-path failed-preflight evidence')
        self.before=dict(meta=dict(epoch=recovery.EPOCH,connected='0'),clock=[dict(id=1,boot='fixture')],
            budget=dict(halt=None,cooldown_ms=0,next_ms=0,last_ms=0),
            requests=[dict(token=f'{i:032x}',denied=0) for i in range(1,4)],
            leases=[dict(token=f'{i:032x}',acquired_ms=stamp) for i,stamp in enumerate(recovery.ACQUIRED,1)])
        self.current=copy.deepcopy(self.before);self.clock=0;self.events=[];self.output=io.StringIO()
        self.scan_ages=[0];self.fetches=0;self.fail_live_read=False;self.report_error=None
        self.raced_guard=False;self.guard_sync_error=False;self.report_fsync_error=False
        self.prepared=dict(snapshot_sha256='f'*64,roster_hash='e'*64,topics=['BTCUSDT'])
        self.upgrade=Mock(OLD=recovery.OLD,NEW=recovery.NEW)
        self.upgrade.releases.return_value=({},{});self.upgrade.tree_manifest.return_value={}
        self.upgrade.write_new.side_effect=self.write_new
        self.upgrade.sync_dir.side_effect=self.sync_dir
        self.upgrade.maintenance.side_effect=self.maintenance
        self.upgrade.state_evidence.side_effect=self.backup
        self.upgrade.move.side_effect=lambda src,dst:src.rename(dst)
        self.upgrade.code_paths.side_effect=lambda prefix,work:(None,work/'staged',None)
        self.upgrade.stage.side_effect=lambda *args:self.events.append('stage')
        self.upgrade.switch.side_effect=lambda *args:self.events.append('switch')
        self.real_read_text=Path.read_text;self.real_report=recovery.write_report
        self.stack=contextlib.ExitStack();self.addCleanup(self.stack.close)
        replacements=[patch.object(recovery,'PREFIX',self.prefix),patch.object(recovery,'STATE',self.state),
            patch.object(recovery,'REPORT_ROOT',self.reports),patch.object(recovery,'INCIDENT_REPORT',self.incident),
            patch.object(recovery,'REPORT_HASH',hashlib.sha256(self.incident.read_bytes()).hexdigest()),
            patch.object(recovery,'helper',return_value=self.upgrade),patch.object(recovery.sys,'platform','linux'),
            patch.object(recovery.os,'geteuid',return_value=0,create=True),
            patch.object(recovery.time,'monotonic',side_effect=lambda:self.clock),
            patch.object(recovery.time,'monotonic_ns',side_effect=lambda:10**12+int(self.clock*10**9)),
            patch.object(recovery.time,'time_ns',side_effect=lambda:10**18+int(self.clock*10**9)),
            patch.object(recovery.time,'sleep',side_effect=self.sleep),
            patch.object(recovery,'command',side_effect=self.command),patch.object(recovery,'child',side_effect=self.child),
            patch.object(recovery,'service_state',return_value=dict(ActiveState='active',SubState='running',MainPID='7')),
            patch.object(recovery,'journal_evidence',side_effect=self.journal),
            patch.object(recovery,'write_report',side_effect=self.write_report),
            patch.object(Path,'read_text',lambda path,*args,**kwargs:'fixture' if path.as_posix()=='/proc/sys/kernel/random/boot_id'
                         else self.real_read_text(path,*args,**kwargs)),
            patch.object(subprocess,'run',side_effect=AssertionError('REAL_PROCESS_FORBIDDEN')),
            contextlib.redirect_stdout(self.output)]
        for replacement in replacements:self.stack.enter_context(replacement)

    def write_new(self,path,data,*args):
        if path==self.marker:self.events.append('guard')
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'wb') as handle:handle.write(data);handle.flush();os.fsync(handle.fileno())

    def sync_dir(self,path):
        if path==self.prefix and self.guard_sync_error:raise OSError('guard fsync failed')

    @contextlib.contextmanager
    def maintenance(self):
        if self.raced_guard:self.marker.write_text('another invocation owns the attempt')
        yield

    def backup(self,work):
        self.events.append('backup');self.assertTrue(self.marker.exists())
        self.write_new(work/'original-db.raw',self.db.read_bytes())
        return {'state.sqlite3':hashlib.sha256(self.db.read_bytes()).hexdigest()}

    def sleep(self,seconds):self.clock+=seconds

    def result(self,value):return subprocess.CompletedProcess([],0,json.dumps(value).encode(),b'')

    def command(self,args,**kwargs):
        if str(args[0])=='git':
            if 'fetch' in args:self.fetches+=1;return self.result(None)
            if 'rev-parse' in args:return subprocess.CompletedProcess([],0,('a'*40).encode(),b'')
            if 'show' in args:
                age=self.scan_ages[min(self.fetches-1,len(self.scan_ages)-1)]
                scan=dict(as_of_utc=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=age)).isoformat(),
                    generation_id='fixture',binance_complete=True,
                    coins={'BTC':dict(pairs=[dict(venue='binance',base='BTC',pair='BTCUSDT')])})
                return self.result(scan)
        if '-c' in args and args[-1]==recovery.SCAN_CHECK:
            stdout=io.StringIO();stderr=b'';code=0
            with patch.object(recovery.sys,'stdin',io.StringIO(kwargs['data'].decode())),contextlib.redirect_stdout(stdout):
                try:exec(recovery.SCAN_CHECK,{})
                except Exception as error:code=1;stderr=(type(error).__name__+': '+str(error)).encode()
            return subprocess.CompletedProcess([],code,stdout.getvalue().encode(),stderr)
        if args[:1]==['systemctl']:
            self.events.append(args[1]);return self.result(None)
        raise AssertionError('UNEXPECTED_EXTERNAL_COMMAND')

    def child(self,function,data=None,**kwargs):
        self.events.append(function.__name__)
        self.assertTrue(self.marker.exists(),'all live-state access requires the durable guard')
        if function is recovery.read_state:
            if self.fail_live_read:raise OSError('live read failed after guard')
            return self.result(self.current)
        if function is recovery.consistent_backup:
            path=self.state/'recovery-evidence-fixture.sqlite3';path.write_bytes(self.db.read_bytes())
            return self.result(dict(path=str(path),integrity='ok'))
        if function is recovery.probe_once:
            self.assertEqual(len(self.current['requests']),3)
            return self.result(dict(success=True,http_attempts=1,http_status=200,process_reaped=True))
        if function is recovery.prepare_manifest:return self.result(dict(receipt=self.prepared))
        if function is recovery.archive_three:
            self.current['requests']=[];self.current['leases']=[]
            return self.result(dict(status='ARCHIVED_UNKNOWN_AND_RETIRED_ACTIVE_BLOCKERS',records=3,historical_outcome='UNKNOWN'))
        if function is recovery.acceptance_sample:
            return self.result(dict(status='READY',epoch='b'*32,halt=None,cooldown_active=False,archived_unknown=3))
        raise AssertionError('UNEXPECTED_CHILD')

    def journal(self,*args,**kwargs):
        observed=10**12+int(self.clock*1000)
        audit=dict(event='BINANCE_CACHE_AUDIT',observed_at_ms=observed,ready_window_seconds=int(self.clock),
            source_head=recovery.NEW,manifest_sha256='f'*64,roster_hash='e'*64,epoch='b'*32,status='READY',
            shadow_only=True,real_trading_enabled=False,capital_authority='NONE_SHADOW_ONLY')
        return dict(at_limit=False,complete=True,records=[dict(evidence=dict(event='BINANCE_SESSION_METRICS',audit_output_failures=0)),
            dict(journal_us=str(observed*1000),evidence=audit)])

    def write_report(self,path,value):
        if self.report_error:raise self.report_error('fixture report error')
        if self.report_fsync_error:
            with patch.object(recovery.os,'fsync',side_effect=OSError('report fsync failed')):
                return self.real_report(path,value)
        return self.real_report(path,value)

    def invoke(self):
        with patch.object(recovery.sys,'argv',['entry','--run']):return recovery.main()

    def reports_written(self):
        return [json.loads(path.read_text()) for path in self.reports.glob('*.report.json') if path!=self.legacy]

    def test_two_stale_admissions_then_fresh_ready_preserve_all_prior_reports(self):
        self.scan_ages=[3600,3600,0];previous={self.legacy:self.legacy.read_bytes()}
        for _ in range(2):
            self.assertEqual(self.invoke(),1);self.assertFalse(self.marker.exists())
            self.assertFalse(self.events);self.assertEqual(self.current,self.before)
            previous.update({p:p.read_bytes() for p in self.reports.glob('*.report.json')})
        self.assertEqual(self.invoke(),0)
        reports=self.reports_written();self.assertEqual(len(reports),3)
        self.assertEqual(sum(r['status']=='READY_300S_CONSUMER_PUBLICATION_PENDING' for r in reports),1)
        self.assertEqual(len({r['invocation_id'] for r in reports}),3)
        for path,data in previous.items():self.assertEqual(path.read_bytes(),data)
        for event in ('guard','stage','switch','probe_once','archive_three','start'):
            self.assertEqual(self.events.count(event),1,event)
        self.assertNotIn('stop',self.events)
        self.assertLess(self.events.index('guard'),self.events.index('backup'))
        self.assertLess(self.events.index('backup'),self.events.index('read_state'))
        marker=json.loads(self.marker.read_text());self.assertEqual(marker['historical_outcome'],'UNKNOWN')
        self.assertEqual(marker['phase'],'BEFORE_LIVE_STATE_ACCESS')

    def test_completed_attempt_cannot_fetch_probe_or_start_again(self):
        self.assertEqual(self.invoke(),0);events=list(self.events);fetches=self.fetches
        self.assertEqual(self.invoke(),1)
        self.assertEqual(self.events,events);self.assertEqual(self.fetches,fetches)
        self.assertIn('INCIDENT_ALREADY_ATTEMPTED_MANUAL_REVIEW_REQUIRED',self.output.getvalue())

    def test_partial_live_state_attempt_blocks_readmission_before_fetch(self):
        self.fail_live_read=True;self.assertEqual(self.invoke(),1)
        self.assertTrue(self.marker.exists());self.assertNotIn('probe_once',self.events);self.assertNotIn('start',self.events)
        events=list(self.events);fetches=self.fetches;self.fail_live_read=False
        self.assertEqual(self.invoke(),1);self.assertEqual(self.events,events);self.assertEqual(self.fetches,fetches)

    def test_changed_source_blocks_readmission_before_fetch_or_guard(self):
        self.db.write_bytes(b'changed by another process')
        self.assertEqual(self.invoke(),1);self.assertEqual(self.fetches,0);self.assertFalse(self.marker.exists())
        self.assertFalse(self.events);self.assertIn('INCIDENT_SOURCE_BYTES_CHANGED',self.output.getvalue())

    def test_racing_attempt_guard_is_rechecked_under_maintenance(self):
        self.raced_guard=True;self.assertEqual(self.invoke(),1)
        self.assertFalse(self.events);self.assertEqual(self.marker.read_text(),'another invocation owns the attempt')

    def assert_report_failure_stops(self,error):
        self.report_error=error;self.assertEqual(self.invoke(),1)
        self.assertEqual(self.events.count('start'),1);self.assertEqual(self.events.count('stop'),1)
        self.assertIn('REPORT_PERSISTENCE_FAILED_STOPPED',self.output.getvalue())
        fallback=list(self.prefix.glob('upgrade-*/recovery-report-persistence-failure.json'))
        self.assertEqual(len(fallback),1);report=json.loads(fallback[0].read_text())
        self.assertTrue(report['stopped_verified']);self.assertEqual(report['report_error_type'],error.__name__)
        self.assertEqual(self.legacy.read_bytes(),b'old fixed-path failed-preflight evidence')
        self.assertTrue(self.marker.exists())
        events=list(self.events);self.assertEqual(self.invoke(),1);self.assertEqual(self.events,events)

    def test_report_permission_error_stops_and_preserves_evidence(self):self.assert_report_failure_stops(PermissionError)

    def test_report_write_error_stops_and_preserves_evidence(self):self.assert_report_failure_stops(OSError)

    def test_report_fsync_failure_preserves_partial_evidence_and_stops(self):
        self.report_fsync_error=True;self.assertEqual(self.invoke(),1)
        self.assertEqual(self.events.count('start'),1);self.assertEqual(self.events.count('stop'),1)
        self.assertEqual(len(self.reports_written()),1)
        fallback=list(self.prefix.glob('upgrade-*/recovery-report-persistence-failure.json'))
        self.assertEqual(len(fallback),1)
        self.assertTrue(json.loads(fallback[0].read_text())['stopped_verified'])
        final=json.loads(self.output.getvalue().splitlines()[-1])
        self.assertFalse(final['acceptance_report_persisted']);self.assertTrue(final['report_persisted'])
        self.assertEqual(final['report'],str(fallback[0]))
        delivered=Path(final['report']);partial=Path(final['partial_report'])
        self.assertEqual(json.loads(delivered.read_text())['status'],'REPORT_PERSISTENCE_FAILED_STOPPED')
        self.assertEqual(hashlib.sha256(delivered.read_bytes()).hexdigest(),final['report_sha256'])
        self.assertEqual(json.loads(partial.read_text())['status'],'READY_300S_CONSUMER_PUBLICATION_PENDING')
        self.assertNotEqual(delivered,partial)
        self.assertTrue(self.marker.exists())

    def test_failed_preflight_report_permission_error_never_starts(self):
        self.scan_ages=[3600];self.report_error=PermissionError;self.assertEqual(self.invoke(),1)
        self.assertFalse(self.marker.exists());self.assertFalse(self.events)
        self.assertIn('"report_persisted": false',self.output.getvalue())

    def test_guard_fsync_failure_prevents_live_access_and_blocks_retry(self):
        self.guard_sync_error=True;self.assertEqual(self.invoke(),1)
        self.assertEqual(self.events,['guard']);self.assertTrue(self.marker.exists())
        self.assertEqual(self.invoke(),1);self.assertEqual(self.events,['guard']);self.assertEqual(self.fetches,1)

    def test_report_exclusive_write_never_overwrites_an_existing_file(self):
        before=self.legacy.read_bytes()
        with self.assertRaises(FileExistsError):self.real_report(self.legacy,dict(status='new'))
        self.assertEqual(self.legacy.read_bytes(),before)


if __name__=='__main__':unittest.main()
