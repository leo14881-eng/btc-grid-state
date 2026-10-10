import copy
import json
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from scripts.hunter_trade_notifications import digest, event_id
from scripts.shadow_readonly_artifacts import (
    Snapshot, PATHS, JOBS, build, candidate_batch, daily_review, read_snapshot, sha256)

SHA = 'a' * 40
AT = '2026-10-10T13:00:00Z'


def fixture():
    event = dict(type='SHADOW_V2_BUY', at=AT, asset='TEST', shadow_id='s1',
                 tranches=1, price=2, notional_usdt=1000)
    portfolio = dict(schema='hunter_shadow_v2_portfolio_v2', mode='SIMULATION_ONLY_NO_REAL_ORDERS',
                     open_positions=[], closed_positions=[], decisions=[], events=[event],
                     updated_at_utc=AT, last_cycle_generation_id='observation-1')
    scheduler = dict(current_generation_id='bucket-1', last_successful_monitor_generation_id='bucket-1',
                     shadow_only=True, real_order_count=0, monitor_started_at_utc=AT,
                     monitor_completed_at_utc=AT)
    cursor = dict(writer='CHATGPT_NOTIFICATION_CONSUMER_ONLY', real_trading_enabled=False,
                  capital_authority='NONE_SHADOW_ONLY', acknowledged_event_ids=[],
                  event_not_before_utc='2026-10-01T00:00:00Z')
    stock = dict(simulation_only=True, positions={}, closed=[], updated_at=AT,
                 run_id='run-1', source_commit=SHA)
    docs = dict(portfolio=portfolio, summary=dict(generation_id='observation-1', as_of_utc=AT),
                rules={'lanes': {'V2': {'capital_pool_usdt': 20000}}},
                scheduler=scheduler, cursor=cursor, stock_portfolio=stock, stock_trades=[])
    for name in JOBS:
        docs[name] = dict(status='RUN_COMPLETED' if name.startswith('stock_') else 'SUCCESS',
                          completed_at_utc=AT, created_at=AT, real_trading_enabled=False,
                          capital_authority='NONE_SHADOW_ONLY', main_readback_verified=True)
    docs['hunter_monitor']['monitor_generation_id'] = 'bucket-1'
    docs['stock_manifest'] = dict(status='RUN_COMPLETED', admitted=True, real_orders=False,
                                  run_id='run-1', source_commit=SHA)
    return docs


def snapshot(docs):
    docs = copy.deepcopy(docs)
    raw = {k: json.dumps(v).encode() for k, v in docs.items()}
    docs['hunter_monitor']['scheduler_health_sha256'] = sha256(raw['scheduler'])
    docs['stock_manifest']['files'] = {PATHS[k]: sha256(raw[k]) for k in ('stock_portfolio', 'stock_trades')}
    for k in ('hunter_monitor', 'stock_manifest'):
        raw[k] = json.dumps(docs[k]).encode()
    return Snapshot(SHA, raw)


def artifact(s):
    return build(s, '2026-10-10T13:01:00Z', {k: 120 for k in JOBS},
                 '2026-10-10T00:00:00Z', AT, 'UTC')


class ReadonlyArtifactsTests(unittest.TestCase):
    def test_parallel_deterministic_no_mutation(self):
        s = snapshot(fixture())
        before = copy.deepcopy(s.raw)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: artifact(s), range(24)))
        self.assertTrue(all(r == results[0] for r in results))
        self.assertEqual(s.raw, before)
        self.assertEqual(results[0]['status'], 'PARTIAL')
        self.assertFalse(results[0]['real_trading_enabled'])
        self.assertEqual(len(results[0]['notification_candidates']['events']), 1)

    def test_all_reservation_locations_suppress_without_ack(self):
        for field in ('acknowledged_event_ids', 'reserved_event_ids', 'pending_event_ids', 'unresolved_batches'):
            d = fixture()
            identity = event_id(d['portfolio']['events'][0])
            d['cursor'][field] = [{'event_ids': [identity]}] if field == 'unresolved_batches' else [identity]
            self.assertEqual(candidate_batch(snapshot(d))['events'], [])

    def test_reserved_mutation_and_ambiguous_reservations_fail_closed(self):
        d = fixture()
        e = d['portfolio']['events'][0]
        d['cursor']['reserved_event_content_hashes'] = {event_id(e): digest(e)}
        e['price'] = 3
        self.assertEqual(artifact(snapshot(d))['notification_candidates']['status'], 'UNKNOWN')
        d = fixture()
        d['cursor']['pending_batch_id'] = 'unverifiable'
        self.assertEqual(artifact(snapshot(d))['notification_candidates']['status'], 'UNKNOWN')

    def test_cursor_race_changes_precondition_and_artifact(self):
        d = fixture()
        before = artifact(snapshot(d))
        d['cursor']['reserved_event_ids'] = [event_id(d['portfolio']['events'][0])]
        after = artifact(snapshot(d))
        self.assertNotEqual(before['artifact_id'], after['artifact_id'])
        self.assertNotEqual(before['notification_candidates']['cursor_sha256'],
                            after['notification_candidates']['cursor_sha256'])
        self.assertEqual(after['notification_candidates']['events'], [])

    def test_mismatched_generations_hashes_and_missing_input(self):
        for key, field, value in [('summary', 'generation_id', 'other'),
                                  ('hunter_monitor', 'monitor_generation_id', None),
                                  ('stock_portfolio', 'run_id', 'other')]:
            d = fixture(); d[key][field] = value
            r = artifact(snapshot(d))
            section = 'stock_daily_review' if key == 'stock_portfolio' else 'notification_candidates'
            self.assertEqual(r[section]['status'], 'UNKNOWN')
        s = snapshot(fixture()); s.raw['scheduler'] += b' '
        self.assertEqual(artifact(s)['notification_candidates']['status'], 'UNKNOWN')
        s = snapshot(fixture()); del s.raw['stock_trades']
        self.assertEqual(artifact(s)['stock_daily_review']['status'], 'UNKNOWN')

    def test_stale_future_and_unknown_freshness(self):
        s = snapshot(fixture())
        for now, budgets in [('2026-10-10T14:00:00Z', {k: 10 for k in JOBS}),
                             ('2026-10-10T12:00:00Z', {k: 10 for k in JOBS}),
                             ('2026-10-10T13:01:00Z', {})]:
            r = build(s, now, budgets, '2026-10-10T00:00:00Z', AT, 'UTC')
            self.assertEqual(r['notification_candidates']['status'], 'UNKNOWN')

    def test_daily_half_open_timezone_duplicate_and_partial_valuation(self):
        d = fixture()
        event = dict(type='BUY', symbol='TEST', at='2026-10-10T08:00:00+08:00', price=2, notional=1000)
        d['stock_trades'] = [event, copy.deepcopy(event), dict(event, at=AT)]
        r = daily_review(snapshot(d), '2026-10-10T00:00:00Z', AT, 'Asia/Shanghai')
        self.assertEqual(r['trade_counts']['BUY'], 1)
        self.assertEqual(r['end_exclusive'], '2026-10-10T21:00:00+08:00')
        d['stock_portfolio']['positions']['TEST'] = dict(tranches=[dict(price=2, notional=1000)], quote_status='STALE')
        r = daily_review(snapshot(d), '2026-10-10T00:00:00Z', AT, 'UTC')
        self.assertEqual(r['status'], 'PARTIAL')
        self.assertIsNone(r['current_snapshot_valuation']['unrealized_net_pnl_usdt'])
        d['stock_trades'].append(dict(event, price=3))
        self.assertEqual(artifact(snapshot(d))['stock_daily_review']['status'], 'UNKNOWN')

    def test_invalid_dates_missing_pnl_and_incomplete_window(self):
        d = fixture()
        d['stock_portfolio']['closed'] = [dict(symbol='TEST', closed_at=AT, tranches=[dict(price=2, notional=1000)])]
        self.assertEqual(artifact(snapshot(d))['stock_daily_review']['status'], 'UNKNOWN')
        d = fixture(); d['portfolio']['events'][0]['at'] = '2026-10-10T13:00:00'
        self.assertEqual(artifact(snapshot(d))['notification_candidates']['status'], 'UNKNOWN')
        r = daily_review(snapshot(fixture()), AT, '2026-10-11T00:00:00Z', 'UTC')
        self.assertFalse(r['interval_watermark_reached'])
        self.assertEqual(r['status'], 'PARTIAL')

    def test_capital_breach_and_future_event_are_reported(self):
        d = fixture()
        d['portfolio']['open_positions'] = [{'tranches': [{'notional_usdt': 18000}]}]
        r = artifact(snapshot(d))
        self.assertEqual(r['hunter_ledger']['status'], 'PARTIAL')
        self.assertTrue(r['hunter_ledger']['capital_limit_breached'])
        d['portfolio']['events'][0]['at'] = '2026-10-10T13:02:00Z'
        self.assertEqual(artifact(snapshot(d))['notification_candidates']['status'], 'UNKNOWN')
        d = fixture(); d['hunter_watchdog']['fast_watch_health'] = {'status': 'PARTIAL_FAST_PATH_DEGRADED'}
        self.assertEqual(artifact(snapshot(d))['health_checks']['hunter_watchdog']['status'], 'PARTIAL')

    def test_reader_ignores_dirty_checkout_and_moving_head(self):
        with tempfile.TemporaryDirectory() as temp:
            def git(*args):
                return subprocess.run(['git', '-C', temp, *args], check=True, capture_output=True).stdout.decode().strip()
            git('init'); git('config', 'user.name', 'Offline test'); git('config', 'user.email', 'test@example.invalid')
            s = snapshot(fixture())
            for key, raw in s.raw.items():
                path = Path(temp) / PATHS[key]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw)
            git('add', '.'); git('commit', '-m', 'fixture')
            sha = git('rev-parse', 'HEAD')
            before = read_snapshot(temp, sha)
            path = Path(temp) / PATHS['cursor']; path.write_text('{}')
            git('add', '.'); git('commit', '-m', 'new cursor')
            path.write_text('invalid dirty json')
            self.assertEqual(read_snapshot(temp, sha).raw, before.raw)
            self.assertEqual(path.read_text(), 'invalid dirty json')
            with self.assertRaises(ValueError): read_snapshot(temp, 'main')


if __name__ == '__main__':
    unittest.main()
