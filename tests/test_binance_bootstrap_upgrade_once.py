"""Pinned package, real scratch SQLite/files, simulated services and monotonic time."""
import contextlib
import copy
import datetime as dt
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import Mock,patch

from scripts import hunter_binance_bootstrap_upgrade_once as entry
from research.hunter_binance_stream_store import Store


def modules():
    root=Path(os.environ['BINANCE_BOOTSTRAP_FIXTURES'])
    upgrade=entry.verified_module((root/'upgrade.py').read_bytes(),entry.UPGRADER_HASH,'test_upgrade')
    evidence=entry.verified_module((root/'evidence.py').read_bytes(),entry.WRAPPER_HASH,'test_evidence')
    upgrade.OLD,upgrade.NEW,upgrade.CHANGED=entry.OLD,entry.NEW,entry.CHANGED
    evidence.OLD,evidence.NEW=entry.OLD,entry.NEW
    return upgrade,evidence


def result(value):return subprocess.CompletedProcess([],0,json.dumps(value).encode(),b'')


def fixture_state():
    tokens=[str(i)*32 for i in (1,2,3)]
    before=dict(meta={'epoch':entry.INCIDENT},clock=[dict(id=1,boot='boot')],budget=dict(next_ms=100,last_ms=50),
        requests=[dict(token=t,denied=0) for t in tokens],
        leases=[dict(token=t,acquired_ms=n) for t,n in zip(tokens,entry.ACQUIRED)])
    previous=dict(before=before,intent_sha256='a'*64,guard_sha256='b'*64)
    rows=[dict(incident_epoch=entry.INCIDENT,token=t,acquired_ms=n,denied=0,historical_outcome='UNKNOWN',
        disposition='USER_AUTHORIZED_BLOCKER_RETIRED',evidence_sha256='a'*64,disposed_at_utc='2026-10-12T23:32:00Z')
        for t,n in zip(tokens,entry.ACQUIRED)]
    value=dict(meta=dict(epoch=entry.PREVIOUS_EPOCH,connected='0'),clock=before['clock'],
        budget=dict(id=1,next_ms=1000,last_ms=999,halt=None,cooldown_ms=0),requests=[],leases=[],dispositions=rows)
    return value,previous


class BindingTests(unittest.TestCase):
    def test_default_plan_has_no_io(self):
        with patch.object(entry,'components',side_effect=AssertionError('IO')),contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(entry.main([]),0)
        self.assertFalse(json.loads(out.getvalue())['production_executed'])

    def test_hash_failure_never_executes_source(self):
        with self.assertRaisesRegex(RuntimeError,'COMPONENT_HASH_MISMATCH'):
            entry.verified_module(b'raise AssertionError()',entry.WRAPPER_HASH,'bad')

    def test_pinned_child_source_really_extracts_and_default_returns_zero(self):
        _,evidence=modules();calls=[]
        with patch.object(evidence,'command',side_effect=lambda args,**kw:calls.append(args) or result({})):
            for fn in (entry.live_state,evidence.consistent_backup,evidence.prepare_manifest,evidence.acceptance_sample):
                evidence.child(fn)
                source=calls[-1][-1];self.assertIn('def '+fn.__name__,source)
                compile(source,'fixture','exec')
        # Actual default child body/return code, redirecting only its fixed Path.
        value,_=fixture_state()
        with tempfile.TemporaryDirectory() as folder:
            db=Path(folder)/'state.sqlite3';create_db(db,value)
            original=Path
            with patch('pathlib.Path',side_effect=lambda x: db if x=='/var/lib/hunter-binance-stream/state.sqlite3' else original(x)),\
                 contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(entry.live_state(),0)
            self.assertEqual(json.loads(out.getvalue()),value)

    def test_sqlite_read_preserves_all_state_and_unknown_fields(self):
        value,previous=fixture_state()
        with tempfile.TemporaryDirectory() as folder:
            db=Path(folder)/'state.sqlite3';create_db(db,value)
            actual=entry.live_state(db)
            entry.validate_state(actual,previous,'boot',10000)
            self.assertEqual(actual,value)
            self.assertEqual(entry.live_state(db),value)

    def test_all_changed_preconditions_reject(self):
        value,previous=fixture_state()
        changes=[('owners',lambda x:x['requests'].append(dict(token='x',denied=0))),
            ('leases',lambda x:x['leases'].append(dict(token='x',acquired_ms=0))),
            ('epoch',lambda x:x['meta'].update(epoch='f'*32)),('connected',lambda x:x['meta'].update(connected='1')),
            ('halt',lambda x:x['budget'].update(halt='403')),('cooldown',lambda x:x['budget'].update(cooldown_ms=10001)),
            ('budget',lambda x:x['budget'].update(last_ms=0)),('boot',lambda x:x['clock'][0].update(boot='new')),
            ('unknown',lambda x:x['dispositions'][0].update(historical_outcome='SUCCESS')),
            ('source',lambda x:x['dispositions'][0].update(evidence_sha256='f'*64)),
            ('token',lambda x:x['dispositions'][0].update(token='f'*32)),
            ('acquisition',lambda x:x['dispositions'][0].update(acquired_ms=0))]
        for name,mutate in changes:
            with self.subTest(name=name),self.assertRaises(RuntimeError):
                changed=copy.deepcopy(value);mutate(changed);entry.validate_state(changed,previous,'boot',10000)
        changed=copy.deepcopy(value);changed['clock'][0]['boot']='new'
        with self.assertRaisesRegex(RuntimeError,'CLOCK'):entry.validate_state(changed,previous,'new',10000)

    def test_scope_must_be_natural_fresh_without_timestamp_rewrite(self):
        for age,ok in [(0,True),(299,True),(301,False),(-60,False)]:
            source={'snapshot':{'as_of_utc':(dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=age)).isoformat()}}
            original=copy.deepcopy(source)
            if ok:entry.scope_fresh(source)
            else:
                with self.assertRaises(RuntimeError):entry.scope_fresh(source)
            self.assertEqual(source,original)

    def test_missing_supervisor_property_or_wrong_hook_rejected(self):
        _,evidence=modules();inv='c'*32
        props=dict(MainPID=str(os.getpid()),Type='exec',Restart='no',Transient='yes',InvocationID=inv,
            RuntimeMaxUSec='30min',TimeoutStopUSec='4min',KillMode='control-group',StandardInput='null',
            StandardOutput='journal',StandardError='journal',
            ExecStopPost='{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 -I -B '+str(Path(entry.__file__).resolve())+' --cleanup ; ignore_errors=no ; }')
        def response(value):return subprocess.CompletedProcess([],0,'\n'.join(k+'='+v for k,v in value.items()).encode(),b'')
        with patch.dict(os.environ,INVOCATION_ID=inv),patch.object(evidence,'command',return_value=response(props)):
            self.assertEqual(entry.supervisor_entry(evidence),inv)
        for key in props:
            with self.subTest(key=key),patch.dict(os.environ,INVOCATION_ID=inv),\
                 patch.object(evidence,'command',return_value=response(dict(props,**{key:'wrong'}))),self.assertRaises(RuntimeError):
                entry.supervisor_entry(evidence)


def create_db(path,value):
    store=Store(path)
    with store.db() as db:
        db.executemany('INSERT INTO meta VALUES(?,?)',value['meta'].items())
        db.execute('INSERT INTO budget_clock VALUES(1,?)',(value['clock'][0]['boot'],))
        db.execute('UPDATE budget SET next_ms=1000,last_ms=999')
        db.execute('CREATE TABLE manual_http_dispositions (incident_epoch,token,acquired_ms,denied,historical_outcome,disposition,evidence_sha256,disposed_at_utc)')
        for row in value['dispositions']:db.execute('INSERT INTO manual_http_dispositions VALUES(?,?,?,?,?,?,?,?)',tuple(row.values()))


class SupervisionTests(unittest.TestCase):
    def run_case(self,*,first_ready=0,forever_unknown=False,proof=True,gap=0,fail_journal=False,
                 final_change=False,history_change=False,slow_journal=False,persist_failure=None,start_timeout=False,
                 stop_timeout=False,quiet_failure=False,denial=None,after_completion_late=False):
        upgrade,evidence=modules();clock=[0.0];commands=[];sample_reads=[0];journal_reads=[0]
        value,_=fixture_state();prepared=dict(snapshot_sha256='f'*64,roster_hash='e'*64,topics=['btcusdt@kline_5m'])
        scratch=tempfile.TemporaryDirectory();self.addCleanup(scratch.cleanup);work=Path(scratch.name)
        (work/'state-before.json').write_text(json.dumps(value))
        def cmd(args,**kwargs):
            commands.append(args)
            if args[:2]==['systemctl','start']:
                clock[0]=1
                if start_timeout:raise subprocess.TimeoutExpired(args,60)
            if args[:2]==['systemctl','stop'] and stop_timeout:raise subprocess.TimeoutExpired(args,60)
            return result({})
        def child(fn,data=None,**kwargs):
            if fn is entry.live_state:
                changed=copy.deepcopy(value)
                if history_change:changed['dispositions'][0]['disposed_at_utc']='changed'
                return result(changed)
            self.assertIs(fn,evidence.acceptance_sample);sample_reads[0]+=1
            late=final_change and clock[0]>=first_ready+301 and sample_reads[0]>1 and journal_reads[0]>0
            return result(dict(status='UNKNOWN' if forever_unknown or clock[0]-1<first_ready or late else 'READY',
                epoch=('b' if late else 'a')*32,owners={'requests':0,'leases':0},archived_unknown=3,
                halt=denial,cooldown_active=False,shadow_only=True,real_trading_enabled=False,capital_authority='NONE_SHADOW_ONLY'))
        def journal(*args,**kwargs):
            journal_reads[0]+=1
            if slow_journal:clock[0]+=1400
            observed=10**12+int(clock[0]*1000)
            audit=dict(event='BINANCE_CACHE_AUDIT',epoch='a'*32,status='READY',observed_at_ms=observed,
                ready_window_seconds=max(0,int(clock[0]-1-max(first_ready,gap))),source_head=entry.NEW,
                manifest_sha256='f'*64,roster_hash='e'*64,shadow_only=True,real_trading_enabled=False,capital_authority='NONE_SHADOW_ONLY')
            records=[dict(evidence=dict(event='BINANCE_SESSION_METRICS',epoch='a'*32,audit_output_failures=0)),
                     dict(evidence=audit,journal_us=str(observed*1000))]
            if proof:records.append(dict(evidence=dict(event='BINANCE_INITIALIZATION_READY',epoch='a'*32)))
            return dict(records=records,complete=not fail_journal,at_limit=False)
        actual_save=evidence.save
        def save(up,where,name,obj):
            if name==persist_failure:raise OSError('fixture disk')
            answer=actual_save(up,where,name,obj)
            if name=='completion.json' and after_completion_late:clock[0]+=1400
            return answer
        with contextlib.ExitStack() as stack:
            for replacement in [patch.object(entry.time,'monotonic',side_effect=lambda:clock[0]),
                patch.object(entry.time,'time_ns',side_effect=lambda:(10**12+int(clock[0]*1000))*1000000),
                patch.object(entry.time,'sleep',side_effect=lambda seconds:clock.__setitem__(0,clock[0]+seconds)),
                patch.object(entry,'service',return_value=dict(ActiveState='active',SubState='running',MainPID='7',InvocationID='c'*32,ExecMainStartTimestampMonotonic='1000000')),
                patch.object(entry,'private_json',side_effect=lambda up,path:json.loads(path.read_bytes())),
                patch.object(evidence,'command',side_effect=cmd),patch.object(evidence,'child',side_effect=child),
                patch.object(evidence,'journal_evidence',side_effect=journal),patch.object(evidence,'save',side_effect=save),
                patch.object(upgrade,'quiet',side_effect=RuntimeError('ALIVE') if quiet_failure else None),
                contextlib.redirect_stdout(io.StringIO())]:stack.enter_context(replacement)
            code=entry.supervise(upgrade,evidence,work,prepared,'d'*32)
        reports={p.name:json.loads(p.read_bytes()) for p in work.glob('*.json')}
        return code,clock[0],commands,reports

    def test_late_899_initialization_accepts_after_900_observation(self):
        code,elapsed,commands,reports=self.run_case(first_ready=899)
        self.assertEqual(code,0);self.assertGreater(elapsed,1200);self.assertLess(elapsed,1291)
        self.assertEqual(len(commands),1);self.assertTrue(reports['completion.json']['accepted'])

    def test_ready_at_start_still_requires_runtime_300(self):
        code,elapsed,commands,_=self.run_case()
        self.assertEqual((code,elapsed,len(commands)),(0,301,1))

    def test_nonready_between_samples_uses_runtime_continuity(self):
        code,elapsed,_,_=self.run_case(gap=290)
        self.assertEqual(code,0);self.assertGreaterEqual(elapsed,591)

    def test_permanent_unknown_never_restarts(self):
        code,elapsed,commands,_=self.run_case(forever_unknown=True)
        self.assertEqual((code,elapsed),(1,991));self.assertEqual([x[1] for x in commands],['start','stop'])

    def test_missing_initialization_proof_cannot_accept(self):
        code,_,commands,_=self.run_case(proof=False)
        self.assertEqual(code,1);self.assertEqual([x[1] for x in commands],['start','stop'])

    def test_final_unknown_or_epoch_change_stops(self):
        code,_,commands,reports=self.run_case(final_change=True)
        self.assertEqual(code,1);self.assertEqual(commands[-1][1],'stop')
        self.assertFalse(reports['failed-report.json']['accepted'])

    def test_historical_row_change_stops(self):
        code,_,_,reports=self.run_case(history_change=True)
        self.assertEqual(code,1);self.assertEqual(reports['failed-report.json']['error_code'],'HISTORICAL_UNKNOWN_ROWS_CHANGED')

    def test_missing_journal_pages_stops(self):
        code,_,_,reports=self.run_case(fail_journal=True)
        self.assertEqual(code,1);self.assertFalse(reports['failed-report.json']['journal']['complete'])

    def test_slow_journal_cannot_extend_deadline(self):
        code,_,_,reports=self.run_case(slow_journal=True)
        self.assertEqual(code,1);self.assertEqual(reports['failed-report.json']['error_code'],'ACCEPTANCE_WINDOW_EXPIRED')

    def test_report_or_completion_failure_stops(self):
        for name in ('acceptance-report.json','completion.json'):
            with self.subTest(name=name):
                code,_,commands,reports=self.run_case(persist_failure=name)
                self.assertEqual(code,1);self.assertEqual(commands[-1][1],'stop')
                self.assertTrue(reports['failed-report.json']['stopped_verified'])

    def test_ambiguous_start_stops_without_retry(self):
        code,_,commands,_=self.run_case(start_timeout=True)
        self.assertEqual(code,1);self.assertEqual([x[1] for x in commands],['start','stop'])

    def test_denial_and_stop_timeout_still_independently_verify(self):
        code,_,_,reports=self.run_case(denial='HTTP_403',stop_timeout=True)
        self.assertEqual(code,1);self.assertTrue(reports['failed-report.json']['stopped_verified'])

    def test_failed_stop_verification_never_claims_stopped(self):
        code,_,_,reports=self.run_case(denial='HTTP_429',stop_timeout=True,quiet_failure=True)
        self.assertEqual(code,1);self.assertFalse(reports['failed-report.json']['stopped_verified'])
        self.assertEqual(reports['failed-report.json']['status'],'STOP_UNCONFIRMED_MANUAL_REVIEW')

    def test_late_after_candidate_persistence_has_authoritative_failure_receipt(self):
        code,_,commands,reports=self.run_case(after_completion_late=True)
        self.assertEqual(code,1);self.assertEqual(commands[-1][1],'stop')
        self.assertTrue(reports['completion.json']['accepted'])
        self.assertFalse(reports['failure-completion.json']['accepted'])
        self.assertNotIn('supervisor-final.json',reports)


class ExitTests(unittest.TestCase):
    def test_active_supervisor_blocks_rollback_before_code_restore(self):
        upgrade,evidence=Mock(),Mock();upgrade.maintenance.return_value=contextlib.nullcontext()
        work=Path('/fixture/upgrade-'+'a'*32)
        evidence.command.return_value=subprocess.CompletedProcess([],0,b'ActiveState=active\nMainPID=12\n',b'')
        with patch.object(entry,'evidence_directory',return_value=work),\
             patch.object(entry,'private_json',return_value=dict(evidence=str(work),new=entry.NEW)),\
             self.assertRaisesRegex(RuntimeError,'SUPERVISOR_STILL_ACTIVE'):
            entry.rollback(upgrade,evidence,work)
        upgrade.restore.assert_not_called();upgrade.releases.assert_not_called()

    def test_component_failure_still_calls_independent_stop(self):
        with patch.object(entry,'root_entry'),patch.object(entry,'components',side_effect=OSError('git')),\
             patch.object(entry,'emergency_stop',return_value=1) as stop:
            self.assertEqual(entry.main(['--cleanup']),1)
        stop.assert_called_once()

    def test_corrupt_guard_and_intent_cannot_bypass_cleanup_fallback(self):
        for failure in (OSError('guard'),ValueError('intent')):
            with patch.object(entry,'root_entry'),patch.object(entry,'components',return_value=modules()),\
                 patch.object(entry,'cleanup',side_effect=failure),patch.object(entry,'emergency_stop',return_value=1) as stop:
                self.assertEqual(entry.main(['--cleanup']),1)
            stop.assert_called_once()


class ReentryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.prefix=self.root/'prefix';self.prefix.mkdir()
        self.state=self.root/'state';self.state.mkdir();self.db=self.state/'state.sqlite3'
        self.value,self.previous=fixture_state();create_db(self.db,self.value)
        self.oldguard=self.prefix/('recovery-once-'+entry.INCIDENT+'.json');self.oldguard.write_bytes(b'consumed old guard')
        self.previous['guard_sha256']=entry.digest(self.oldguard.read_bytes())
        actual,self.evidence=modules();self.upgrade=Mock();self.events=[];self.fail_sync=False;self.fail_read=False;self.race=False
        self.upgrade.OLD,self.upgrade.NEW=entry.OLD,entry.NEW
        self.upgrade.write_new.side_effect=actual.write_new
        self.upgrade.sync_dir.side_effect=self.sync
        self.upgrade.releases.return_value=({'old':(b'old',0o444)},{'new':(b'new',0o444)})
        self.upgrade.maintenance.side_effect=self.maintenance
        self.upgrade.state_evidence.side_effect=self.backup
        self.upgrade.tree_manifest.return_value={}
        self.upgrade.code_paths.side_effect=actual.code_paths
        # Windows sandbox denies this SQLite backup rename; the real Linux
        # rename and every interrupted code-swap boundary run in the CI fixture.
        self.upgrade.move.side_effect=lambda a,b:b.write_bytes(a.read_bytes()) if os.name=='nt' else a.rename(b)
        self.upgrade.stage.side_effect=lambda *args:self.events.append('stage')
        self.upgrade.switch.side_effect=lambda *args:self.events.append('switch')
        self.real_read_text=Path.read_text
        self.source=dict(source_head='f'*40,snapshot={'as_of_utc':dt.datetime.now(dt.timezone.utc).isoformat()})
        self.stack=contextlib.ExitStack();self.addCleanup(self.stack.close)
        for replacement in [patch.object(entry,'PREFIX',self.prefix),patch.object(entry,'STATE',self.state),
            patch.object(entry,'effective_unit'),patch.object(entry,'previous_evidence',return_value=self.previous),
            patch.object(entry,'fresh_scope',return_value=self.source),
            patch.object(entry,'supervise',side_effect=lambda *args:self.events.append('supervise') or 0),
            patch.object(self.evidence,'child',side_effect=self.child),
            patch.object(Path,'read_text',lambda p,*a,**k:'boot' if p.as_posix()=='/proc/sys/kernel/random/boot_id' else self.real_read_text(p,*a,**k))]:
            self.stack.enter_context(replacement)

    def sync(self,path):
        if self.fail_sync and path==self.prefix:raise OSError('guard sync')

    @contextlib.contextmanager
    def maintenance(self):
        if self.race:entry.marker().write_bytes(b'another attempt')
        yield

    def backup(self,work):
        self.assertTrue(entry.marker().exists());self.events.append('backup')
        target=work/'state-evidence';target.mkdir();(target/'state.sqlite3').write_bytes(self.db.read_bytes())
        return {'state.sqlite3':entry.digest(self.db.read_bytes())}

    def child(self,fn,data=None,**kwargs):
        self.assertTrue(entry.marker().exists());self.events.append(fn.__name__)
        if fn is entry.live_state:
            if self.fail_read:raise OSError('live read')
            return result(entry.live_state(self.db))
        if fn is self.evidence.consistent_backup:return result(fn(self.db))
        if fn is self.evidence.prepare_manifest:return result({'receipt':{'topics':['btcusdt@kline_5m']}})
        self.fail('probe, disposition or unexpected child: '+fn.__name__)

    def invoke(self):return entry.apply(self.upgrade,self.evidence,'e'*32)

    def test_success_preserves_guard_and_unknown_and_blocks_reentry(self):
        self.assertEqual(self.invoke(),0);events=self.events[:]
        with self.assertRaisesRegex(RuntimeError,'ALREADY_CONSUMED'):self.invoke()
        self.assertEqual(self.events,events);self.assertEqual(entry.live_state(self.db),self.value)
        self.assertEqual(self.oldguard.read_bytes(),b'consumed old guard')
        self.assertEqual(self.events.count('switch'),1);self.assertEqual(self.events.count('supervise'),1)

    def test_guard_directory_fsync_failure_blocks_live_state_and_reentry(self):
        self.fail_sync=True
        with self.assertRaises(OSError):self.invoke()
        self.assertTrue(entry.marker().exists());self.assertEqual(self.events,[])
        with self.assertRaisesRegex(RuntimeError,'ALREADY_CONSUMED'):self.invoke()

    def test_live_read_failure_preserves_raw_backup_and_consumes_attempt(self):
        self.fail_read=True
        with self.assertRaises(OSError):self.invoke()
        self.assertEqual(self.events,['backup','live_state'])
        self.assertEqual(len(list(self.prefix.glob('upgrade-*/state-evidence/state.sqlite3'))),1)
        with self.assertRaisesRegex(RuntimeError,'ALREADY_CONSUMED'):self.invoke()

    def test_concurrent_guard_wins_before_any_live_access(self):
        self.race=True
        with self.assertRaisesRegex(RuntimeError,'ALREADY_CONSUMED'):self.invoke()
        self.assertEqual(self.events,[]);self.assertEqual(entry.marker().read_bytes(),b'another attempt')

    def test_nonzero_owner_after_guard_cannot_switch_or_start(self):
        with sqlite3.connect(self.db) as db:db.execute('INSERT INTO requests VALUES(?,0)',('f'*32,))
        with self.assertRaisesRegex(RuntimeError,'OWNERS_NOT_ZERO'):self.invoke()
        self.assertEqual(self.events,['backup','live_state'])

    def test_stale_natural_scan_fails_before_guard(self):
        self.source['snapshot']['as_of_utc']=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=301)).isoformat()
        with self.assertRaisesRegex(RuntimeError,'NATURAL_SCAN'):self.invoke()
        self.assertFalse(entry.marker().exists());self.assertEqual(self.events,[])


if __name__=='__main__':unittest.main()
