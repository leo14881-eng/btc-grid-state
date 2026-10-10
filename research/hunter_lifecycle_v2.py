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
        partial = [r for r in rows if float(r[0]) <= captured.timestamp()*1000 <= float(r[6])]
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
        # A live bar cannot confirm weakness, but observable recovery vetoes an
        # exit based on an older closed window. Absence/invalidity stays unknown.
        live_veto = None
        live = dict(status='UNKNOWN', reason='CURRENT_PARTIAL_BAR_UNAVAILABLE')
        if len(partial) == 1:
            r = partial[0]
            op, hi, lo, cl, vol, buy = [number(r[j]) for j in (1,2,3,4,7,10)]
            if (all(x is not None for x in (op,hi,lo,cl,vol,buy))
                    and float(r[0]) == float(rows[-1][6])+1
                    and float(r[6])-float(r[0]) == 899999
                    and 0 < lo <= min(op,cl) <= max(op,cl) <= hi
                    and vol > 0 and 0 <= buy <= vol):
                live_veto = cl > op and cl > closes[-1] and buy/vol > .5
                live = dict(status='OBSERVED_PARTIAL_VETO_ONLY', close=cl,
                    taker_buy_quote_ratio=buy/vol, recovery_veto=live_veto,
                    source_open_at_ms=r[0], source_close_at_ms=r[6])
        return dict(status='FRESH', source_closed_at_ms=rows[-1][6],
                    receipt_sha256=hashlib.sha256(json.dumps(receipt, sort_keys=True).encode()).hexdigest(),
                    structure_weak=weak_structure, structure_recovered=strong_structure,
                    rebound_exhaustion=rebound, volume_ratio=recent/prior,
                    taker_buy_quote_ratio=demand, demand_weak=recent < prior and demand < .5,
                    demand_recovered=recent >= prior and demand > .5,
                    support=min(lows[-8:]), resistance=max(highs[-8:]), close=closes[-1],
                    live_recovery_veto=live_veto, live_observation=live)
    except (KeyError, TypeError, ValueError, IndexError, ZeroDivisionError, OverflowError) as ex:
        return dict(status='UNKNOWN', reason=str(ex))


def relative_sources(packets, asset, signal, now):
    """Validate asset AND BTC source rows; a new processing stamp is not source evidence."""
    try:
        returns = {}; sources = {}; latest_opens=set()
        explicit = signal.get('source_closed_at_utc')
        if explicit:
            age=(now-dt.datetime.fromisoformat(explicit.replace('Z','+00:00'))).total_seconds()
            if not 0 <= age <= 3700:raise ValueError('EXPLICIT_RELATIVE_SOURCE_STALE')
        for window, count in (('1h',2),('4h',5)):
            for role,symbol in (('asset',asset+'USDT'),('btc','BTCUSDT')):
                key=role+'_'+window; p=packets[key]
                if (p.get('schema')!='hunter_v2_rolling_source_v1' or p.get('symbol')!=symbol
                        or p.get('exchange')!='binance' or p.get('market')!='spot'
                        or p.get('interval')!='1h' or p.get('lookback')!=window
                        or not fresh(p.get('fetched_at'),now)):
                    raise ValueError('RELATIVE_SOURCE_IDENTITY_OR_TIME_INVALID:'+key)
                captured=dt.datetime.fromisoformat(p['fetched_at'].replace('Z','+00:00'))
                rows=p['rows']
                if len(rows)!=count:raise ValueError('RELATIVE_SOURCE_ROWS_INCOMPLETE:'+key)
                for i,r in enumerate(rows):
                    op,hi,lo,cl=[number(r[j]) for j in (1,2,3,4)]
                    if (any(x is None for x in (op,hi,lo,cl))
                            or not 0 < lo <= min(op,cl) <= max(op,cl) <= hi
                            or float(r[6])-float(r[0])!=3599999
                            or (i and float(r[0])-float(rows[i-1][0])!=3600000)):
                        raise ValueError('RELATIVE_SOURCE_BAR_INVALID:'+key)
                expected=int(captured.timestamp()*1000)//3600000*3600000
                if float(rows[-1][0])!=expected:raise ValueError('RELATIVE_SOURCE_WINDOW_STALE:'+key)
                latest_opens.add(float(rows[-1][0]))
                returns[key]=(float(rows[-1][4])/float(rows[0][1])-1)*100
                sources[key]=dict(source_open_at_ms=rows[0][0],source_close_at_ms=rows[-1][6],
                    fetched_at=p['fetched_at'],rows_sha256=hashlib.sha256(json.dumps(rows,sort_keys=True).encode()).hexdigest())
        if len(latest_opens)!=1:raise ValueError('RELATIVE_SOURCE_WINDOWS_NOT_ALIGNED')
        r1=returns['asset_1h']-returns['btc_1h'];r4=returns['asset_4h']-returns['btc_4h']
        for name,value in (('btc_relative_1h_pct',r1),('btc_relative_4h_pct',r4),('relative_acceleration_pct',r1-r4/4)):
            if number(signal.get(name)) is None or abs(float(signal[name])-round(value,4))>.00011:
                raise ValueError('RELATIVE_SOURCE_VALUE_MISMATCH:'+name)
        return dict(status='FRESH',sources=sources)
    except (KeyError,TypeError,ValueError,IndexError,OverflowError,ZeroDivisionError) as ex:
        return dict(status='UNKNOWN',reason=str(ex))


def bybit_sources(source,pos,signal,now):
    """Use primary Bybit packets; absent taker-buy evidence stays UNKNOWN."""
    try:
        from research.hunter_bybit_management import candles
        from research.hunter_bybit_signal_capture import features
        if source.get('schema')!='hunter_bybit_thesis_source_v1':raise ValueError('BYBIT_THESIS_SOURCE_MISSING')
        parsed={};opens=set()
        for role,symbol in (('asset',pos['market_symbol']),('btc','BTCUSDT')):
            packet=source[role+'_source']
            if packet.get('symbol')!=symbol or not fresh(packet.get('captured_at'),now):raise ValueError('BYBIT_THESIS_CAPTURE_INVALID')
            at=dt.datetime.fromisoformat(packet['captured_at'])
            hourly=candles(packet['hourly'],symbol,'60',5,at)
            quarter=candles(packet['quarter'],symbol,'15',25,at)
            parsed[role]=(features(hourly,quarter),quarter)
            opens.add(int(hourly[-1][0]))
        if len(opens)!=1:raise ValueError('BYBIT_RELATIVE_WINDOWS_NOT_ALIGNED')
        a,b=parsed['asset'][0],parsed['btc'][0]
        r1=a[0]['return_pct']-b[0]['return_pct'];r4=a[1]['return_pct']-b[1]['return_pct']
        for key,value in (('btc_relative_1h_pct',r1),('btc_relative_4h_pct',r4),('relative_acceleration_pct',r1-r4/4)):
            if number(signal.get(key)) is None or abs(float(signal[key])-round(value,4))>.00011:raise ValueError('BYBIT_RELATIVE_VALUE_MISMATCH')
        rows=parsed['asset'][1][:-1];highs=[float(r[2]) for r in rows];lows=[float(r[3]) for r in rows]
        closes=[float(r[4]) for r in rows]
        tech=dict(status='FRESH',exchange='bybit',source_closed_at_ms=int(rows[-1][0])+899999,
            structure_weak=max(highs[-4:])<max(highs[-8:-4]) and min(lows[-4:])<min(lows[-8:-4]),
            structure_recovered=max(highs[-4:])>max(highs[-8:-4]) and min(lows[-4:])>min(lows[-8:-4]),
            rebound_exhaustion=closes[-2]>closes[-3] and closes[-1]<closes[-2],
            support=min(lows[-8:]),resistance=max(highs[-8:]),close=closes[-1],
            demand_status='UNKNOWN',demand_weak=None,demand_recovered=None,live_recovery_veto=None,
            reason='BYBIT_TAKER_BUY_DEMAND_NOT_AVAILABLE')
        return tech,dict(status='FRESH',exchange='bybit',source=copy.deepcopy(source))
    except (KeyError,TypeError,ValueError,IndexError,OverflowError,ZeroDivisionError) as ex:
        unknown=dict(status='UNKNOWN',reason=str(ex));return unknown,copy.deepcopy(unknown)


def enrich(evidence, candidate, supply, review, pos, scan, now):
    """Recheck each dimension, preserving UNKNOWN separately from deterioration."""
    e = copy.deepcopy(evidence)
    c = candidate or {}; meta = e.get('signal_evidence') or {}
    source = c.get('v2_lifecycle_evidence') or (c.get('signal') or {}).get('v2_lifecycle_evidence') or {}
    receipt = source.get('micro_receipt') or {}
    same_cycle = source.get('generation_id') == scan.get('generation_id')
    tech = technical(receipt, pos['asset'], now) if same_cycle else dict(status='UNKNOWN', reason='MICRO_GENERATION_MISSING_OR_MISMATCH')
    relative = relative_sources(source.get('relative_receipts') or {},pos['asset'],c.get('signal') or {},now)
    if same_cycle and pos.get('execution_venue')=='BYBIT_SPOT':
        tech,relative=bybit_sources(source,pos,c.get('signal') or {},now)
    signal_ok = (meta.get('generation_id') == scan.get('generation_id') and bool(meta.get('evidence_id'))
                 and fresh(meta.get('observed_at_utc'), now)
                 and all(number(e.get(k)) is not None for k in ('score', 'independent', 'btc_rel_1h', 'btc_rel_4h', 'rel_accel'))
                 and all(number((c.get('signal') or {}).get(k)) is not None for k in
                         ('score','independent_signal_count','btc_relative_1h_pct','btc_relative_4h_pct','relative_acceleration_pct'))
                 and same_cycle and relative['status']=='FRESH')
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
                  structure=tech['status'], volume_demand=tech.get('demand_status',tech['status']),
                  fundamental_supply='FRESH' if supply_ok and sr.get('tactical_supply_risk_verified') else 'UNKNOWN',
                  venue='MATCHED' if not pos.get('execution_venue') or pos.get('market_symbol') == pos['asset']+'USDT' else 'UNKNOWN',
                  original_thesis='RECORDED' if entry_thesis_known(pos) else 'LEGACY_ENTRY_EVIDENCE_NOT_RETAINED')
    checks['original_thesis_revalidation']='UNKNOWN'
    e['v2_thesis_review'] = dict(generation_id=scan.get('generation_id'), checked_at=now.isoformat(),
        checks=checks, technical=tech, relative_sources=relative,
        relative_receipts=source.get('relative_receipts') if same_cycle else None,
        micro_receipt=receipt if same_cycle else None,
        supply=dict(status=sr.get('status'), source=sr.get('source'), verified_at_utc=sr.get('verified_at_utc'),
                    confirmed_major_risk=e['supply_confirmed_major_risk']),
        original_executable_gate=copy.deepcopy(pos.get('executable_gate')),
        entry_thesis_evidence=copy.deepcopy(pos.get('entry_thesis_evidence')))
    e['v2_thesis_review']['original_thesis_comparison'] = dict(status='UNKNOWN',
        reason='NO_VALIDATED_ORIGINAL_EXECUTION_THESIS_REVALIDATOR',
        recorded_inputs_available=entry_thesis_known(pos),
        numeric_comparisons={k:dict(original=(pos.get('entry_thesis_evidence') or {}).get(k),current=e.get(k))
            for k in ('score','independent','btc_rel_1h','btc_rel_4h','rel_accel')})
    e['v2_source_closed_at_ms']=tech.get('source_closed_at_ms') if signal_ok and tech['status']=='FRESH' else None
    e['v2_hard_generation_id']=scan.get('generation_id')
    e['v2_weak_reasons'] = []
    if tech.get('status') == 'FRESH':
        if tech.get('structure_weak'):e['v2_weak_reasons'].append('OBSERVED_LOWER_HIGHS_AND_LOWS')
        if tech.get('demand_weak'):e['v2_weak_reasons'].append('CLOSED_BAR_VOLUME_AND_BUY_DEMAND_WEAK')
    return e, signal_ok and tech['status']=='FRESH'


def rebound_review(pos, e, now, execution, health, generation):
    """Review only: no approved holding-risk model/original-thesis evaluator exists."""
    row = pos.setdefault('loss_recovery_lifecycle', {'transitions': []})
    previous = row.get('rebound_review') or {}
    if previous.get('generation_id') == generation:
        return dict(previous, exit=False, reason='REBOUND_EVIDENCE_ALREADY_CONSUMED')
    review = e.get('v2_thesis_review') or {}; tech = review.get('technical') or {}
    meta = e.get('signal_evidence') or {}
    net = number(execution.get('net_pnl_usdt'))
    r1, r4 = number(e.get('btc_rel_1h')), number(e.get('btc_rel_4h'))
    recovered = (health == 'STRONG' or (r1 is not None and r1 >= 0)
                 or (r4 is not None and r4 >= 0) or tech.get('structure_recovered') is True
                 or tech.get('live_recovery_veto') is True)
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
    comparison = dict(status='UNKNOWN', reason='NO_APPROVED_HOLDING_VS_EXIT_RISK_MODEL',
        observed_support=tech.get('support'),observed_resistance=tech.get('resistance'),
        geometric_distance_is_not_expected_risk=True)
    risk_higher = False
    checks=review.get('checks') or {}
    conditions = dict(fresh_cycle=bool(fresh_cycle), net_loss=net is not None and net < 0,
        original_thesis_known=entry_thesis_known(pos),
        original_thesis_revalidated=False,
        candidate_evidence_current=checks.get('candidate')=='FRESH',
        fundamental_supply_current=checks.get('fundamental_supply')=='FRESH',
        current_recovery_observation_known=tech.get('live_recovery_veto') is not None,
        thesis_still_invalidated=health == 'THESIS_INVALIDATED' and int(pos.get('degraded_cycles') or 0) >= C['DEGRADE_CONFIRM_CYCLES'],
        rebound_window=tech.get('rebound_exhaustion') is True,
        relative_still_weak=r1 is not None and r4 is not None and r1 < 0 and r4 < 0,
        structure_still_weak=tech.get('structure_weak') is True,
        demand_still_weak=tech.get('demand_weak') is True,
        execution_clear=bool(liquidity_ok), holding_risk_higher=risk_higher, no_recovery=not recovered)
    authorized = False  # Unknown model and thesis revalidation cannot be overridden by geometry or wrappers.
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
        hard_invalidation_evidence=copy.deepcopy(pos.get('last_hard_invalidation_evidence')) if health=='HARD_INVALIDATION' else None,
        rebound_review=(pos.get('loss_recovery_lifecycle') or {}).get('rebound_review'),
        capital_authority='NONE_SHADOW_ONLY', real_trading_enabled=False)
    checks = ((e or {}).get('v2_thesis_review') or {}).get('checks') or {}
    receipt['evidence_status'] = ('PARTIAL_DATA' if not checks or any(value in
        ('UNKNOWN','LEGACY_ENTRY_EVIDENCE_NOT_RETAINED') for value in checks.values()) else 'COMPLETE')
    pos['last_monitor_decision'] = receipt
    snapshot = pos.get('health_lifecycle_snapshot')
    if snapshot and snapshot.get('generation_id') == pos.get('last_health_generation_id') and health != 'EVIDENCE_PENDING':
        snapshot.update(recovery_state=pos.get('recovery_state'), rebound_review_state=(receipt['rebound_review'] or {}).get('state'))


def finish_allocation(pos,state,start,scan,now,e):
    """The allocator, after management, owns the final action for this cycle."""
    previous=copy.deepcopy(pos.get('last_monitor_decision') or {})
    same=previous.get('generation_id')==scan.get('generation_id')
    evidence=dict(e)
    if same:evidence['v2_thesis_review']=previous.get('thesis_review')
    finish(pos,state,start,scan,now,previous.get('thesis_status','EVIDENCE_PENDING') if same else 'EVIDENCE_PENDING',
           evidence,previous.get('profit_protection') if same else None)
    final=pos['last_monitor_decision']
    final['phase']='CAPITAL_ALLOCATION_COMPLETE'
    if same:final['management_phase']={k:previous.get(k) for k in ('action','reasons','checked_at')}
    pos['last_capital_allocation_generation_id']=scan.get('generation_id')


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
