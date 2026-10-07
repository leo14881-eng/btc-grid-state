"""Authoritative Monitor observation of Fast Watch subscriptions; no mutation."""
from research.hunter_fast_watch import DualWatch, bind_observed_model_routes
from scripts.hunter_ops_health import read_health


def reconcile_view(portfolio, monitor, source_sha, now, health):
    routed, admitted = bind_observed_model_routes(portfolio, monitor, source_sha, now.timestamp())
    dual = DualWatch()
    dual.reconcile(routed, source_sha, now.timestamp())
    result = dict(schema='hunter_fast_subscription_reconciliation_v1',
                  monitor_generation_id=portfolio.get('active_observation_generation_id'),
                  monitor_source_sha=source_sha, runtime_source_sha=health.get('source_sha'),
                  runtime_observed_at=health.get('generated_at'),
                  formal_portfolio_mutated=False, formal_writer=False,
                  reconciliation_owner='HUNTER_MARKET_STREAM_RUNTIME',
                  model_route_admitted_ids=admitted, unroutable_positions=dual.unroutable,
                  venues={})
    for name, watch in dual.watches.items():
        expected=set(watch.symbols)
        actual=set(health.get('venues', {}).get(name, {}).get('actual_subscriptions', []))
        result['venues'][name]=dict(expected_symbols=sorted(expected),
                                   actual_subscriptions=sorted(actual),
                                   missing_symbols=sorted(expected-actual),
                                   extra_symbols=sorted(actual-expected))
    result['status']=('PRIMARY_VENUE_IDENTITY_MISSING' if dual.unroutable else
                      'RECONCILE_REQUIRED' if any(v['missing_symbols'] or v['extra_symbols']
                                                   for v in result['venues'].values()) else 'MATCHED')
    return result


def observe(portfolio, monitor, source_sha, now):
    try:
        return reconcile_view(portfolio, monitor, source_sha, now, read_health(now=now))
    except (OSError, ValueError, KeyError, TypeError):
        return dict(status='FAST_WATCH_HEALTH_UNAVAILABLE', formal_portfolio_mutated=False,
                    authoritative_monitor_affected=False)
