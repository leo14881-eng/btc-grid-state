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
                     monitor_completed_at_utc=AT, state_revision=SHA)
    cursor = dict(schema='hunter_notification_runtime_v1', notification_policy='V2_NEW_TRADE_EVENTS_ONLY',
                  writer='CHATGPT_NOTIFICATION_CONSUMER_ONLY', real_trading_enabled=False,
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
                          capital_authority='NONE_SHADOW_ONLY', main_readback_verified=True,
                          main_readback_head_sha=SHA, source_head_sha=SHA, started_at_utc=AT,
                          job=name.split('_', 1)[1], source='VULTR_SYSTEMD',
                          schema='hunter_runtime_job_health_v1')
        if name.startswith('stock_'):
            docs[name].update(schema='stock_shadow_generation_v1', shadow_only=True, real_orders=False,
                              owner='server', epoch=3, run_id='run-1', generation='gen-1',
                              admitted=True, source_commit=SHA)
    docs['hunter_monitor']['monitor_generation_id'] = 'bucket-1'
    docs['stock_manifest'] = dict(status='RUN_COMPLETED', admitted=True, real_orders=False,
                                  run_id='run-1', source_commit=SHA, job='main', owner='server', epoch=3,
                                  generation='gen-1', shadow_only=True, schema='stock_shadow_generation_v1')
    sync_summary(docs)
    return docs


def sync_summary(d):
    p, s = d['portfolio'], d['summary']
    archive = p.get('closed_trade_archive', [])
    realized = sum(x['net_pnl_usdt'] for x in p['closed_positions'] + archive)
    used = sum(t['notional_usdt'] for x in p['open_positions'] for t in x['tranches'])
    reserve = sum(t.get('strategic_notional_usdt', 0) for x in p['open_positions'] for t in x['tranches'])
    ordinary = used - reserve
    s.update(schema='hunter_shadow_v2_summary_v3', mode='SIMULATION_ONLY_NO_REAL_ORDERS',
             capital_authority='NONE_SHADOW_ONLY', open_positions=len(p['open_positions']),
             closed_positions=len(p['closed_positions']), archived_closed_positions=len(archive),
             total_closed_positions=len(p['closed_positions']) + len(archive),
             net_pnl_usdt=round(realized, 2), realized_net_pnl_usdt=round(realized, 2),
             policy={'capital_pool_usdt': 20000, 'capital_management': {
                 'used_capital_usdt': used, 'ordinary_used': ordinary, 'strategic_reserve_used': reserve,
                 'ordinary_available': max(0, 17000-ordinary), 'strategic_reserve_available': max(0, 3000-reserve),
                 'capital_pool': 20000, 'ordinary_opportunity_cap': 17000, 'strategic_reserve': 3000,
                 'initial_capital_usdt': 20000, 'realized_net_pnl_usdt': round(realized, 2),
                 'equity_usdt': max(0, 20000+realized), 'total_cash_usdt': max(0, 20000+realized-used)}})


def snapshot(docs):
    docs = copy.deepcopy(docs)
    raw = {k: json.dumps(v).encode() for k, v in docs.items()}
    docs['hunter_monitor']['scheduler_health_sha256'] = sha256(raw['scheduler'])
    docs['stock_manifest']['files'] = {PATHS[k]: sha256(raw[k]) for k in ('stock_portfolio', 'stock_trades')}
    for k in ('hunter_monitor', 'stock_manifest'):
        raw[k] = json.dumps(docs[k]).encode()
    return Snapshot(SHA, raw, {SHA: True})


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

    def test_review_summary_count_and_amount_conflicts_block_candidates(self):
        for field in ('open_positions', 'closed_positions', 'archived_closed_positions',
                      'total_closed_positions', 'net_pnl_usdt', 'realized_net_pnl_usdt'):
            with self.subTest(field=field):
                d = fixture(); d['summary'][field] = 999
                r = artifact(snapshot(d))
                self.assertEqual(r['hunter_ledger']['status'], 'UNKNOWN')
                self.assertEqual(r['notification_candidates']['status'], 'UNKNOWN')
        d = fixture()
        d['summary']['policy'] = {'capital_management': {'used_capital_usdt': 999999}}
        r = artifact(snapshot(d))
        self.assertEqual(r['hunter_ledger']['status'], 'UNKNOWN')
        self.assertEqual(r['notification_candidates']['status'], 'UNKNOWN')

    def test_review_duplicate_shadow_identity_is_not_complete(self):
        d = fixture()
        position = dict(shadow_id='duplicate', asset='TEST', tranches=[{'notional_usdt': 1000}])
        d['portfolio']['open_positions'] = [position, copy.deepcopy(position)]
        sync_summary(d)
        r = artifact(snapshot(d))
        self.assertEqual(r['hunter_ledger']['status'], 'UNKNOWN')
        self.assertEqual(r['notification_candidates']['status'], 'UNKNOWN')

    def test_review_unsafe_or_missing_receipt_evidence_is_not_complete(self):
        for job in JOBS:
            if job.startswith('stock_'):
                continue
            for field, value in [('main_readback_verified', False), ('real_trading_enabled', True),
                                 ('capital_authority', 'LIVE'), ('main_readback_head_sha', 'bad')]:
                with self.subTest(job=job, field=field):
                    d = fixture(); d[job][field] = value
                    r = artifact(snapshot(d))
                    self.assertEqual(r['health_checks'][job]['status'], 'UNKNOWN')
                    self.assertEqual(r['notification_candidates']['status'], 'UNKNOWN')
            d = fixture(); del d[job]['real_trading_enabled']
            self.assertEqual(artifact(snapshot(d))['health_checks'][job]['status'], 'UNKNOWN')

    def test_review_missing_summary_and_corrupt_capital_fields_are_unknown(self):
        for field in ('schema', 'capital_authority', 'open_positions', 'realized_net_pnl_usdt', 'policy'):
            with self.subTest(field=field):
                d = fixture(); del d['summary'][field]
                self.assertEqual(artifact(snapshot(d))['hunter_ledger']['status'], 'UNKNOWN')
                self.assertEqual(artifact(snapshot(d))['notification_candidates']['status'], 'UNKNOWN')
        for field in fixture()['summary']['policy']['capital_management']:
            for value in (None, True, 999999, float('nan')):
                with self.subTest(field=field, value=value):
                    d = fixture(); d['summary']['policy']['capital_management'][field] = value
                    self.assertEqual(artifact(snapshot(d))['hunter_ledger']['status'], 'UNKNOWN')

    def test_review_duplicate_cross_book_identity_and_missing_pnl(self):
        for container in ('closed_positions', 'closed_trade_archive'):
            d = fixture()
            row = dict(shadow_id='duplicate', net_pnl_usdt=1, tranches=[{'notional_usdt': 1000}])
            d['portfolio']['open_positions'] = [row]
            d['portfolio'][container] = [copy.deepcopy(row)]
            sync_summary(d)
            self.assertEqual(artifact(snapshot(d))['hunter_ledger']['status'], 'UNKNOWN')
        d = fixture(); d['portfolio']['closed_positions'] = [{'shadow_id': 'closed'}]
        d['summary'].update(closed_positions=1, total_closed_positions=1)
        self.assertEqual(artifact(snapshot(d))['hunter_ledger']['status'], 'UNKNOWN')

    def test_review_readback_lineage_and_stock_manifest_evidence(self):
        s = snapshot(fixture()); s.ancestors = {}
        r = artifact(s)
        self.assertEqual(r['health_checks']['hunter_discovery']['status'], 'UNKNOWN')
        self.assertEqual(r['notification_candidates']['status'], 'UNKNOWN')
        d = fixture(); d['hunter_discovery']['main_readback_head_sha'] = 'b' * 40
        s = snapshot(d); s.ancestors['b' * 40] = True
        self.assertEqual(artifact(s)['health_checks']['hunter_discovery']['status'], 'UNKNOWN')
        s.ancestors[SHA + '..' + 'b' * 40] = True
        self.assertEqual(artifact(s)['health_checks']['hunter_discovery']['status'], 'COMPLETE')
        for field, value in [('real_orders', True), ('shadow_only', None), ('admitted', False),
                             ('source_commit', 'bad'), ('epoch', True), ('generation', None)]:
            d = fixture(); d['stock_main'][field] = value; d['stock_manifest'][field] = value
            r = artifact(snapshot(d))
            self.assertEqual(r['health_checks']['stock_main']['status'], 'UNKNOWN')
            self.assertEqual(r['stock_daily_review']['status'], 'UNKNOWN')

    def test_changed_capital_policy_and_failed_watchdog_block_candidates(self):
        d = fixture(); d['rules']['lanes']['V2']['capital_pool_usdt'] = 999999
        self.assertEqual(artifact(snapshot(d))['notification_candidates']['status'], 'UNKNOWN')
        d = fixture(); d['hunter_watchdog']['status'] = 'FAILED'
        self.assertEqual(artifact(snapshot(d))['notification_candidates']['status'], 'UNKNOWN')

    def test_weekend_age_budget_is_not_a_proven_missed_schedule(self):
        r = artifact(snapshot(fixture()))
        self.assertEqual(r['health_checks']['stock_monitor']['status'], 'PARTIAL')
        self.assertEqual(r['health_checks']['stock_monitor']['readback_status'], 'UNVERIFIED_MANIFEST_ONLY')
        self.assertEqual(r['health_checks']['stock_monitor']['scheduled_run_overdue'], 'UNKNOWN')
        self.assertEqual(r['health_checks']['stock_monitor']['calendar_coverage'], 'UNVERIFIED')
        self.assertNotIn('overdue', r['health_checks']['stock_monitor'])

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

    def test_vietnam_0823_reports_previous_calendar_day_including_sunday(self):
        # Original requirement: Oct 5 08:23 reports Sunday Oct 4, not Friday's
        # US exchange session. Check both modern UTC+7 zone names without
        # asserting which TZID the existing scheduler stores.
        d = fixture()
        times = ['2026-10-02T18:00:00Z', '2026-10-03T16:59:59.999999Z',
                 '2026-10-03T17:00:00Z', '2026-10-04T16:59:59.999999Z',
                 '2026-10-04T17:00:00Z']
        d['stock_trades'] = [dict(type='BUY', symbol='TEST', at=at, price=2,
                                  notional=1000) for at in times]
        for zone in ('Asia/Ho_Chi_Minh', 'Asia/Bangkok'):
            with self.subTest(zone=zone):
                r = daily_review(snapshot(d), '2026-10-04T00:00:00+07:00',
                                 '2026-10-05T00:00:00+07:00', zone)
                self.assertEqual(r['trade_counts'], {'BUY': 2, 'ADD': 0, 'SELL': 0})
                self.assertEqual(r['start_inclusive'], '2026-10-04T00:00:00+07:00')
                self.assertEqual(r['end_exclusive'], '2026-10-05T00:00:00+07:00')

    def test_capital_breach_and_future_event_are_reported(self):
        d = fixture()
        d['portfolio']['open_positions'] = [{'shadow_id': 'p1', 'tranches': [{'notional_usdt': 18000}]}]
        sync_summary(d)
        r = artifact(snapshot(d))
        self.assertEqual(r['hunter_ledger']['status'], 'PARTIAL')
        self.assertTrue(r['hunter_ledger']['capital_limit_breached'])
        self.assertEqual(r['notification_candidates']['status'], 'UNKNOWN')
        d['portfolio']['events'][0]['at'] = '2026-10-10T13:02:00Z'
        self.assertEqual(artifact(snapshot(d))['notification_candidates']['status'], 'UNKNOWN')
        d = fixture(); d['hunter_watchdog']['fast_watch_health'] = {'status': 'PARTIAL_FAST_PATH_DEGRADED'}
        self.assertEqual(artifact(snapshot(d))['health_checks']['hunter_watchdog']['status'], 'PARTIAL')

    def test_missing_ack_future_stock_and_later_bootstrap_fail_closed(self):
        d = fixture(); del d['cursor']['acknowledged_event_ids']
        self.assertEqual(artifact(snapshot(d))['notification_candidates']['status'], 'UNKNOWN')
        d = fixture(); d['stock_portfolio']['updated_at'] = '2026-10-11T00:00:00Z'
        self.assertEqual(artifact(snapshot(d))['stock_daily_review']['status'], 'UNKNOWN')
        d = fixture(); d['cursor']['bootstrap_since_utc'] = '2026-10-11T00:00:00Z'
        self.assertEqual(candidate_batch(snapshot(d))['events'], [])

    def test_reader_ignores_dirty_checkout_and_moving_head(self):
        with tempfile.TemporaryDirectory() as temp:
            def git(*args):
                return subprocess.run(['git', '-C', temp, *args], check=True, capture_output=True).stdout.decode().strip()
            git('init'); git('config', 'user.name', 'Offline test'); git('config', 'user.email', 'test@example.invalid')
            git('commit', '--allow-empty', '-m', 'source baseline')
            source = git('rev-parse', 'HEAD')
            docs = fixture()
            for doc in docs.values():
                if isinstance(doc, dict):
                    for field in ('source_commit', 'source_head_sha', 'main_readback_head_sha', 'state_revision'):
                        if field in doc:
                            doc[field] = source
            s = snapshot(docs)
            for key, raw in s.raw.items():
                path = Path(temp) / PATHS[key]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw)
            git('add', '.'); git('commit', '-m', 'fixture')
            sha = git('rev-parse', 'HEAD')
            before = read_snapshot(temp, sha)
            self.assertEqual(len(artifact(before)['notification_candidates']['events']), 1)
            path = Path(temp) / PATHS['cursor']; path.write_text('{}')
            git('add', '.'); git('commit', '-m', 'new cursor')
            path.write_text('invalid dirty json')
            self.assertEqual(read_snapshot(temp, sha).raw, before.raw)
            self.assertEqual(path.read_text(), 'invalid dirty json')
            with self.assertRaises(ValueError): read_snapshot(temp, 'main')


if __name__ == '__main__':
    unittest.main()
