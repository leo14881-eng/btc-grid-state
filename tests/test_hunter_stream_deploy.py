import copy
import contextlib
import datetime as dt
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research import hunter_bybit_stream as stream
from research import hunter_bybit_stream_store as storage
from research import hunter_bybit_stream_consumer as consumer
from research import hunter_signal_provenance as provenance
from scripts import hunter_bybit_server_route as route
from scripts import hunter_bybit_stream_ops as ops
from test_hunter_stream import NOW, message, rest

ROOT = Path(__file__).resolve().parents[1]


class RouteTests(unittest.TestCase):
    def environment(self):
        return dict(HUNTER_BYBIT_SERVER_ROUTE='SERVER_DIRECT',
                    HUNTER_RESEARCH_ISOLATED_CHECKOUT='1',
                    HUNTER_BYBIT_SPOT_SOURCE='SERVER_DIRECT',
                    HUNTER_BYBIT_KLINE_SOURCE='SPOT_CACHE', HUNTER_BYBIT_DIRECT_DISABLED='0')

    def test_default_discovery_retains_direct_veto(self):
        env = dict(HUNTER_BYBIT_DIRECT_DISABLED='0')
        self.assertEqual(route.configure('discovery', env), 'DEFAULT')
        self.assertEqual(env['HUNTER_BYBIT_DIRECT_DISABLED'], '1')
        self.assertNotIn('HUNTER_BYBIT_SPOT_SOURCE', env)

    def test_default_availability_unchanged(self):
        env = {}
        self.assertEqual(route.configure('availability', env), 'DEFAULT')
        self.assertEqual(env, {})

    def test_default_both_consumers_reject_residual_direct_before_selected(self):
        from research.hunter_bybit_public import selected
        for job in route.MODULES:
            for platform, extra in [('linux', {}), ('linux', {'GITHUB_ACTIONS':'true'}),
                                    ('win32', {}), ('linux', self.environment())]:
                env = {**extra, 'HUNTER_BYBIT_SPOT_SOURCE':'SERVER_DIRECT'}
                env.pop('HUNTER_BYBIT_SERVER_ROUTE', None)
                with self.subTest(job=job, platform=platform, env=env), patch.dict(os.environ, env, clear=True):
                    # This was the actual leaking path: selected alone chooses direct.
                    self.assertEqual(selected(), 'SERVER_DIRECT')
                    with self.assertRaisesRegex(ValueError, 'DEFAULT_ROUTE_REJECTS_DIRECT'):
                        route.configure(job, os.environ, platform)

    def test_configure_selected_roundtrip_requires_explicit_complete_server_route(self):
        from research.hunter_bybit_public import selected
        for job in route.MODULES:
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(route.configure(job, os.environ, 'linux'), 'DEFAULT')
                self.assertEqual(selected(), 'WORKER')
            with patch.dict(os.environ, self.environment(), clear=True):
                self.assertEqual(route.configure(job, os.environ, 'linux'), 'SERVER_DIRECT')
                self.assertEqual(selected(), 'SERVER_DIRECT')
            for missing in ('HUNTER_RESEARCH_ISOLATED_CHECKOUT', 'HUNTER_BYBIT_KLINE_SOURCE',
                            'HUNTER_BYBIT_DIRECT_DISABLED'):
                env = self.environment(); env.pop(missing)
                with self.subTest(job=job, missing=missing), patch.dict(os.environ, env, clear=True):
                    with self.assertRaises(ValueError): route.configure(job, os.environ, 'linux')

    def test_explicit_server_route_preserves_selectors(self):
        env = self.environment()
        before = dict(env)
        self.assertEqual(route.configure('discovery', env, 'linux'), 'SERVER_DIRECT')
        self.assertEqual(env, before)

    def test_hosted_windows_and_nonisolated_rejected(self):
        for change, platform in [({'GITHUB_ACTIONS':'true'}, 'linux'), ({}, 'win32'),
                                 ({'HUNTER_RESEARCH_ISOLATED_CHECKOUT':'0'}, 'linux')]:
            with self.subTest(change=change, platform=platform), self.assertRaises(ValueError):
                route.configure('discovery', {**self.environment(), **change}, platform)

    def test_conflicting_or_missing_selectors_never_choose_worker(self):
        for key in ('HUNTER_BYBIT_SPOT_SOURCE', 'HUNTER_BYBIT_KLINE_SOURCE', 'HUNTER_BYBIT_DIRECT_DISABLED'):
            for value in ('', 'WORKER', '1'):
                env = self.environment(); env[key] = value
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    route.configure('discovery', env, 'linux')

    def test_unknown_route_rejected(self):
        with self.assertRaisesRegex(ValueError, 'SERVER_SPOT_ROUTE_UNKNOWN'):
            route.configure('discovery', {'HUNTER_BYBIT_SERVER_ROUTE':'typo'})

    def test_wrapper_runs_only_existing_module(self):
        for job, module in route.MODULES.items():
            with patch.dict(os.environ, {}, clear=True), patch('sys.argv', ['route', job]), \
                    patch.object(route.runpy, 'run_module') as run, patch('sys.stdout', new=io.StringIO()):
                route.main()
                run.assert_called_once_with(module, run_name='__main__')

    def test_job_runner_parses_new_routes_and_does_not_override_veto(self):
        from scripts import hunter_job_runner as runner
        for job, needle in [('discovery', 'Scan Binance'), ('research', 'Refresh official Bybit')]:
            # Read the same workflow parser used by the server launcher.
            with patch.object(runner.pathlib, 'Path', side_effect=lambda *p:ROOT.joinpath(*p)):
                steps = runner.workflow_steps(job)
            step = next(x for x in steps if x.get('name', '').startswith(needle))
            self.assertIn('scripts.hunter_bybit_server_route', step['run'])
            self.assertNotIn('HUNTER_BYBIT_DIRECT_DISABLED', step.get('env', {}))


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = storage.Store(Path(self.temp.name)/'state.sqlite3')
        self.names = consumer.topics_for(['GRASS'])
        self.epoch = self.store.begin_session(self.names, 'a'*64)
        self.store.ack(self.epoch, self.names)
        for name in self.names:
            symbol, interval = storage.split_topic(name)
            self.store.ingest_rest(self.epoch, name, rest(symbol, interval), NOW, NOW)
            self.store.ingest_ws(self.epoch, message(symbol, interval, NOW+1), NOW+1)

    def snapshot(self):
        return json.loads((ROOT/'research/results/hunter-bybit-worker-snapshot.json').read_text())

    def test_manifest_binds_full_roster_snapshot_and_source(self):
        snapshot = self.snapshot()
        now = dt.datetime.fromisoformat(snapshot['captured_at_utc'])
        receipt = ops.prepare(snapshot, 'b'*40, now)
        self.assertEqual(receipt['snapshot_sha256'], snapshot['snapshot_sha256'])
        self.assertEqual(len(receipt['topics']), 2*(len(snapshot['venue_status']['signal_expected_bases'])+1))
        self.assertEqual({storage.split_topic(t)[1] for t in receipt['topics']}, {'15', '60'})
        self.assertIn('kline.60.BTCUSDT', receipt['topics'])
        self.assertFalse(receipt['real_trading_enabled'])
        self.assertEqual(receipt['receipt_sha256'], storage.content_hash({k:v for k,v in receipt.items() if k!='receipt_sha256'}))

    def test_stale_corrupt_and_unpinned_manifests_rejected(self):
        snapshot = self.snapshot()
        now = dt.datetime.fromisoformat(snapshot['captured_at_utc'])
        with self.assertRaises(ValueError): ops.prepare(snapshot, 'main', now)
        with self.assertRaises(ValueError): ops.prepare(snapshot, 'b'*40, now+dt.timedelta(seconds=901))
        snapshot['snapshot_sha256'] = '0'*64
        with self.assertRaises(ValueError): ops.prepare(snapshot, 'b'*40, now)

    def test_historical_ws_bar_without_close_is_unknown(self):
        with self.store.db() as db:
            db.execute("UPDATE revisions SET confirmed=0,origin='OFFICIAL_BYBIT_V5_SPOT_WS' WHERE start<?",
                       (NOW//3_600_000*3_600_000,))
        result = self.store.freeze(self.names, NOW+1, NOW+1)
        self.assertEqual(result['reason'], 'CLOSED_BAR_UNCONFIRMED')
        self.assertEqual(result['windows'], {})

    def test_missing_close_marks_gap_after_grace_and_rest_repairs(self):
        name = 'kline.15.BTCUSDT'
        previous = NOW//900_000*900_000-900_000
        with self.store.db() as db:
            db.execute('UPDATE bars SET confirmed=0 WHERE topic=? AND start=?', (name, previous))
        self.store.ingest_ws(self.epoch, message(ts=NOW+2), NOW+2)
        state = next(s for s in self.store.states() if s['topic']==name)
        self.assertTrue(state['gap'])
        self.assertEqual(state['error'], 'MISSING_CLOSE_CONFIRMATION')
        self.store.ingest_rest(self.epoch, name, rest(end=NOW+3), NOW+3, NOW+3)
        self.assertFalse(next(s for s in self.store.states() if s['topic']==name)['gap'])

    def test_packet_validator_rejects_rehashed_unclosed_history(self):
        generation = self.store.freeze(self.names, NOW+1, NOW+1)
        packet = consumer.packet(generation, 'BTCUSDT', '15m', 25)
        packet['transport'][0].update(source='OFFICIAL_BYBIT_V5_SPOT_WS', confirmation='WS_EXPLICIT', confirm=False)
        packet['bars'][0]['confirmed'] = False
        packet['content_hash'] = provenance.hash_value(packet['bars'])
        with self.assertRaisesRegex(ValueError, 'CLOSED_BAR_UNCONFIRMED'):
            provenance.validate_cache_packet(packet, 'BTCUSDT', '15m', 25)

    def test_acceptance_never_claims_publication(self):
        now = dt.datetime.fromtimestamp((NOW+1)/1000, dt.timezone.utc)
        with patch.object(ops, 'roster', return_value=self.names):
            report = ops.acceptance({}, self.store, now, lambda:now)
        self.assertEqual(report['status'], 'READY')
        self.assertEqual(report['windows'], 4)
        self.assertFalse(report['publication_verified'])
        self.store.disconnect(self.epoch)
        with patch.object(ops, 'roster', return_value=self.names):
            self.assertEqual(ops.acceptance({}, self.store, now, lambda:now)['status'], 'UNKNOWN')

    def test_acceptance_rejects_late_read_completion(self):
        now = dt.datetime.fromtimestamp((NOW+1)/1000, dt.timezone.utc)
        with patch.object(ops, 'roster', return_value=self.names):
            report = ops.acceptance({}, self.store, now, lambda:now+dt.timedelta(seconds=91))
        self.assertEqual(report['status'], 'UNKNOWN')
        self.assertEqual(report['windows'], 0)
        self.assertEqual(report['reason'], 'CACHE_READ_FINISHED_STALE_OR_CLOCK_ROLLBACK')

    def test_audit_is_bounded_and_truthful(self):
        with patch('sys.stdout', new=io.StringIO()) as output:
            stream.audit(self.store, self.epoch, NOW+1, 'b'*40)
        record = json.loads(output.getvalue())
        self.assertEqual(record['acked'], 4)
        self.assertEqual(record['stale'], 0)
        self.assertEqual(record['manifest_sha256'], 'a'*64)
        self.assertEqual(record['source_head_sha'], 'b'*40)
        self.assertEqual(record['status'], 'OBSERVATION_ONLY_USE_FROZEN_CHECK_FOR_ACCEPTANCE')

    def test_production_unit_has_no_restart_or_privilege_escalation(self):
        unit = (ROOT/'deploy/systemd/hunter-bybit-stream.service').read_text()
        for line in ['Restart=no', 'StartLimitBurst=1', 'KillMode=control-group', 'TimeoutStopSec=45',
                     'NoNewPrivileges=true', 'ProtectSystem=strict', 'User=hunter-bybit-stream',
                     'ReadWritePaths=/var/lib/hunter-bybit-stream']:
            self.assertIn(line, unit.splitlines())
        self.assertNotIn('ExecStartPost=', unit)


class RecoveryTests(unittest.TestCase):
    def test_backoff_and_five_consecutive_failures(self):
        budget = stream.RecoveryBudget()
        self.assertEqual([budget.failed() for _ in range(5)], [5,10,20,40,80])
        self.assertEqual(budget.failures, 5)

    def test_only_five_minutes_continuous_full_health_restores_budget(self):
        budget = stream.RecoveryBudget(); budget.failed(); budget.failed()
        for stamp in range(0,300,30):
            self.assertFalse(budget.observe(True, stamp))
        self.assertEqual(budget.failures, 2)
        self.assertTrue(budget.observe(True, 300))
        self.assertEqual(budget.failures, 0)

    def test_unknown_missing_samples_and_clock_rollback_break_health_window(self):
        for stamp, ready in [(240,False),(301,True),(100,True)]:
            budget = stream.RecoveryBudget(); budget.failed()
            for tick in range(0,240,30): budget.observe(True,tick)
            self.assertFalse(budget.observe(ready,stamp))
            self.assertFalse(budget.observe(True,stamp+30))
            self.assertEqual(budget.failures,1)

    def test_recovered_session_survives_more_than_five_lifetime_connections(self):
        from unittest.mock import Mock
        for ready, expected in [(True,7),(False,5)]:
            opened=[]; monotonic=[0]
            fake = Mock()
            fake.freeze.return_value = {'status':'READY' if ready else 'UNKNOWN'}
            @contextlib.contextmanager
            def connect():
                opened.append(1)
                yield object()
            def session(store,topics,scope,ws,stop,clock,sleep,*,observer):
                if len(opened)==3:
                    for stamp in range(0,301,30):
                        monotonic[0]=stamp
                        observer(store,'fixture',NOW)
                raise OSError('fixture disconnect')
            with patch.object(stream.Budget,'status',return_value={'halt':None,'cooldown_ms':0}), \
                    patch.object(stream,'session',session), patch('sys.stdout',new=io.StringIO()) as output:
                with self.assertRaisesRegex(RuntimeError,'CONNECTION_BUDGET_EXHAUSTED'):
                    stream.collect(fake,['fixture'],'a'*64,connect,lambda:NOW,lambda _:None,steady=lambda:monotonic[0])
            self.assertEqual(len(opened),expected)
            self.assertIn('BYBIT_SIDECAR_CIRCUIT_OPEN',output.getvalue())
            self.assertEqual('BYBIT_RECOVERY_BUDGET_RESTORED' in output.getvalue(),ready)


if __name__ == '__main__':
    unittest.main()
