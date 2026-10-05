import contextlib
import copy
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location('monitor_persist',
    pathlib.Path(__file__).resolve().parents[1] / 'scripts/hunter_monitor_persist.py')
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)
GEN = '2026-10-05T22:55:00Z'


def fixture():
    d = {}
    for prefix in ('hunter-shadow', 'hunter-shadow-v2'):
        d[prefix + '-portfolio.json'] = dict(schema='hunter_shadow_v2_portfolio_v2',
            mode='SIMULATION_ONLY_NO_REAL_ORDERS', open_positions=[{'asset': 'BTC'}],
            closed_positions=[], updated_at_utc=GEN)
        d[prefix + '-summary.json'] = dict(mode='SIMULATION_ONLY_NO_REAL_ORDERS',
            capital_authority='NONE_SHADOW_ONLY', open_positions=1, closed_positions=0,
            as_of_utc=GEN, policy={'capital_pool_usdt': 20000,
                                 'capital_management': {'initial_capital_usdt': 20000}})
    d['hunter-position-monitor.json'] = {'results': [
        {'lane': 'SHADOW_V1', 'open': 1}, {'lane': 'SHADOW_V2', 'open': 1}]}
    d['hunter-leading-risk.json'] = dict(capital_authority='NONE_SHADOW_ONLY',
        shadow_only=True, real_position_mutation=False, current={'level': 'NORMAL'})
    d['hunter-scheduler-health.json'] = dict(schema='hunter_scheduler_health_v1',
        current_generation_id=GEN, last_successful_monitor_generation_id=GEN,
        shadow_only=True, real_order_count=0)
    return {k: json.dumps(v, sort_keys=True) + '\n' for k, v in d.items()}


class ValidationTests(unittest.TestCase):
    def test_valid_snapshot(self):
        self.assertEqual(m.validate(fixture(), GEN), [1, 1])

    def test_failure_injections(self):
        changes = [
            ('hunter-scheduler-health.json', 'shadow_only', False),
            ('hunter-scheduler-health.json', 'real_order_count', 1),
            ('hunter-scheduler-health.json', 'real_order_count', False),
            ('hunter-scheduler-health.json', 'current_generation_id', 'wrong'),
            ('hunter-scheduler-health.json', 'last_successful_monitor_generation_id', 'wrong'),
            ('hunter-shadow-summary.json', 'closed_positions', 2),
            ('hunter-shadow-v2-summary.json', 'open_positions', 2),
            ('hunter-shadow-summary.json', 'as_of_utc', 'wrong'),
            ('hunter-shadow-portfolio.json', 'mode', 'LIVE'),
            ('hunter-shadow-portfolio.json', 'open_positions', [{}, {}]),
            ('hunter-leading-risk.json', 'real_position_mutation', True),
        ]
        for name, key, value in changes:
            with self.subTest(name=name, key=key):
                raw = fixture()
                doc = json.loads(raw[name]); doc[key] = value
                raw[name] = json.dumps(doc)
                with self.assertRaises((RuntimeError, ValueError, KeyError)):
                    m.validate(raw, GEN)
        for name in m.NAMES:
            for mutation in ('missing', 'malformed'):
                with self.subTest(name=name, mutation=mutation):
                    raw = fixture()
                    if mutation == 'missing': del raw[name]
                    else: raw[name] = '{'
                    with self.assertRaises((RuntimeError, ValueError, KeyError)):
                        m.validate(raw, GEN)

    def test_capital_pool_invariant(self):
        for field in ('capital_pool_usdt', 'initial_capital_usdt'):
            raw = fixture(); doc = json.loads(raw['hunter-shadow-v2-summary.json'])
            target = doc['policy'] if field == 'capital_pool_usdt' else doc['policy']['capital_management']
            target[field] = 50000
            raw['hunter-shadow-v2-summary.json'] = json.dumps(doc)
            with self.assertRaisesRegex(RuntimeError, 'V2_INITIAL_CAPITAL'):
                m.validate(raw, GEN)

    def test_readback_failure_never_prints_success(self):
        for corruption in ('missing', 'malformed', 'generation', 'state'):
            raw = fixture()
            if corruption == 'missing': del raw[m.NAMES[0]]
            elif corruption == 'malformed': raw[m.NAMES[0]] = '{'
            elif corruption == 'generation':
                raw['hunter-scheduler-health.json'] = raw['hunter-scheduler-health.json'].replace(GEN, 'wrong')
            else: raw[m.NAMES[0]] += ' '
            out = io.StringIO()
            with mock.patch.object(m, 'git'), mock.patch.object(m, 'snapshot', return_value=raw), contextlib.redirect_stdout(out):
                with self.assertRaises((RuntimeError, ValueError, KeyError)):
                    m.readback(fixture(), GEN)
            self.assertNotIn('SERVER_POST_PUSH_READBACK_OK', out.getvalue())


class GitRaceTests(unittest.TestCase):
    def run_git(self, directory, *args):
        return subprocess.run(['git', *args], cwd=directory, check=True,
                              capture_output=True, text=True)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); root = pathlib.Path(self.temp.name)
        self.remote, self.writer, self.other = (root / x for x in ('remote.git', 'writer', 'other'))
        self.run_git(root, 'init', '--bare', '--initial-branch=main', str(self.remote))
        self.run_git(root, 'clone', str(self.remote), str(self.writer))
        for key, value in [('user.name', 'test'), ('user.email', 'test@localhost')]:
            self.run_git(self.writer, 'config', key, value)
        for name, raw in fixture().items():
            p = self.writer / m.RESULTS / name; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(raw)
        self.run_git(self.writer, 'add', '.'); self.run_git(self.writer, 'commit', '-m', 'baseline')
        self.run_git(self.writer, 'push', 'origin', 'main')
        self.base = self.run_git(self.writer, 'rev-parse', 'HEAD').stdout.strip()
        self.run_git(root, 'clone', str(self.remote), str(self.other))
        for key, value in [('user.name', 'other'), ('user.email', 'other@localhost')]:
            self.run_git(self.other, 'config', key, value)
        self.previous = os.getcwd(); os.chdir(self.writer)
        # Valid local mutation awaiting publication.
        p = pathlib.Path(m.PATHS[0]); p.write_text(p.read_text() + ' ')

    def tearDown(self):
        os.chdir(self.previous); self.temp.cleanup()

    def competing_commit(self, protected=False):
        self.run_git(self.other, 'fetch', 'origin', 'main')
        self.run_git(self.other, 'reset', '--hard', 'origin/main')
        p = self.other / (m.PATHS[0] if protected else 'unrelated.md')
        p.write_text((p.read_text() if p.exists() else '') + ' ')
        self.run_git(self.other, 'add', '.'); self.run_git(self.other, 'commit', '-m', 'competitor')
        self.run_git(self.other, 'push', 'origin', 'main')

    def exercise_race(self, races, protected=False):
        real_git = m.git; push_calls = []
        def intercepted(*args, **kwargs):
            if args[0] == 'push':
                push_calls.append(args)
                if len(push_calls) <= races:
                    self.competing_commit(protected)
            return real_git(*args, **kwargs)
        with mock.patch.object(m, 'git', side_effect=intercepted):
            m.persist(self.base, GEN)
        return len(push_calls)

    def test_nonprotected_rebase_and_retry(self):
        self.assertEqual(self.exercise_race(2), 3)
        self.assertEqual(m.snapshot(), m.snapshot('origin/main'))

    def test_protected_race_rejected_and_remote_untouched(self):
        with self.assertRaisesRegex(RuntimeError, 'CAS_REJECTED_STALE_WRITER'):
            self.exercise_race(1, True)
        m.git('fetch', 'origin', 'main')
        self.assertEqual(m.git('show', 'origin/main:' + m.PATHS[0]).stdout,
                         (self.other / m.PATHS[0]).read_text())

    def test_five_races_exhausted_without_success(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaisesRegex(RuntimeError, 'PUSH_RACE_RETRY_EXHAUSTED'):
                self.exercise_race(5)
        self.assertEqual(out.getvalue().count('SHADOW_STATE_PUSH_RACE'), 5)
        self.assertNotIn('SERVER_POST_PUSH_READBACK_OK', out.getvalue())

    def test_failed_writer_does_not_publish_generation_and_healthy_writer_recovers(self):
        p = self.other / m.RESULTS / 'hunter-scheduler-health.json'
        old = json.loads(p.read_text()); old['current_generation_id'] = old['last_successful_monitor_generation_id'] = '2026-10-05T22:50:00Z'
        p.write_text(json.dumps(old)); self.run_git(self.other, 'add', '.')
        self.run_git(self.other, 'commit', '-m', 'old generation'); self.run_git(self.other, 'push', 'origin', 'main')
        with self.assertRaisesRegex(RuntimeError, 'CAS_REJECTED_STALE_WRITER'):
            m.persist(self.base, GEN)
        self.assertIn('22:50:00', m.git('show', 'origin/main:' + m.RESULTS + 'hunter-scheduler-health.json').stdout)
        m.git('reset', '--hard', 'origin/main')
        fresh = m.git('rev-parse', 'HEAD').stdout.strip()
        for name, raw in fixture().items(): pathlib.Path(m.RESULTS + name).write_text(raw)
        m.persist(fresh, GEN)
        self.assertEqual(json.loads(m.snapshot('origin/main')['hunter-scheduler-health.json'])['current_generation_id'], GEN)

    def test_transitive_inputs_protected(self):
        for p in ['research/hunter_cex_scan.py', 'research/hunter_market.py',
                  'research/hunter_scheduler_health.py', m.PATHS[-1]]:
            self.assertTrue(m.protected(p))


if __name__ == '__main__':
    unittest.main()
