import contextlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('upgrade_entry', ROOT/'scripts/hunter_binance_health_upgrade_once.py')
entry = importlib.util.module_from_spec(spec); spec.loader.exec_module(entry)


def value(stamp=1000000, epoch='epoch', **changes):
    return dict(dict(observed_at=stamp, epoch=epoch, roster_hash='r', manifest_sha256='m',
        source_head=entry.NEW, health_verified=True, service_status='DEGRADED', data_status='UNKNOWN'), **changes)


class WindowTests(unittest.TestCase):
    def test_healthy_degraded_does_not_require_data_ready(self):
        window=entry.Window()
        for n in range(30): self.assertFalse(window.observe(value(1000000+n*10000), n*10))
        self.assertTrue(window.observe(value(1300000), 300))

    def test_replayed_sample_cannot_accumulate_wall_time(self):
        window=entry.Window()
        for n in range(100): self.assertFalse(window.observe(value(), n*10))

    def test_epoch_change_resets_window(self):
        window=entry.Window()
        for n in range(30): window.observe(value(1000000+n*10000), n*10)
        self.assertFalse(window.observe(value(1300000, epoch='other'),300))

    def test_unverified_or_unhealthy_resets_window(self):
        for change in ({'health_verified':False},{'service_status':'UNKNOWN'}):
            window=entry.Window()
            for n in range(30): window.observe(value(1000000+n*10000), n*10)
            self.assertFalse(window.observe(value(1300000, **change),300))
            self.assertFalse(window.observe(value(1310000),310))

    def test_time_jump_and_backwards_clock_reset(self):
        for stamp,steady in ((1040000,40),(1000001,-1),(999999,10)):
            window=entry.Window();window.observe(value(),0)
            self.assertFalse(window.observe(value(stamp),steady))
            self.assertEqual(window.first, stamp)


class GateTests(unittest.TestCase):
    def test_plan_does_not_touch_host_or_bundle(self):
        with patch.object(entry,'bundle',side_effect=AssertionError), patch.object(entry,'command',side_effect=AssertionError):
            with contextlib.redirect_stdout(__import__('io').StringIO()) as stream: self.assertEqual(entry.main([]),0)
        report=json.loads(stream.getvalue());self.assertFalse(report['consumer_switch']);self.assertFalse(report['publication_verified'])

    def test_consumed_guard_stops_before_host_checks(self):
        upgrade=Mock()
        with patch.object(entry.os.path,'lexists',return_value=True):
            with self.assertRaisesRegex(RuntimeError,'ATTEMPT_ALREADY_CONSUMED'):entry.preflight(upgrade,{})
        upgrade.check_host.assert_not_called()

    def test_existing_dropin_stops_before_host_checks(self):
        upgrade=Mock()
        with patch.object(entry.os.path,'lexists',side_effect=[False,True]):
            with self.assertRaisesRegex(RuntimeError,'EXISTING_DROPINS'):entry.preflight(upgrade,{})
        upgrade.check_host.assert_not_called()

    def test_changed_guard_is_not_written_or_reset(self):
        with patch.object(entry,'regular',return_value=b'changed'), patch.object(entry,'save',side_effect=AssertionError):
            with self.assertRaisesRegex(RuntimeError,'PREVIOUS_GUARD_CHANGED'):entry.guard_hashes()

    def test_preflight_failure_cleanup_never_stops_service(self):
        with patch.object(entry.os.path,'lexists',return_value=False), patch.object(entry,'stop_owned',side_effect=AssertionError), patch.object(entry,'save',side_effect=AssertionError):
            with contextlib.redirect_stdout(__import__('io').StringIO()):self.assertEqual(entry.cleanup(),1)

    def test_history_hash_halt_owners_epoch_and_boot_fail_closed(self):
        good={'meta':{'connected':'0','epoch':entry.OLD_EPOCH},'requests':[],'leases':[],
              'clock':[{'id':1,'boot':entry.BOOT}],'budget':{'halt':None,'cooldown_ms':0,'last_ms':0},'dispositions':[]}
        with patch.object(entry,'HISTORY_HASH',entry.digest(entry.canonical([]))):
            entry.validate_state(good)
            import copy
            cases=[]
            for key,val in [('requests',[{}]),('leases',[{}]),('clock',[]),('dispositions',[{}])]:
                bad=copy.deepcopy(good);bad[key]=val;cases.append(bad)
            for key,val in [('halt','403'),('cooldown_ms',10**30),('last_ms',10**30)]:
                bad=copy.deepcopy(good);bad['budget'][key]=val;cases.append(bad)
            bad=copy.deepcopy(good);bad['meta']['epoch']='changed';cases.append(bad)
            for bad in cases:
                with self.assertRaises(RuntimeError):entry.validate_state(bad)

    def test_parent_sample_rejects_stale_and_future_health_proofs(self):
        now=2000000
        prepared={'snapshot_sha256':'m','roster_hash':'r','topics':['topic']}
        good=value(now, health_verified_at=now, queue_progress_at=now, oldest_active_ms=0,
            shards=[{'observed_at':now}], topics=1, historical_unknown_sha256=entry.HISTORY_HASH,
            publication_verified=False, shadow_only=True, real_trading_enabled=False, capital_authority='NONE_SHADOW_ONLY')
        with patch.object(entry.time,'time_ns',return_value=now*1000000):
            self.assertEqual(entry.sample(Mock(child=Mock(return_value=Mock(stdout=json.dumps(good)))),prepared),good)
            for change in ({'observed_at':now-30001},{'health_verified_at':now+1},
                           {'queue_progress_at':now-30001},{'oldest_active_ms':45001},
                           {'shards':[{'observed_at':now-90001}]}):
                bad=dict(good,**change)
                with self.assertRaisesRegex(RuntimeError,'HEALTH_EXPIRED_AT_PARENT_RECEIVE'):
                    entry.sample(Mock(child=Mock(return_value=Mock(stdout=json.dumps(bad)))),prepared)

    def test_unqualified_health_sample_cannot_be_promoted_by_parent(self):
        unknown=dict(health_verified=False, historical_unknown_sha256=entry.HISTORY_HASH,
            publication_verified=False, shadow_only=True, real_trading_enabled=False, capital_authority='NONE_SHADOW_ONLY')
        result=entry.sample(Mock(child=Mock(return_value=Mock(stdout=json.dumps(unknown)))),{})
        self.assertFalse(result['health_verified'])

    def test_dropin_bound_to_invocation_and_has_no_selector(self):
        raw=entry.dropin('a'*32)
        self.assertIn(b'--health-supervision',raw);self.assertIn(b'TimeoutStopSec=90',raw)
        self.assertNotIn(b'KLINE_SOURCE',raw);self.assertNotIn(b'ReadWritePaths',raw)
        with self.assertRaises(RuntimeError):entry.dropin('../other')

    def test_replacement_invocation_is_never_stopped(self):
        with patch.object(entry,'owned_service',side_effect=RuntimeError('COLLECTOR_OWNERSHIP_CHANGED')), patch.object(entry,'command',side_effect=AssertionError):
            result=entry.stop_owned(Path('/unused'))
        self.assertFalse(result['stop_attempted']);self.assertFalse(result['stopped_verified'])

    def test_ambiguous_stop_still_checks_quiescence(self):
        stopped={'ActiveState':'inactive','MainPID':'0','Job':''}
        with patch.object(entry,'owned_service',return_value={'ActiveState':'active','MainPID':'1'}), patch.object(entry,'command',side_effect=subprocess.TimeoutExpired('stop',110)), patch.object(entry,'show',return_value=stopped), patch.object(entry,'stopped_processes') as verify:
            result=entry.stop_owned(Path('/unused'))
        verify.assert_called_once_with(stopped);self.assertTrue(result['stopped_verified']);self.assertTrue(result['stop_timed_out'])

    def test_cleanup_retains_verified_running_invocation_before_binding_persist(self):
        running={'ActiveState':'active','MainPID':'12','InvocationID':'a'*32}
        quiet={'ActiveState':'inactive','MainPID':'0','Job':''}
        with patch.object(entry,'owned_service',return_value=running), patch.object(entry,'command',return_value=Mock(returncode=0)), patch.object(entry,'show',return_value=quiet),patch.object(entry,'stopped_processes'):
            result=entry.stop_owned(Path('/unused'))
        self.assertTrue(result['stopped_verified'])
        self.assertEqual(result['owned_collector_invocation'],'a'*32)

    def test_no_stop_when_already_stopped(self):
        state={'ActiveState':'failed','MainPID':'0','Job':''}
        with patch.object(entry,'owned_service',return_value=state), patch.object(entry,'command',side_effect=AssertionError), patch.object(entry,'show',return_value=state),patch.object(entry,'stopped_processes'):
            result=entry.stop_owned(Path('/unused'))
        self.assertFalse(result['stop_attempted']);self.assertTrue(result['stopped_verified'])

    def test_queued_start_and_missing_job_are_never_called_stopped(self):
        for job in (None,'123 /org/freedesktop/systemd1/job/123'):
            self.assertFalse(entry.stopped({'ActiveState':'inactive','MainPID':'0','Job':job}))

    def test_owned_pending_start_is_stopped_even_without_process_timestamp(self):
        invocation='a'*32
        intent={'supervisor_invocation':invocation,'new':entry.NEW,'started_monotonic_us':100}
        pending={'ActiveState':'inactive','MainPID':'0','Job':'123 /org/freedesktop/systemd1/job/123','ExecMainStartTimestampMonotonic':'0'}
        quiet={'ActiveState':'inactive','MainPID':'0','Job':''}
        def regular(path,*args):
            return (entry.NEW+'\n').encode() if path.name=='release-head' else entry.dropin(invocation)
        with patch.dict(os.environ,{'INVOCATION_ID':invocation}),patch.object(entry,'private_json',return_value=intent),patch.object(entry,'regular',side_effect=regular),patch.object(Path,'exists',return_value=False),patch.object(entry,'show',side_effect=[pending,quiet]),patch.object(entry,'command',return_value=Mock(returncode=0)) as stop,patch.object(entry,'stopped_processes'):
            result=entry.stop_owned(Path('/unused'))
        self.assertTrue(result['stop_attempted']);self.assertTrue(result['stopped_verified'])
        self.assertEqual(stop.call_args.args[0],['systemctl','stop',entry.SERVICE])


@unittest.skipUnless(sys.platform=='linux','Real POSIX filesystem checks require Linux')
class FilesystemTests(unittest.TestCase):
    def test_guard_create_exclusive_and_preserves_existing_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'guard.json';entry.save(path,{'consumed':True})
            before=path.read_bytes()
            with self.assertRaises(FileExistsError):entry.save(path,{'consumed':False})
            self.assertEqual(path.read_bytes(),before)

    def test_atomic_guard_rejects_symlink_without_touching_target(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);target=root/'target';target.write_bytes(b'keep');link=root/'guard';link.symlink_to(target)
            with self.assertRaises(FileExistsError):entry.save(link,b'change')
            self.assertEqual(target.read_bytes(),b'keep')


if __name__=='__main__':unittest.main()
