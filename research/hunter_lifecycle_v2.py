"""V2-only decision receipts and evidence-gated rebound risk review.

No order API, clock/price stop, historical fill reconstruction or writer here.
The caller remains the normal Single Writer lifecycle.
"""
import copy
import datetime as dt
import hashlib
import json
import math
import pathlib
from functools import lru_cache

try:
    from research.hunter_lifecycle_state import fresh
    from research.hunter_policy import C
except ModuleNotFoundError as exc:
    if exc.name != 'research':
        raise
    from hunter_lifecycle_state import fresh
    from hunter_policy import C


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def candle_receipt(symbol, rows, fetched_at):
    """Keep actual Binance rows; score_row's missing-data defaults are not evidence."""
    return dict(schema='hunter_v2_candles_v1', exchange='binance', market='spot',
                symbol=symbol, interval='15m', fetched_at=fetched_at.isoformat(),
                source='/api/v3/klines', rows=copy.deepcopy(rows))


def entry_thesis_known(pos):
    original = pos.get('entry_thesis_evidence') or {}
    provenance = original.get('historical_provenance') or {}
    recorded = (provenance.get('kind') == 'DEPLOYED_FIRST_PARENT_MAIN_BUY_DECISION'
                and provenance.get('shadow_id') == pos.get('shadow_id')
                and len(str(provenance.get('commit_sha') or '')) == 40)
    return ((bool((original.get('signal_evidence') or {}).get('evidence_id')) or recorded)
            and all(number(original.get(k)) is not None for k in
                    ('score','independent','btc_rel_1h','btc_rel_4h','rel_accel')))


@lru_cache(maxsize=1)
def legacy_entries():
    return json.loads(pathlib.Path(__file__).with_name('hunter_v2_entry_theses.json').read_text(encoding='utf-8'))['positions']


def hydrate_legacy_entry(pos, entries=None):
    """Attach exact recorded BUY inputs as metadata; never recreate fills/times."""
    if pos.get('entry_thesis_evidence') or not pos.get('tranches'):return
    for row in legacy_entries() if entries is None else entries:
        if row['shadow_id'] != pos.get('shadow_id') or row['asset'] != pos.get('asset'):continue
        if row['opened_at_utc'] != pos.get('opened_at_utc'):return
        first = pos['tranches'][0]
        if any(first.get(k) != value for k,value in row['first_tranche_identity'].items()):return
        pos['entry_thesis_evidence'] = copy.deepcopy(row['evidence'])
        return


def technical(receipt, asset, now):
    try:
        if (receipt.get('schema') != 'hunter_v2_candles_v1'
                or receipt.get('exchange') != 'binance' or receipt.get('market') != 'spot'
                or receipt.get('symbol') != asset + 'USDT' or receipt.get('interval') != '15m'
                or not fresh(receipt.get('fetched_at'), now)):
            raise ValueError('MICRO_RECEIPT_IDENTITY_OR_TIME_INVALID')
        rows = receipt['rows']
        # Only completed bars contribute to exit evidence; an unfinished bar's
        # low volume must never be classified as a weak rebound.
        captured = dt.datetime.fromisoformat(receipt['fetched_at'].replace('Z', '+00:00'))
        rows = [r for r in rows if float(r[6]) < captured.timestamp() * 1000]
        if len(rows) < 24:
            raise ValueError('MICRO_CLOSED_BARS_INCOMPLETE')
        rows = rows[-24:]
        for i, r in enumerate(rows):
            values = [number(r[j]) for j in (0, 1, 2, 3, 4, 6, 7, 10)]
            if any(v is None for v in values):
                raise ValueError('MICRO_NONFINITE')
            opened, op, hi, lo, cl, closed, vol, buy = values
            if not (0 < lo <= min(op, cl) <= max(op, cl) <= hi and vol > 0 and 0 <= buy <= vol
                    and closed - opened == 899999):
                raise ValueError('MICRO_INVALID_BAR')
            if i and opened - float(rows[i-1][0]) != 900000:
                raise ValueError('MICRO_NONCONTIGUOUS')
        age = now.timestamp() - float(rows[-1][6])/1000
        latest_expected_close = int(captured.timestamp() * 1000)//900000*900000-1
        if not 0 <= age <= 1500 or float(rows[-1][6]) != latest_expected_close:
            raise ValueError('MICRO_SOURCE_STALE')
        highs = [float(r[2]) for r in rows]; lows = [float(r[3]) for r in rows]
        closes = [float(r[4]) for r in rows]; volumes = [float(r[7]) for r in rows]
        recent = sum(volumes[-2:]); prior = sum(volumes[-14:-2])/6
        demand = sum(float(r[10]) for r in rows[-2:])/recent
        weak_structure = max(highs[-4:]) < max(highs[-8:-4]) and min(lows[-4:]) < min(lows[-8:-4])
        strong_structure = max(highs[-4:]) > max(highs[-8:-4]) and min(lows[-4:]) > min(lows[-8:-4])
        # A bounce followed by loss of momentum, not a fixed rebound percent.
        rebound = closes[-2] > closes[-3] and closes[-1] < closes[-2] and closes[-1] > min(lows[-8:])
        return dict(status='FRESH', source_closed_at_ms=rows[-1][6],
                    receipt_sha256=hashlib.sha256(json.dumps(receipt, sort_keys=True).encode()).hexdigest(),
                    structure_weak=weak_structure, structure_recovered=strong_structure,
                    rebound_exhaustion=rebound, volume_ratio=recent/prior,
                    taker_buy_quote_ratio=demand, demand_weak=recent < prior and demand < .5,
                    demand_recovered=recent >= prior and demand > .5,
                    support=min(lows[-8:]), resistance=max(highs[-8:]), close=closes[-1])
    except (KeyError, TypeError, ValueError, IndexError, ZeroDivisionError, OverflowError) as ex:
        return dict(status='UNKNOWN', reason=str(ex))


def enrich(evidence, candidate, supply, review, pos, scan, now):
    """Recheck each dimension, preserving UNKNOWN separately from deterioration."""
    e = copy.deepcopy(evidence)
    c = candidate or {}; meta = e.get('signal_evidence') or {}
    source = c.get('v2_lifecycle_evidence') or {}
    receipt = source.get('micro_receipt') or {}
    same_cycle = source.get('generation_id') == scan.get('generation_id')
    tech = technical(receipt, pos['asset'], now) if same_cycle else dict(status='UNKNOWN', reason='MICRO_GENERATION_MISSING_OR_MISMATCH')
    signal_ok = (meta.get('generation_id') == scan.get('generation_id') and bool(meta.get('evidence_id'))
                 and fresh(meta.get('observed_at_utc'), now)
                 and all(number(e.get(k)) is not None for k in ('score', 'independent', 'btc_rel_1h', 'btc_rel_4h', 'rel_accel'))
                 and all(number((c.get('signal') or {}).get(k)) is not None for k in
                         ('score','independent_signal_count','btc_relative_1h_pct','btc_relative_4h_pct','relative_acceleration_pct')))
    sr = (supply.get('assets') or {}).get(pos['asset']) or {}
    supply_ok = fresh(sr.get('verified_at_utc'), now)
    candidate_ok = fresh(review.get('as_of_utc'), now)
    # Refreshing a price signal does not refresh an old fundamental finding.
    e['supply_confirmed_major_risk'] = bool(e.get('supply_confirmed_major_risk') and supply_ok and sr.get('source'))
    if not candidate_ok:
        e['blockers'] = [b for b in e.get('blockers', []) if not any(t in str(b).upper() for t in ('MISMATCH', 'INVALID', 'WRONG_ASSET', 'CONFLICT'))]
    checks = dict(signal='FRESH' if signal_ok else 'UNKNOWN',
                  candidate='FRESH' if candidate_ok else 'UNKNOWN',
                  liquidity='FRESH' if fresh(e.get('book_observed_at_utc'), now) else 'UNKNOWN',
                  structure=tech['status'], volume_demand=tech['status'],
                  fundamental_supply='FRESH' if supply_ok and sr.get('tactical_supply_risk_verified') else 'UNKNOWN',
                  venue='MATCHED' if not pos.get('execution_venue') or pos.get('market_symbol') == pos['asset']+'USDT' else 'UNKNOWN',
                  original_thesis='RECORDED' if entry_thesis_known(pos) else 'LEGACY_ENTRY_EVIDENCE_NOT_RETAINED')
    e['v2_thesis_review'] = dict(generation_id=scan.get('generation_id'), checked_at=now.isoformat(),
        checks=checks, technical=tech, micro_receipt=receipt if same_cycle else None,
        supply=dict(status=sr.get('status'), source=sr.get('source'), verified_at_utc=sr.get('verified_at_utc'),
                    confirmed_major_risk=e['supply_confirmed_major_risk']),
        original_executable_gate=copy.deepcopy(pos.get('executable_gate')),
        entry_thesis_evidence=copy.deepcopy(pos.get('entry_thesis_evidence')))
    e['v2_weak_reasons'] = []
    if tech.get('status') == 'FRESH':
        if tech.get('structure_weak'):e['v2_weak_reasons'].append('OBSERVED_LOWER_HIGHS_AND_LOWS')
        if tech.get('demand_weak'):e['v2_weak_reasons'].append('CLOSED_BAR_VOLUME_AND_BUY_DEMAND_WEAK')
    return e, signal_ok


def rebound_review(pos, e, now, execution, health, generation, minimum_rr=1.5):
    """Conjunctive loss exit; observed risk scenario, not a forecast or stop.

    Support/resistance are observed levels. Reusing the admission RR margin in
    reverse demands materially more downside room than upside, AND weak thesis,
    failed rebound structure/demand, current executable depth and a net loss.
    No missing input can satisfy a condition.
    """
    row = pos.setdefault('loss_recovery_lifecycle', {'transitions': []})
    previous = row.get('rebound_review') or {}
    if previous.get('generation_id') == generation:
        return dict(previous, exit=False, reason='REBOUND_EVIDENCE_ALREADY_CONSUMED')
    review = e.get('v2_thesis_review') or {}; tech = review.get('technical') or {}
    meta = e.get('signal_evidence') or {}
    net = number(execution.get('net_pnl_usdt'))
    r1, r4 = number(e.get('btc_rel_1h')), number(e.get('btc_rel_4h'))
    recovered = (health == 'STRONG' or (r1 is not None and r1 >= 0)
                 or (r4 is not None and r4 >= 0) or tech.get('structure_recovered') is True)
    fresh_cycle = (generation and meta.get('generation_id') == generation
                   and review.get('generation_id') == generation
                   and review.get('checks', {}).get('signal') == 'FRESH'
                   and fresh(meta.get('observed_at_utc'), now) and health != 'EVIDENCE_PENDING')
    known_execution = execution.get('status') == 'SHADOW_RECEIPT_ESTIMATE' and fresh(execution.get('fetched_at'), now)
    spread = number(e.get('spread_bps')); bid = number(e.get('bid_depth_2pct_usdt')); ask = number(e.get('ask_depth_2pct_usdt'))
    liquidity_ok = (known_execution and fresh(e.get('book_observed_at_utc'), now)
                    and review.get('checks', {}).get('liquidity') == 'FRESH'
                    and spread is not None and 0 <= spread <= C['MAX_SPREAD_BPS']
                    and bid is not None and ask is not None and min(bid, ask) >= C['MIN_DEPTH_USDT'])
    comparison = dict(status='UNKNOWN', model='OBSERVED_SUPPORT_RESISTANCE_SCENARIO_NOT_FORECAST')
    risk_higher = False
    if known_execution and tech.get('status') == 'FRESH':
        q = number(execution.get('quantity')); vwap = number(execution.get('vwap')); mid = number(execution.get('book_mid'))
        if q and vwap and mid and tech['support'] < vwap < tech['resistance']:
            downside = q * (vwap - tech['support']); upside = q * (tech['resistance'] - vwap)
            friction = q * (max(0, mid-vwap) + vwap*execution['fee_bps']/10000)
            risk_higher = downside > minimum_rr * max(upside, friction)
            comparison.update(status='OBSERVED', downside_to_support_usdt=downside,
                upside_to_resistance_usdt=upside, immediate_exit_friction_usdt=friction,
                required_risk_reward_margin=minimum_rr, holding_risk_higher=risk_higher)
    conditions = dict(fresh_cycle=bool(fresh_cycle), net_loss=net is not None and net < 0,
        original_thesis_known=entry_thesis_known(pos),
        thesis_still_invalidated=health == 'THESIS_INVALIDATED' and int(pos.get('degraded_cycles') or 0) >= C['DEGRADE_CONFIRM_CYCLES'],
        rebound_window=tech.get('rebound_exhaustion') is True,
        relative_still_weak=r1 is not None and r4 is not None and r1 < 0 and r4 < 0,
        structure_still_weak=tech.get('structure_weak') is True,
        demand_still_weak=tech.get('demand_weak') is True,
        execution_clear=bool(liquidity_ok), holding_risk_higher=risk_higher, no_recovery=not recovered)
    authorized = all(conditions.values())
    old = previous.get('state', 'NONE')
    if fresh_cycle and recovered:
        state = 'CANCELLED_RECOVERY'
    elif fresh_cycle and health in ('WEAKENING', 'DEGRADED', 'THESIS_INVALIDATED') and net is not None and net < 0:
        state = 'REBOUND_EXIT_REVIEW' if tech.get('rebound_exhaustion') is True else 'LOSS_RECOVERY'
        if pos.get('recovery_state', 'NONE') == 'NONE':
            pos['recovery_state'] = 'LOSS_RECOVERY'
    else:
        state = 'EVIDENCE_PENDING' if not fresh_cycle else 'NONE'
    reason = ('REBOUND_RISK_EXIT_ALL_CONDITIONS_CONFIRMED' if authorized else
              'THESIS_RELATIVE_OR_STRUCTURE_RECOVERED_HOLD' if recovered and fresh_cycle else
              'REBOUND_REVIEW_BLOCKED:' + ','.join(k for k, v in conditions.items() if not v))
    result = dict(generation_id=generation, checked_at=now.isoformat(), state=state, exit=authorized,
                  conditions=conditions, risk_comparison=comparison, reason=reason,
                  evidence_status='FRESH' if fresh_cycle else 'UNKNOWN')
    row.update(rebound_review=result, risk_reduction_authorized=authorized)
    if state != old:
        row['review_transitions'] = (row.get('review_transitions', []) + [dict(from_state=old, to=state,
            generation_id=generation, at=now.isoformat(), reason=reason)])[-32:]
    return result


def finish(pos, state, start, scan, now, health='EVIDENCE_PENDING', e=None, protection=None):
    """One final per-position receipt survives bounded decision-history pruning."""
    rows = [d for d in state.get('decisions', [])[start:] if d.get('shadow_id') == pos.get('shadow_id')]
    reasons = list(dict.fromkeys(r for d in rows for r in d.get('reasons', [])))
    if protection:
        if not protection.get('armed') and pos.get('mfe_pct', 0) >= C['PROTECT_ARM_PCT']:
            reasons.append('HISTORICAL_MFE_IS_NOT_LIVE_PROTECTION_ARM')
        if (protection.get('execution') or {}).get('status') != 'SHADOW_RECEIPT_ESTIMATE':
            reasons.append('PROFIT_PROTECTION_EXECUTION_UNKNOWN')
        if protection.get('incident'):
            reasons.append(protection['incident'])
    action = 'SELL_'+pos['exit_reason'] if pos.get('closed_at_utc') else (rows[-1]['action'] if rows else 'HOLD')
    last = pos.get('last_monitor_decision') or {}
    if last.get('checked_at') and now < dt.datetime.fromisoformat(last['checked_at']):
        return
    receipt = dict(schema='hunter_v2_monitor_decision_v1', generation_id=scan.get('generation_id'),
        source_observed_at=scan.get('as_of_utc'), last_admitted_market_observed_at=pos.get('last_monitor_market_observed_at'),
        checked_at=now.isoformat(), action=action, reasons=reasons or ['HOLD_NO_EXIT_CONDITION'],
        thesis_status=health, confirmed_health_generation_id=pos.get('last_health_generation_id'),
        confirmed_health_observed_at=pos.get('last_health_observed_at_utc'),
        thesis_review=(e or {}).get('v2_thesis_review'), profit_protection=protection,
        rebound_review=(pos.get('loss_recovery_lifecycle') or {}).get('rebound_review'),
        capital_authority='NONE_SHADOW_ONLY', real_trading_enabled=False)
    checks = ((e or {}).get('v2_thesis_review') or {}).get('checks') or {}
    receipt['evidence_status'] = ('PARTIAL_DATA' if not checks or any(value in
        ('UNKNOWN','LEGACY_ENTRY_EVIDENCE_NOT_RETAINED') for value in checks.values()) else 'COMPLETE')
    pos['last_monitor_decision'] = receipt
    snapshot = pos.get('health_lifecycle_snapshot')
    if snapshot and snapshot.get('generation_id') == pos.get('last_health_generation_id') and health != 'EVIDENCE_PENDING':
        snapshot.update(recovery_state=pos.get('recovery_state'), rebound_review_state=(receipt['rebound_review'] or {}).get('state'))


def projection(pos):
    d = pos.get('last_monitor_decision') or {}; p = pos.get('protection_lifecycle') or {}
    health = d.get('thesis_status', 'NOT_YET_RECORDED')
    return dict(asset=pos['asset'], shadow_id=pos.get('shadow_id'),
        position_state='HARD_INVALIDATED' if health == 'HARD_INVALIDATION' else health,
        thesis_status=health, degrade_confirm_count=pos.get('degraded_cycles', 0),
        hard_invalidation=(None if health in ('EVIDENCE_PENDING','NOT_YET_RECORDED') else health == 'HARD_INVALIDATION'),
        profit_protection_armed=p.get('state', 'UNARMED') != 'UNARMED',
        profit_protection_peak=p.get('peak_reference_price'), profit_protection_floor=p.get('protected_floor_usdt'),
        profit_protection_floor_unit='NET_USDT', loss_recovery_state=('REBOUND_EXIT_REVIEW' if (d.get('rebound_review') or {}).get('state') == 'REBOUND_EXIT_REVIEW' else pos.get('recovery_state', 'NONE')),
        last_monitor_action=d.get('action'), last_monitor_reason=d.get('reasons'), last_checked_at=d.get('checked_at'),
        generation_id=d.get('generation_id'), confirmed_health_state=pos.get('health_state'))
