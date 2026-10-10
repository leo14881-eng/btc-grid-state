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
    def __init__(self, sha, raw):
        if not re.fullmatch('[0-9a-f]{40}', sha):
            raise ValueError('FULL_SOURCE_SHA_REQUIRED')
        self.sha, self.raw = sha, raw

    def doc(self, key):
        return json.loads(self.raw[key])

    def hashes(self):
        return {PATHS[k]: sha256(v) for k, v in sorted(self.raw.items())}


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def hunter_coherence(s):
    p, summary, monitor, scheduler = (s.doc(k) for k in
                                      ('portfolio', 'summary', 'hunter_monitor', 'scheduler'))
    validate_portfolio(p)
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
    require(instant(scheduler['monitor_started_at_utc']) <= observed <=
            instant(scheduler['monitor_completed_at_utc']) <= instant(monitor['completed_at_utc']),
            'OBSERVATION_OUTSIDE_MONITOR')
    return p


def candidate_batch(s):
    p = hunter_coherence(s)
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
    total = strategic = 0
    for position in p['open_positions']:
        require(isinstance(position.get('tranches'), list) and position['tranches'], 'TRANCHES_UNKNOWN')
        for tranche in position['tranches']:
            amount = number(tranche.get('notional_usdt'), 'notional', positive=True)
            reserve = number(tranche.get('strategic_notional_usdt', 0), 'strategic_notional')
            require(0 <= reserve <= amount, 'RESERVE_ALLOCATION_INVALID')
            total += amount
            strategic += reserve
    ordinary = total - strategic
    breached = total > 20000 + 1e-9 or ordinary > 17000 + 1e-9 or strategic > 3000 + 1e-9
    return {'status': 'PARTIAL' if breached else 'COMPLETE',
            'event_count': len(verified_events(p)), 'capital_limit_breached': breached,
            'total_used_usdt': total, 'ordinary_used_usdt': ordinary, 'reserve_used_usdt': strategic,
            'unchanged_limits_usdt': {'total': 20000, 'ordinary': 17000, 'reserve': 3000},
            'risk_scope': 'LEDGER_SCHEMA_EVENT_INTEGRITY_AND_CAPITAL_ONLY'}


def stock_book(s):
    p, trades, manifest = (s.doc(k) for k in ('stock_portfolio', 'stock_trades', 'stock_manifest'))
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
            'schedule_status': 'UNCONFIGURED_0823_TIMEZONE_AND_WINDOW_REQUIRE_CONFIRMATION',
            'trade_counts': {t: sum(e['type'] == t for e in selected) for t in ('BUY', 'ADD', 'SELL')},
            'closed_in_interval': len(closed), 'wins': interval['wins'], 'losses': interval['losses'],
            'realized_net_pnl_usdt': interval['realized_net_pnl_usdt'],
            'current_snapshot_valuation': current,
            'valuation_note': 'SNAPSHOT_VALUE_IS_NOT_HISTORICAL_INTERVAL_END_VALUE'}


def freshness(doc, now, max_age, stock=False):
    require(type(max_age) in (int, float) and 0 < max_age < float('inf'), 'FRESHNESS_BUDGET_REQUIRED')
    at = instant(doc['created_at'] if stock else doc['completed_at_utc'])
    age = (now - at).total_seconds()
    require(age >= 0, 'FUTURE_HEALTH_TIMESTAMP')
    expected = 'RUN_COMPLETED' if stock else 'SUCCESS'
    status = 'COMPLETE' if age <= max_age and doc.get('status') == expected else 'PARTIAL'
    degraded = doc.get('fast_watch_health', {}).get('status', '')
    if degraded.startswith('PARTIAL'):
        status = 'PARTIAL'
    return {'status': status, 'reported_status': doc.get('status', 'UNKNOWN'),
            'age_seconds': age, 'max_age_seconds': max_age, 'overdue': age > max_age,
            'reported_fast_watch_health': degraded or 'UNKNOWN',
            'scope': 'PUBLISHED_RECEIPT_ONLY_NOT_LIVE_SERVICE_VERIFICATION'}


def guarded(fn):
    try:
        return fn()
    except (ValueError, TypeError, KeyError, RuntimeError, AttributeError) as exc:
        return {'status': 'UNKNOWN', 'reason': str(exc), 'error_type': type(exc).__name__}


def build(s, as_of, budgets, start, end, timezone):
    now = instant(as_of)
    checks = {name: guarded(lambda name=name: freshness(s.doc(name), now, budgets.get(name),
                                                         name.startswith('stock_'))) for name in JOBS}
    stock = guarded(lambda: stock_book(s)[2])
    hunter = guarded(lambda: hunter_ledger(s))
    candidates = guarded(lambda: candidate_batch(s))
    # Stale/unknown monitor receipts cannot authorize a new notification candidate.
    if checks['hunter_monitor']['status'] != 'COMPLETE':
        candidates = {'status': 'UNKNOWN', 'reason': 'MONITOR_FRESHNESS_UNVERIFIED'}
    daily = guarded(lambda: daily_review(s, start, end, timezone))
    if instant(end) > now:
        daily = {'status': 'UNKNOWN', 'reason': 'REPORT_END_AFTER_AS_OF'}
    elif 'stock_portfolio' in s.raw:
        clock = guarded(lambda: {'future': instant(s.doc('stock_portfolio')['updated_at']) > now})
        if clock.get('future') is True or clock.get('status') == 'UNKNOWN':
            stock = daily = {'status': 'UNKNOWN', 'reason': 'STOCK_SNAPSHOT_CLOCK_UNVERIFIED'}
    result = {'schema': 'shadow_readonly_artifacts_v1', 'source_main_sha': s.sha,
              'as_of_utc': now.isoformat(), 'source_file_sha256': s.hashes(),
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
    return Snapshot(sha, raw)


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
