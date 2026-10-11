import ast
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

# Enumerate the audited read/write contract independently of the implementation:
# omitting a dependency from the production manifest must still fail its race.
MONITOR_RESULT_CASES = (
    'hunter-shadow-portfolio.json', 'hunter-shadow-v2-portfolio.json',
    'hunter-shadow-summary.json', 'hunter-shadow-v2-summary.json',
    'hunter-position-monitor.json', 'hunter-leading-risk.json',
    'hunter-scheduler-health.json', 'hunter-shadow-rules.json',
    'hunter-cex-universe-run.json', 'hunter-tactical-capital-review.json',
    'hunter-liquidity-probe.json', 'hunter-tactical-supply-risk.json',
    'hunter-shadow-v1-overfilter-guard.json', 'hunter-shadow-v2-overfilter-guard.json',
)
INDEPENDENT_RESULT_CASES = (
    'hunter-health-and-queue.json', 'hunter-runtime-monitor-health.json',
    'hunter-early-signals.json', 'hunter-identity-audit.json',
    'hunter-bybit-availability.json', 'hunter-strategy-release.json',
)


def fixture():
    d = {}
    for prefix in ('hunter-shadow', 'hunter-shadow-v2'):
        d[prefix + '-portfolio.json'] = dict(schema='hunter_shadow_v2_portfolio_v2',
            mode='SIMULATION_ONLY_NO_REAL_ORDERS', open_positions=[{'asset': 'BTC',
                'tranches': [{'notional_usdt': 1000}]}],
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

    def test_actual_exposure_hard_cap_and_invalid_amounts(self):
        for amount in (20000, 20001, -1, 0, None, True, '1000', float('nan'), float('inf')):
            with self.subTest(amount=amount):
                raw = fixture()
                doc = json.loads(raw['hunter-shadow-v2-portfolio.json'])
                doc['open_positions'][0]['tranches'] = [{'notional_usdt': amount}]
                raw['hunter-shadow-v2-portfolio.json'] = json.dumps(doc)
                if amount == 20000:
                    self.assertEqual(m.validate(raw, GEN), [1, 1])
                else:
                    with self.assertRaisesRegex(RuntimeError, 'V2_(EXPOSURE|INVALID_TRANCHE)'):
                        m.validate(raw, GEN)

    def test_exposure_sums_all_tranches_and_rejects_missing(self):
        for tranches in (None, [], [{'notional_usdt': 11000}, {'notional_usdt': 10000}]):
            raw = fixture()
            doc = json.loads(raw['hunter-shadow-v2-portfolio.json'])
            doc['open_positions'][0]['tranches'] = tranches
            raw['hunter-shadow-v2-portfolio.json'] = json.dumps(doc)
            with self.assertRaisesRegex(RuntimeError, 'V2_EXPOSURE'):
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

    def test_monitor_result_manifest_matches_reviewed_contract(self):
        self.assertEqual(m.MONITOR_RESULT_PATHS,
                         {m.RESULTS + name for name in MONITOR_RESULT_CASES})
        for name in INDEPENDENT_RESULT_CASES:
            self.assertFalse(m.protected(m.RESULTS + name), name)

    def test_direct_monitor_result_paths_cannot_drift_outside_manifest(self):
        # A small source-level tripwire for new direct file dependencies, without
        # executing strategies/network or importing their unused main() paths.
        # Transitive/constructed dependencies still require call-path review.
        source = pathlib.Path(__file__).resolve().parents[1] / 'research/hunter_position_monitor.py'
        tree = ast.parse(source.read_text())
        paths = {}
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            value = node.value
            resolved = None
            if (isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute)
                    and value.func.attr == 'Path' and len(value.args) == 1
                    and isinstance(value.args[0], ast.Constant)
                    and isinstance(value.args[0].value, str)):
                resolved = value.args[0].value
            elif (isinstance(value, ast.BinOp) and isinstance(value.op, ast.Div)
                    and isinstance(value.left, ast.Name) and value.left.id in paths
                    and isinstance(value.right, ast.Constant)
                    and isinstance(value.right.value, str)):
                resolved = paths[value.left.id].rstrip('/') + '/' + value.right.value
            if resolved is not None:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        paths[target.id] = resolved
        result_paths = {p for p in paths.values() if p.startswith(m.RESULTS) and p.endswith('.json')}
        self.assertTrue(result_paths, 'MONITOR_RESULT_DECLARATIONS_NOT_FOUND')
        self.assertFalse(result_paths - m.MONITOR_RESULT_PATHS,
                         'Review new Monitor result dependencies before changing the manifest')


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

    def competing_result_commit(self, name):
        self.run_git(self.other, 'fetch', 'origin', 'main')
        self.run_git(self.other, 'reset', '--hard', 'origin/main')
        path = self.other / m.RESULTS / name
        # Whitespace changes the tracked bytes without invalidating the snapshot.
        path.write_text((path.read_text() if path.exists() else '{}\n') + ' ')
        self.run_git(self.other, 'add', '.')
        self.run_git(self.other, 'commit', '-m', 'concurrent result update')
        self.run_git(self.other, 'push', 'origin', 'main')
        return self.run_git(self.other, 'rev-parse', 'HEAD').stdout.strip()

    def fresh_pending_writer(self):
        # Every subcase starts from the latest temporary main; no earlier
        # protected competitor can accidentally cause a later case to pass.
        m.git('fetch', 'origin', 'main')
        m.git('reset', '--hard', 'origin/main')
        base = m.git('rev-parse', 'HEAD').stdout.strip()
        path = pathlib.Path(m.PATHS[0])
        path.write_text(path.read_text() + ' ')
        return base

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
                  'research/hunter_scheduler_health.py', 'scripts/hunter_portfolio_lock.py',
                  'scripts/hunter_job_runner.py', m.PATHS[-1], 'research/__init__.py',
                  'scripts/__init__.py', m.RESULTS+'hunter-cex-universe-run.json',
                  m.RESULTS+'hunter-tactical-capital-review.json',
                  m.RESULTS+'hunter-liquidity-probe.json',
                  m.RESULTS+'hunter-shadow-v2-overfilter-guard.json']:
            self.assertTrue(m.protected(p))

    def test_decision_input_race_rejected_without_publishing(self):
        p = self.other / m.RESULTS / 'hunter-tactical-capital-review.json'
        p.write_text('{"generation":"new"}')
        self.run_git(self.other, 'add', '.')
        self.run_git(self.other, 'commit', '-m', 'concurrent decision input')
        self.run_git(self.other, 'push', 'origin', 'main')
        with self.assertRaisesRegex(RuntimeError, 'CAS_REJECTED_STALE_WRITER'):
            m.persist(self.base, GEN)
        self.assertEqual(m.git('show','origin/main:'+m.PATHS[0]).stdout, fixture()[m.NAMES[0]])

    def test_each_monitor_result_race_rejected_without_overwriting_remote(self):
        for name in MONITOR_RESULT_CASES:
            with self.subTest(name=name):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    base = self.fresh_pending_writer()
                    real_git = m.git
                    pushes = []
                    competing_heads = []

                    def intercepted(*args, **kwargs):
                        if args[0] == 'push':
                            pushes.append(args)
                            if len(pushes) == 1:
                                competing_heads.append(self.competing_result_commit(name))
                        return real_git(*args, **kwargs)

                    with mock.patch.object(m, 'git', side_effect=intercepted):
                        with self.assertRaisesRegex(RuntimeError, 'CAS_REJECTED_STALE_WRITER') as error:
                            m.persist(base, GEN)
                    self.assertIn(m.RESULTS + name, str(error.exception))
                    self.assertEqual(len(pushes), 1)
                    self.assertEqual(self.run_git(self.remote, 'rev-parse', 'main').stdout.strip(),
                                     competing_heads[0])
                    expected = {n: (self.other / m.RESULTS / n).read_text() for n in m.NAMES}
                    self.assertEqual(m.snapshot('origin/main'), expected)
                self.assertNotIn('SERVER_POST_PUSH_READBACK_OK', out.getvalue())
                self.assertNotIn('SHADOW_STATE_PUSH_VERIFIED', out.getvalue())

    def test_independent_result_races_rebase_and_preserve_both_ledgers(self):
        for name in INDEPENDENT_RESULT_CASES:
            with self.subTest(name=name):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    base = self.fresh_pending_writer()
                    expected = m.snapshot()
                    real_git = m.git
                    pushes = []

                    def intercepted(*args, **kwargs):
                        if args[0] == 'push':
                            pushes.append(args)
                            if len(pushes) == 1:
                                self.competing_result_commit(name)
                        return real_git(*args, **kwargs)

                    with mock.patch.object(m, 'git', side_effect=intercepted):
                        m.persist(base, GEN)
                    self.assertEqual(len(pushes), 2)
                    self.assertEqual(m.snapshot('origin/main'), expected)
                    self.assertEqual(m.snapshot(), expected)
                    self.assertEqual(m.git('show', 'origin/main:' + m.RESULTS + name).stdout,
                                     (self.other / m.RESULTS / name).read_text())
                self.assertIn('SERVER_POST_PUSH_READBACK_OK', out.getvalue())
                self.assertIn('SHADOW_STATE_PUSH_VERIFIED', out.getvalue())

    def test_duplicate_persistence_adds_no_commit_or_trade(self):
        m.persist(self.base, GEN)
        head = m.git('rev-parse', 'HEAD').stdout.strip()
        before = m.snapshot('origin/main')
        m.persist(head, GEN)
        self.assertEqual(m.git('rev-parse', 'HEAD').stdout.strip(), head)
        self.assertEqual(m.snapshot('origin/main'), before)

    def test_rebind_discards_pre_lock_pair_and_preserves_new_history(self):
        m.git('reset', '--hard', self.base)
        for name in m.NAMES[:2]:
            p = self.other / m.RESULTS / name
            doc = json.loads(p.read_text())
            doc['events'] = [{'type': 'MONITOR_EXIT', 'id': 'retained'}]
            doc['circuit_breaker'] = {'status': 'TRIPPED'}
            p.write_text(json.dumps(doc))
        self.run_git(self.other, 'add', '.')
        self.run_git(self.other, 'commit', '-m', 'latest monitor history')
        self.run_git(self.other, 'push', 'origin', 'main')
        with mock.patch.dict(os.environ, {'HUNTER_MONITOR_ISOLATED_CHECKOUT': '1'}):
            latest = m.rebind_after_prepare(self.base, isolated=True)
        self.assertNotEqual(latest, self.base)
        self.assertEqual(m.snapshot(), m.snapshot('origin/main'))
        for name in m.NAMES[:2]:
            self.assertEqual(json.loads(m.snapshot()[name])['events'][0]['id'], 'retained')

    def test_rebind_rejects_changed_tested_code_without_reset(self):
        m.git('reset', '--hard', self.base)
        p = self.other / 'scripts/hunter_portfolio_lock.py'
        p.parent.mkdir(parents=True); p.write_text('# new code')
        self.run_git(self.other, 'add', '.')
        self.run_git(self.other, 'commit', '-m', 'changed code')
        self.run_git(self.other, 'push', 'origin', 'main')
        with mock.patch.dict(os.environ, {'HUNTER_MONITOR_ISOLATED_CHECKOUT': '1'}):
            with self.assertRaisesRegex(RuntimeError, 'MONITOR_PREPARATION_INVALIDATED'):
                m.rebind_after_prepare(self.base, isolated=True)
        self.assertEqual(m.git('rev-parse', 'HEAD').stdout.strip(), self.base)

    def test_rebind_never_resets_dirty_or_non_disposable_checkout(self):
        with self.assertRaisesRegex(RuntimeError, 'DISPOSABLE_MONITOR_CHECKOUT_REQUIRED'):
            m.rebind_after_prepare(self.base)
        before = m.snapshot()
        with mock.patch.dict(os.environ, {'HUNTER_MONITOR_ISOLATED_CHECKOUT': '1'}):
            with self.assertRaisesRegex(RuntimeError, 'UNEXPECTED_MONITOR_PREPARE_MUTATION'):
                m.rebind_after_prepare(self.base, isolated=True)
        self.assertEqual(m.snapshot(), before)


if __name__ == '__main__':
    unittest.main()

