"""Pinned, offline shadow observations. stdout only; no delivery/state authority."""
import argparse
import datetime as dt
import hashlib
import json
import re
import subprocess
from zoneinfo import ZoneInfo

from scripts.hunter_trade_notifications import digest, make_batch, verified_events
from research.hunter_portfolio_integrity import validate_portfolio
from research.stock_shadow.state_safety import number, portfolio_statistics

H = 'research/results/'
S = H + 'stock-shadow/'
PATHS = {
    'portfolio': H + 'hunter-shadow-v2-portfolio.json',
    'summary': H + 'hunter-shadow-v2-summary.json',
    'rules': H + 'hunter-shadow-rules.json',
    'scheduler': H + 'hunter-scheduler-health.json',
    'cursor': '.github/hunter-notification-runtime.json',
    'stock_portfolio': S + 'portfolio-v1.json',
    'stock_trades': S + 'trades-v1.json',
    'stock_manifest': S + 'runtime-main-v1.json',
}
JOBS = {**{'hunter_' + job: H + 'hunter-runtime-' + job + '-health.json'
           for job in ('discovery', 'research', 'monitor', 'watchdog', 'blind-replay', 'missed-replay')},
        **{'stock_' + job: S + 'runtime-' + job + '-v1.json'
           for job in ('main', 'monitor', 'replay')}}
PATHS.update(JOBS)


def instant(value):
    value = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if value.tzinfo is None:
        raise ValueError('TIMEZONE_REQUIRED')
    return value.astimezone(dt.timezone.utc)


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


class Snapshot:
    """Bytes must be read from one immutable commit, never a working tree."""
    def __init__(self, sha, raw, ancestors=None):
        if not re.fullmatch('[0-9a-f]{40}', sha):
            raise ValueError('FULL_SOURCE_SHA_REQUIRED')
        self.sha, self.raw = sha, raw
        self.ancestors = ancestors or {}

    def doc(self, key):
        return json.loads(self.raw[key])

    def hashes(self):
        return {PATHS[k]: sha256(v) for k, v in sorted(self.raw.items())}


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def receipt_integrity(s, name, doc=None):
    doc = s.doc(name) if doc is None else doc
    stock = name.startswith('stock_')
    require(doc.get('job') == name.split('_', 1)[1], 'RECEIPT_JOB_MISMATCH')
    if stock:
        require(doc.get('schema') == 'stock_shadow_generation_v1' and
                doc.get('shadow_only') is True and doc.get('real_orders') is False and
                doc.get('admitted') is True and doc.get('owner') == 'server' and
                type(doc.get('epoch')) is int and doc['epoch'] > 0 and
                isinstance(doc.get('run_id'), str) and bool(doc['run_id']) and
                isinstance(doc.get('generation'), str) and bool(doc['generation']),
                'STOCK_RECEIPT_SAFETY_OR_IDENTITY_UNKNOWN')
        refs = [doc.get('source_commit')]
    else:
        require(doc.get('schema') == 'hunter_runtime_job_health_v1' and
                doc.get('source') == 'VULTR_SYSTEMD' and
                doc.get('real_trading_enabled') is False and
                doc.get('capital_authority') == 'NONE_SHADOW_ONLY', 'RECEIPT_SHADOW_BOUNDARY_UNKNOWN')
        require(doc.get('main_readback_verified') is True, 'RECEIPT_READBACK_UNVERIFIED')
        source = (s.doc('scheduler').get('state_revision') if name == 'hunter_monitor'
                  else doc.get('source_head_sha'))
        refs = [source, doc.get('main_readback_head_sha')]
        if name != 'hunter_monitor':
            require(instant(doc['started_at_utc']) <= instant(doc['completed_at_utc']),
                    'RECEIPT_CLOCK_ORDER_INVALID')
    for ref in refs:
        require(isinstance(ref, str) and re.fullmatch('[0-9a-f]{40}', ref), 'RECEIPT_SHA_UNKNOWN')
        require(s.ancestors.get(ref) is True, 'RECEIPT_ANCESTRY_UNVERIFIED')
    if not stock:
        require(refs[0] == refs[1] or s.ancestors.get(refs[0] + '..' + refs[1]) is True,
                'RECEIPT_SOURCE_READBACK_ORDER_UNVERIFIED')
    return doc


def reconcile_hunter_accounting(p, summary):
    archive = p.get('closed_trade_archive', [])
    require(isinstance(archive, list), 'CLOSED_ARCHIVE_UNKNOWN')
    identities = set()
    for row in p['open_positions'] + p['closed_positions'] + archive:
        identity = row.get('shadow_id')
        require(isinstance(identity, str) and bool(identity), 'POSITION_IDENTITY_UNKNOWN')
        require(identity not in identities, 'DUPLICATE_SHADOW_ID')
        identities.add(identity)
    counts = {'open_positions': len(p['open_positions']), 'closed_positions': len(p['closed_positions']),
              'archived_closed_positions': len(archive),
              'total_closed_positions': len(p['closed_positions']) + len(archive)}
    for key, expected in counts.items():
        require(type(summary.get(key)) is int and summary[key] == expected, 'SUMMARY_COUNT_MISMATCH:' + key)
    require(summary.get('schema') == 'hunter_shadow_v2_summary_v3' and
            summary.get('mode') == 'SIMULATION_ONLY_NO_REAL_ORDERS' and
            summary.get('capital_authority') == 'NONE_SHADOW_ONLY', 'SUMMARY_SHADOW_BOUNDARY_UNKNOWN')
    realized = sum(number(row.get('net_pnl_usdt'), 'closed.net_pnl_usdt')
                   for row in p['closed_positions'] + archive)
    total = strategic = 0
    for row in p['open_positions']:
        require(isinstance(row.get('tranches'), list) and bool(row['tranches']), 'TRANCHES_UNKNOWN')
        for tranche in row['tranches']:
            amount = number(tranche.get('notional_usdt'), 'notional', positive=True)
            reserve = number(tranche.get('strategic_notional_usdt', 0), 'strategic_notional')
            require(0 <= reserve <= amount, 'RESERVE_ALLOCATION_INVALID')
            total += amount
            strategic += reserve
    def equal_amount(doc, key, expected):
        require(abs(number(doc.get(key), key) - round(expected, 2)) <= 1e-8,
                'SUMMARY_AMOUNT_MISMATCH:' + key)
    for key in ('net_pnl_usdt', 'realized_net_pnl_usdt'):
        equal_amount(summary, key, realized)
    ordinary = total - strategic
    capital = summary['policy']['capital_management']
    amounts = {'used_capital_usdt': total, 'ordinary_used': ordinary,
               'strategic_reserve_used': strategic, 'ordinary_available': max(0, 17000 - ordinary),
               'strategic_reserve_available': max(0, 3000 - strategic),
               'capital_pool': 20000, 'ordinary_opportunity_cap': 17000, 'strategic_reserve': 3000,
               'initial_capital_usdt': 20000, 'realized_net_pnl_usdt': realized,
               'equity_usdt': max(0, 20000 + realized), 'total_cash_usdt': max(0, 20000 + realized - total)}
    for key, expected in amounts.items():
        equal_amount(capital, key, expected)
    equal_amount(summary['policy'], 'capital_pool_usdt', 20000)
    return total, strategic


def hunter_coherence(s):
    p, summary, monitor, scheduler = (s.doc(k) for k in
                                      ('portfolio', 'summary', 'hunter_monitor', 'scheduler'))
    validate_portfolio(p)
    require(s.doc('rules')['lanes']['V2']['capital_pool_usdt'] == 20000, 'CAPITAL_POLICY_CHANGED')
    reconcile_hunter_accounting(p, summary)
    receipt_integrity(s, 'hunter_monitor')
    generation = p.get('last_cycle_generation_id')
    require(generation and generation == summary.get('generation_id'), 'PORTFOLIO_SUMMARY_GENERATION')
    require(p.get('updated_at_utc') == summary.get('as_of_utc'), 'PORTFOLIO_SUMMARY_TIMESTAMP')
    generation = monitor.get('monitor_generation_id')
    require(generation and generation == scheduler.get('current_generation_id') ==
            scheduler.get('last_successful_monitor_generation_id'), 'MONITOR_SCHEDULER_GENERATION')
    require(monitor.get('scheduler_health_sha256') == sha256(s.raw['scheduler']), 'SCHEDULER_HASH')
    require(monitor.get('status') == 'SUCCESS' and monitor.get('main_readback_verified') is True,
            'MONITOR_READBACK_UNKNOWN')
    require(monitor.get('real_trading_enabled') is False and
            monitor.get('capital_authority') == 'NONE_SHADOW_ONLY' and
            scheduler.get('shadow_only') is True and scheduler.get('real_order_count') == 0,
            'SHADOW_BOUNDARY_UNKNOWN')
    # Monitor bucket IDs and portfolio observation IDs are different namespaces.
    # Their ordering proves a bounded observation, not an invented equality.
    observed = instant(p['updated_at_utc'])
    for event in verified_events(p).values():
        require(instant(event['at']) <= observed, 'EVENT_AFTER_PORTFOLIO')
    require(instant(scheduler['monitor_started_at_utc']) <= observed <=
            instant(scheduler['monitor_completed_at_utc']) <= instant(monitor['completed_at_utc']),
            'OBSERVATION_OUTSIDE_MONITOR')
    return p


def candidate_batch(s):
    p = hunter_coherence(s)
    require(hunter_ledger(s)['status'] == 'COMPLETE', 'HUNTER_LEDGER_NOT_COMPLETE')
    for name in JOBS:
        if name.startswith('hunter_'):
            require(receipt_integrity(s, name).get('status') == 'SUCCESS', 'HUNTER_JOB_NOT_SUCCESSFUL')
    c = s.doc('cursor')
    require(c.get('schema') == 'hunter_notification_runtime_v1' and
            c.get('notification_policy') == 'V2_NEW_TRADE_EVENTS_ONLY' and
            c.get('writer') == 'CHATGPT_NOTIFICATION_CONSUMER_ONLY' and
            c.get('real_trading_enabled') is False and
            c.get('capital_authority') == 'NONE_SHADOW_ONLY', 'CURSOR_AUTHORITY_UNKNOWN')
    excluded = set()
    hashes = {}
    def ids(value):
        require(isinstance(value, list) and all(isinstance(x, str) and
                re.fullmatch('[0-9a-f]{64}', x) for x in value), 'RESERVATIONS_INVALID')
        excluded.update(value)
    def content_hashes(value):
        require(isinstance(value, dict), 'RESERVATION_HASHES_INVALID')
        for identity, content in value.items():
            require(re.fullmatch('[0-9a-f]{64}', identity) and isinstance(content, str) and
                    re.fullmatch('[0-9a-f]{64}', content), 'RESERVATION_HASHES_INVALID')
            require(identity not in hashes or hashes[identity] == content, 'RESERVATION_HASH_CONFLICT')
            hashes[identity] = content
            excluded.add(identity)
    ids(c['acknowledged_event_ids'])
    for key in ('reserved_event_ids', 'pending_event_ids'):
        ids(c.get(key, []))
    for key in ('reserved_event_content_hashes', 'pending_event_content_hashes'):
        content_hashes(c.get(key, {}))
    unresolved = c.get('unresolved_batches', [])
    require(isinstance(unresolved, list), 'UNRESOLVED_BATCHES_INVALID')
    for batch in unresolved:
        require(isinstance(batch, dict) and bool(batch.get('event_ids')), 'UNRESOLVED_IDS_UNKNOWN')
        ids(batch['event_ids'])
        content_hashes(batch.get('event_content_hashes', {}))
    require(not c.get('pending_batch_id') or bool(c.get('pending_event_ids')), 'PENDING_IDS_UNKNOWN')
    events = verified_events(p)
    for identity, event in events.items():
        require(instant(event['at']) <= instant(p['updated_at_utc']), 'EVENT_AFTER_PORTFOLIO')
        if identity in hashes:
            require(digest(event) == hashes[identity], 'RESERVED_EVENT_CONTENT_CHANGED')
    cutoffs = [c[k] for k in ('event_not_before_utc', 'bootstrap_since_utc') if c.get(k)]
    require(bool(cutoffs), 'NOTIFICATION_CUTOFF_REQUIRED')
    cutoff = max(cutoffs, key=instant)
    result = make_batch(p, excluded, s.sha, after=cutoff)
    result.update(delivery_status='CANDIDATE_ONLY_NOT_RESERVED_NOT_DELIVERED',
                  cursor_sha256=sha256(s.raw['cursor']),
                  consumer_requirement='REVALIDATE_CURRENT_SOURCE_AND_CURSOR_THEN_SINGLE_WRITER_CAS',
                  excluded_event_count=len(excluded))
    return result


def hunter_ledger(s):
    p = hunter_coherence(s)
    require(s.doc('rules')['lanes']['V2']['capital_pool_usdt'] == 20000, 'CAPITAL_POLICY_CHANGED')
    total, strategic = reconcile_hunter_accounting(p, s.doc('summary'))
    ordinary = total - strategic
    breached = total > 20000 + 1e-9 or ordinary > 17000 + 1e-9 or strategic > 3000 + 1e-9
    return {'status': 'PARTIAL' if breached else 'COMPLETE',
            'event_count': len(verified_events(p)), 'capital_limit_breached': breached,
            'total_used_usdt': total, 'ordinary_used_usdt': ordinary, 'reserve_used_usdt': strategic,
            'unchanged_limits_usdt': {'total': 20000, 'ordinary': 17000, 'reserve': 3000},
            'valuation_and_strategy_risk_reconciliation': 'UNVERIFIED',
            'risk_scope': 'LEDGER_SCHEMA_EVENT_INTEGRITY_AND_CAPITAL_ONLY'}


def stock_book(s):
    p, trades, manifest = (s.doc(k) for k in ('stock_portfolio', 'stock_trades', 'stock_manifest'))
    receipt_integrity(s, 'stock_main', manifest)
    require(p.get('simulation_only') is True and manifest.get('real_orders') is False,
            'STOCK_SHADOW_BOUNDARY_UNKNOWN')
    require(manifest.get('status') == 'RUN_COMPLETED' and manifest.get('admitted') is True,
            'STOCK_CURRENT_RUN_NOT_COMPLETED')
    require(p.get('run_id') and p['run_id'] == manifest.get('run_id') and
            p.get('source_commit') and p['source_commit'] == manifest.get('source_commit'),
            'STOCK_GENERATION_MISMATCH')
    for key in ('stock_portfolio', 'stock_trades'):
        require(manifest.get('files', {}).get(PATHS[key]) == sha256(s.raw[key]), 'STOCK_MANIFEST_HASH')
    return p, trades, portfolio_statistics(p, trades)


def daily_review(s, start, end, timezone):
    zone = ZoneInfo(timezone)  # User-specified reporting zone; no 08:23 assumption.
    begin, finish = instant(start), instant(end)
    require(begin < finish, 'REPORT_INTERVAL_INVALID')
    p, trades, current = stock_book(s)
    coverage = instant(p['updated_at']) >= finish
    events = {}
    for e in trades:
        if e.get('type') not in ('BUY', 'ADD', 'SELL'):
            continue
        require(isinstance(e.get('symbol'), str) and e['symbol'], 'STOCK_EVENT_IDENTITY')
        at = instant(e['at'])
        identity = (e['type'], at.isoformat(), e['symbol'])
        require(identity not in events or events[identity] == e, 'STOCK_EVENT_CONTENT_CONFLICT')
        events[identity] = e
    selected = [e for e in events.values() if begin <= instant(e['at']) < finish]
    closed = [p0 for p0 in p['closed'] if begin <= instant(p0['closed_at']) < finish]
    closed_ids = [(p0.get('symbol'), p0.get('opened_at'), p0['closed_at']) for p0 in closed]
    require(len(set(closed_ids)) == len(closed_ids), 'DUPLICATE_CLOSED_POSITION')
    interval = portfolio_statistics(dict(p, positions={}, closed=closed), selected)
    return {'status': 'PARTIAL' if not coverage or current['valuation_coverage_status'] == 'PARTIAL' else 'COMPLETE',
            'interval_watermark_reached': coverage,
            'timezone': timezone, 'start_inclusive': begin.astimezone(zone).isoformat(),
            'end_exclusive': finish.astimezone(zone).isoformat(),
            'schedule_status': 'NOT_DEPLOYED',
            'historical_daily_contract': '0823_ASIA_BANGKOK_PREVIOUS_LOCAL_CALENDAR_DAY',
            'trade_counts': {t: sum(e['type'] == t for e in selected) for t in ('BUY', 'ADD', 'SELL')},
            'closed_in_interval': len(closed), 'wins': interval['wins'], 'losses': interval['losses'],
            'realized_net_pnl_usdt': interval['realized_net_pnl_usdt'],
            'current_snapshot_valuation': current,
            'valuation_note': 'SNAPSHOT_VALUE_IS_NOT_HISTORICAL_INTERVAL_END_VALUE'}


def freshness(s, name, now, max_age):
    doc = receipt_integrity(s, name)
    stock = name.startswith('stock_')
    require(type(max_age) in (int, float) and 0 < max_age < float('inf'), 'FRESHNESS_BUDGET_REQUIRED')
    at = instant(doc['created_at'] if stock else doc['completed_at_utc'])
    age = (now - at).total_seconds()
    require(age >= 0, 'FUTURE_HEALTH_TIMESTAMP')
    expected = 'RUN_COMPLETED' if stock else 'SUCCESS'
    status = 'COMPLETE' if age <= max_age and doc.get('status') == expected else 'PARTIAL'
    degraded = doc.get('fast_watch_health', {}).get('status', '')
    if degraded.startswith('PARTIAL'):
        status = 'PARTIAL'
    if stock:
        status = 'PARTIAL'  # Published manifest does not contain final readback receipt.
    return {'status': status, 'reported_status': doc.get('status', 'UNKNOWN'),
            'age_seconds': age, 'max_age_seconds': max_age, 'age_budget_exceeded': age > max_age,
            'calendar_coverage': 'UNVERIFIED', 'scheduled_run_overdue': 'UNKNOWN',
            'readback_status': 'UNVERIFIED_MANIFEST_ONLY' if stock else 'VERIFIED_RECEIPT_AND_ANCESTRY',
            'reported_fast_watch_health': degraded or 'UNKNOWN',
            'scope': 'PUBLISHED_RECEIPT_ONLY_NOT_LIVE_SERVICE_VERIFICATION'}


def guarded(fn):
    try:
        return fn()
    except (ValueError, TypeError, KeyError, RuntimeError, AttributeError) as exc:
        return {'status': 'UNKNOWN', 'reason': str(exc), 'error_type': type(exc).__name__}


def build(s, as_of, budgets, start, end, timezone):
    now = instant(as_of)
    checks = {name: guarded(lambda name=name: freshness(s, name, now, budgets.get(name))) for name in JOBS}
    stock = guarded(lambda: stock_book(s)[2])
    hunter = guarded(lambda: hunter_ledger(s))
    candidates = guarded(lambda: candidate_batch(s))
    # Stale/unknown monitor receipts cannot authorize a new notification candidate.
    if any(checks[name]['status'] != 'COMPLETE' for name in
           ('hunter_monitor', 'hunter_discovery', 'hunter_research')):
        candidates = {'status': 'UNKNOWN', 'reason': 'CORE_HUNTER_FRESHNESS_UNVERIFIED'}
    daily = guarded(lambda: daily_review(s, start, end, timezone))
    if instant(end) > now:
        daily = {'status': 'UNKNOWN', 'reason': 'REPORT_END_AFTER_AS_OF'}
    elif 'stock_portfolio' in s.raw:
        clock = guarded(lambda: {'future': instant(s.doc('stock_portfolio')['updated_at']) > now})
        if clock.get('future') is True or clock.get('status') == 'UNKNOWN':
            stock = daily = {'status': 'UNKNOWN', 'reason': 'STOCK_SNAPSHOT_CLOCK_UNVERIFIED'}
    result = {'schema': 'shadow_readonly_artifacts_v1', 'source_main_sha': s.sha,
              'as_of_utc': now.isoformat(), 'source_file_sha256': s.hashes(),
              'source_ancestry': s.ancestors,
              'shadow_only': True, 'real_trading_enabled': False, 'capital_authority': 'NONE_SHADOW_ONLY',
              'status': 'PARTIAL', 'coverage_note': 'NO_LIVE_DEPLOYMENT_OR_FULL_STRATEGY_RISK_PROOF',
              'health_checks': checks, 'hunter_ledger': hunter, 'stock_ledger': stock,
              'notification_candidates': candidates, 'stock_daily_review': daily}
    result['artifact_id'] = digest(result)
    return result


def read_snapshot(repo, sha):
    require(bool(re.fullmatch('[0-9a-f]{40}', sha)), 'FULL_SOURCE_SHA_REQUIRED')
    def git(*args):
        return subprocess.run(['git', '-C', repo, *args], capture_output=True, check=False)
    resolved = git('rev-parse', '--verify', sha + '^{commit}')
    require(resolved.returncode == 0 and resolved.stdout.decode().strip() == sha, 'COMMIT_NOT_AVAILABLE')
    raw = {}
    for key, path in PATHS.items():
        response = git('show', sha + ':' + path)
        if response.returncode == 0:
            raw[key] = response.stdout
    refs, pairs = set(), set()
    for key in (*JOBS, 'scheduler'):
        try:
            doc = json.loads(raw[key])
            for field in ('source_head_sha', 'main_readback_head_sha', 'source_commit', 'state_revision'):
                ref = doc.get(field)
                if isinstance(ref, str) and re.fullmatch('[0-9a-f]{40}', ref):
                    refs.add(ref)
            if key.startswith('hunter_'):
                source = (json.loads(raw['scheduler']).get('state_revision') if key == 'hunter_monitor'
                          else doc.get('source_head_sha'))
                readback = doc.get('main_readback_head_sha')
                if all(isinstance(x, str) and re.fullmatch('[0-9a-f]{40}', x) for x in (source, readback)):
                    pairs.add((source, readback))
        except (KeyError, ValueError, TypeError, AttributeError):
            continue
    ancestors = {ref: git('merge-base', '--is-ancestor', ref, sha).returncode == 0 for ref in sorted(refs)}
    ancestors.update({a + '..' + b: git('merge-base', '--is-ancestor', a, b).returncode == 0
                      for a, b in sorted(pairs)})
    return Snapshot(sha, raw, ancestors)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', default='.')
    parser.add_argument('--source-sha', required=True)
    parser.add_argument('--as-of', required=True)
    parser.add_argument('--freshness-budgets-json', required=True,
                        help='Explicit JSON mapping of job names to maximum age in seconds')
    parser.add_argument('--report-start', required=True)
    parser.add_argument('--report-end', required=True)
    parser.add_argument('--report-timezone', required=True)
    args = parser.parse_args()
    result = build(read_snapshot(args.repo, args.source_sha), args.as_of,
                   json.loads(args.freshness_budgets_json), args.report_start,
                   args.report_end, args.report_timezone)
    print(json.dumps(result, sort_keys=True, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
