"""Shared post-exit reassessment. No orders, capital allocation or policy tuning.

Two ordered, materially different sampled observations describe a trajectory;
wall-clock passage and regenerated evidence IDs alone cannot do so.
"""
import copy
import datetime as dt
import hashlib
import json
import math
try:
    from research import hunter_signal_provenance as provenance
except ModuleNotFoundError as exc:
    if exc.name != 'research':raise
    import hunter_signal_provenance as provenance

VERSION = 'hunter-reentry-evidence-v2'
STRATEGY_VERSION = 'hunter-2026-0-10-11-v1'
STRATEGY_NOTE = '2026.0.10.11 新策略'


def buy_annotation(engine, state, context=None, now=None):
    try:
        from research import hunter_strategy_release as release
    except ModuleNotFoundError as exc:
        if exc.name != 'research':raise
        import hunter_strategy_release as release
    return release.annotation(engine, state, context, now or dt.datetime.now(dt.timezone.utc))


def audit(state, asset):
    row = (state.get('reentry_registry') or {}).get(asset) or {}
    return copy.deepcopy({k: row[k] for k in ('last_exit_reason', 'risk_lock', 'state',
        'reentry_path', 'last_reassessment', 'reentry_last_observation') if k in row})


def missing_candidates(state, review, scan, now, fresh):
    """A new valid cycle omitting an asset cannot bridge its old confirmation."""
    if not fresh(scan.get('as_of_utc'), now):return
    present = {c.get('asset') for c in review.get('candidates') or []}
    for asset,row in (state.get('reentry_registry') or {}).items():
        setup = row.get('reentry_setup')
        if (asset not in present and setup and scan.get('generation_id') != setup['generation']
                and time(scan.get('as_of_utc')) > time(setup['times'][1])):
            row.pop('reentry_setup', None)
            row.pop('reentry_path', None)
            row.update(reentry_confirmation_after_utc=now.isoformat(),state='REENTRY_UNKNOWN',
                       last_reassessment={'at':now.isoformat(),'reason':'CURRENT_CANDIDATE_MISSING'})


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def time(value):
    try:
        value = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return value if value.tzinfo else None
    except (TypeError, ValueError):
        return None


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


SIGNAL_FIELDS = ('score', 'independent_signal_count', 'btc_relative_1h_pct',
                 'btc_relative_4h_pct', 'relative_acceleration_pct',
                 'return_1h_pct', 'return_4h_pct', 'return_15m_pct',
                 'volume_acceleration_15m', 'compression_ratio', 'range_position_6h')


def signal_hash(candidate):
    signal = (candidate or {}).get('signal') or {}
    return digest({key: number(signal.get(key)) for key in SIGNAL_FIELDS})


def capture(row, pos, candidate, scan, book=None):
    """Attach immutable exit context to the new registry row, never old trades."""
    try:
        source_hash = book_hash(book) if book else None
    except (TypeError, ValueError, OverflowError):
        source_hash = None  # Missing audit context must not change SELL behavior.
    row['reentry_context'] = {
        'schema': VERSION,
        'generation': (scan or {}).get('generation_id'),
        'signal_hash': signal_hash(candidate) if candidate else None,
        'signal_evidence': copy.deepcopy((candidate or {}).get('signal_evidence') or {}),
        'health_reasons': list(pos.get('health_reasons') or []),
        'identity_audit': copy.deepcopy((candidate or {}).get('identity_audit') or {}),
        'signal_sources': copy.deepcopy(((candidate or {}).get('signal') or {}).get('source_provenance') or {}),
        'source_hash': source_hash,
        'source_sequence': number((book or {}).get('source_timestamp') if (book or {}).get('exchange') == 'bybit' else (book or {}).get('last_update_id')),
        'position': copy.deepcopy({k: pos[k] for k in (
            'asset', 'tranches', 'execution_venue', 'market_symbol', 'market_type',
            'execution_fee_bps', 'protection_lifecycle') if k in pos}),
    }


def book_hash(book):
    # Exclude every timestamp, sequence ID and the independent reference mark.
    # Numeric normalization also rejects a recycled payload with new formatting.
    return digest({side: [[number(p), number(q)] for p, q in book.get(side, [])]
                   for side in ('bids', 'asks')})


def restore_context(row, state, asset):
    """Read existing closed trade evidence; never alter ledger or EXITED state."""
    matches = [p for p in state.get('closed_positions', []) + state.get('closed_trade_archive', [])
               if p.get('asset') == asset and p.get('closed_at_utc') == row.get('last_exit_at_utc')
               and p.get('exit_reason') == row.get('last_exit_reason')]
    if len(matches) != 1:
        return
    pos = matches[0]
    if not pos.get('tranches'):
        return
    generation = (pos.get('protection_lifecycle') or {}).get('exited_generation_id') or pos.get('last_health_generation_id')
    capture(row, pos, None, {'generation_id': generation})
    row['reentry_context']['provenance'] = 'EXISTING_CLOSED_POSITION_READ_ONLY'


def contract_cause_cleared(cause, original, current, asset, now, exited):
    """A ticker/native-ID pass cannot resolve a contract or pair mismatch."""
    supported = {'FATAL_IDENTITY_OR_CONTRACT:THIRD_PARTY_CONTRACT_MISMATCH',
                 'FATAL_IDENTITY_OR_CONTRACT:OFFICIAL_EXCHANGE_PAIR_MISMATCH'}
    if cause == 'FATAL_IDENTITY_OR_CONTRACT:ASSET_IDENTITY_MISMATCH':
        causes = original.get('blockers') or []
        if not causes or any('FATAL_IDENTITY_OR_CONTRACT:'+c not in supported for c in causes):return False
        cause = 'FATAL_IDENTITY_OR_CONTRACT:'+causes[0]
    if cause not in supported:return False
    old = (original.get('contract_evidence') or {}).get('official') or {}
    proof = current.get('contract_evidence') or {}
    official, independent = proof.get('official') or {}, proof.get('independent') or {}
    platform = old.get('platform')
    address = str(old.get('contract_address') or '').strip().lower()
    if (current.get('identity_status') != 'THIRD_PARTY_CORROBORATED' or proof.get('contract_verified') is not True
            or not platform or not address or official.get('platform') != platform
            or str(official.get('contract_address') or '').strip().lower() != address
            or str((independent.get('platforms') or {}).get(platform) or '').strip().lower() != address
            or official.get('exchange_pair') != asset+'USDT'
            or not str(official.get('official_contract_source') or '').startswith('https://')
            or not str(independent.get('source_url') or '').startswith('https://')):
        return False
    official_at, independent_at = time(official.get('verified_at_utc')), time(independent.get('contract_as_of_utc'))
    # These are Identity Audit's existing 336h and 24h validity contracts.
    return bool(official_at and independent_at and exited < independent_at <= now
                and 0 <= (now-official_at).total_seconds() <= 336*3600
                and 0 <= (now-independent_at).total_seconds() <= 24*3600)


def evaluate(engine, state, candidate, price, scan, liq, supply, now, identity=None):
    row = (state.get('reentry_registry') or {}).get((candidate or {}).get('asset'))
    if not row:
        return True, ['FIRST_ENTRY']

    def deny(category, detail, interrupt=True):
        if interrupt:
            row.pop('reentry_setup', None)
            row.pop('reentry_path', None)
            row['reentry_confirmation_after_utc'] = now.isoformat()
        row.update(state=category, last_reassessment={'at': now.isoformat(), 'reason': detail})
        return False, [category, detail]

    if not row.get('reentry_context'):
        restore_context(row, state, (candidate or {}).get('asset'))
    context = row.get('reentry_context') or {}
    exited = time(row.get('last_exit_at_utc'))
    if not exited or not context.get('position'):
        return deny('REENTRY_UNKNOWN', 'EXIT_CONTEXT_MISSING')
    scan = scan or {}
    candidate = candidate or {}
    meta = candidate.get('signal_evidence') or {}
    book = engine.liq_for(liq or {}, candidate.get('asset')).get('raw_book_evidence') or {}
    times = [time(meta.get('observed_at_utc')), time(scan.get('as_of_utc')), time(book.get('fetched_at'))]
    generation = scan.get('generation_id')
    watermark = row.get('reentry_last_observation')
    cutoff = time(row.get('reentry_confirmation_after_utc'))
    # Duplicates/old packets are no-ops, including old malformed replays. A
    # genuinely newer observation with missing source data interrupts setup.
    if (generation == context.get('generation') or generation in row.get('reentry_seen_generations', [])
            or (meta.get('evidence_id') and meta['evidence_id'] in row.get('reentry_seen_evidence_ids', []))
            or (watermark and times[0] and times[0] <= time(watermark['times'][0]))
            or (cutoff and times[0] and times[0] <= cutoff)):
        return deny('REENTRY_NO_NEW_EVIDENCE', 'SAME_OR_REPLAYED_CYCLE', False)
    if (not generation or generation != meta.get('generation_id') or not meta.get('evidence_id')
            or any(t is None or not exited < t <= now or not engine.fresh(t.isoformat(), now) for t in times)):
        return deny('REENTRY_NO_NEW_EVIDENCE', 'POST_EXIT_FRESH_SOURCE_EVIDENCE_REQUIRED')
    prior = row.get('reentry_setup')
    if prior and any(not engine.fresh(at, now) for at in prior.get('validity_times', prior['times'])):
        prior = None
        row.pop('reentry_setup', None)
    if (generation == context.get('generation') or meta.get('evidence_id') == context['signal_evidence'].get('evidence_id')
            or generation in row.get('reentry_seen_generations', [])
            or meta['evidence_id'] in row.get('reentry_seen_evidence_ids', [])
            or (prior and (generation == prior['generation'] or any(t <= time(old) for t, old in zip(times, prior['times']))))):
        return deny('REENTRY_NO_NEW_EVIDENCE', 'SAME_OR_REPLAYED_CYCLE', False)
    current_hash = signal_hash(candidate)
    try:
        sources = provenance.signal_sources(candidate.get('signal'), candidate['asset'], now, exited, engine.fresh)
    except (TypeError, ValueError, KeyError, OverflowError):
        return deny('REENTRY_UNKNOWN', 'SOURCE_WINDOW_MISSING_INVALID_OR_OLD')
    baseline = (watermark or {}).get('signal_sources')
    if cutoff and any(time(s['observed_at_utc']) <= cutoff for s in sources.values()):
        return deny('REENTRY_NO_NEW_EVIDENCE', 'SOURCE_WINDOW_BEFORE_CONFIRMATION_RESTART', False)
    if not baseline:
        old_sources = context.get('signal_sources') or {}
        if all(old_sources.get(k) and sources[k]['hash'] == old_sources[k].get('content_hash') for k in ('asset_1h','asset_4h','micro')):
            return deny('REENTRY_NO_NEW_EVIDENCE', 'SOURCE_WINDOW_UNCHANGED_SINCE_EXIT', False)
    if baseline:
        if any(sources[k]['window'][-1] < baseline[k]['window'][-1]
               or time(sources[k]['observed_at_utc']) <= time(baseline[k]['observed_at_utc']) for k in sources):
            return deny('REENTRY_NO_NEW_EVIDENCE', 'SOURCE_WINDOW_REPLAY_OR_REVERSE', False)
        if all(sources[k]['window'] == baseline[k]['window'] and sources[k]['hash'] == baseline[k]['hash'] for k in ('asset_1h', 'asset_4h', 'micro')):
            return deny('REENTRY_NO_NEW_EVIDENCE', 'SOURCE_WINDOW_CONTENT_NOT_NEW', False)
    # Original venue matching is common safety; V1 does not inherit V2's
    # quantity/depth/rr entry gates. PP clearance alone needs old-quantity net.
    old_pos = context.get('position') or {}
    if old_pos.get('asset') != candidate.get('asset'):
        return deny('REENTRY_UNKNOWN', 'EXIT_ASSET_IDENTITY_MISMATCH')
    execution = engine.lifecycle.liquidation(old_pos, book, now, old_pos.get('execution_fee_bps', engine.FEE_BPS))
    expected = 'bybit' if old_pos.get('execution_venue') == 'BYBIT_SPOT' else 'binance'
    if (book.get('exchange') != expected or book.get('market') != 'spot'
            or book.get('symbol') != candidate['asset']+'USDT' or book.get('price_unit') != 'USDT'
            or book.get('quantity_unit') != 'BASE'):
        return deny('REENTRY_UNKNOWN', 'SOURCE_IDENTITY_MISMATCH')
    try:
        bids = [[float(p), float(q)] for p, q in book['bids']]
        asks = [[float(p), float(q)] for p, q in book['asks']]
        if not bids or not asks or any(not math.isfinite(p*q) or p <= 0 or q <= 0 for p, q in bids+asks):
            raise ValueError('invalid')
        if bids[0][0] >= asks[0][0]:raise ValueError('crossed')
        if any(bids[i][0] <= bids[i+1][0] for i in range(len(bids)-1)) or any(asks[i][0] >= asks[i+1][0] for i in range(len(asks)-1)):
            raise ValueError('unsorted')
        market_hash = book_hash(book)
    except (KeyError, TypeError, ValueError, OverflowError):
        return deny('REENTRY_UNKNOWN', 'SOURCE_BOOK_INVALID')
    sequence = number(book.get('last_update_id'))
    if expected == 'bybit':
        source_at = number(book.get('source_timestamp'))
        if source_at is None or not exited.timestamp() < source_at/1000 <= now.timestamp() or now.timestamp()-source_at/1000 > engine.MAX_EVIDENCE_AGE_SECONDS:
            return deny('REENTRY_NO_NEW_EVIDENCE', 'PRIMARY_VENUE_SOURCE_STALE_OR_UNKNOWN')
        sequence = source_at
    old_sequence = (prior or {}).get('source_sequence', context.get('source_sequence'))
    if old_sequence is not None and (sequence is None or sequence <= old_sequence):
        return deny('REENTRY_NO_NEW_EVIDENCE', 'SOURCE_SEQUENCE_NOT_NEW', False)
    if market_hash == context.get('source_hash') or market_hash in row.get('reentry_seen_markets', []):
        # New source window is new evidence even with equal derived/book values.
        if not baseline or all(sources[k]['window'] == baseline[k]['window'] for k in sources):
            return deny('REENTRY_NO_NEW_EVIDENCE', 'MARKET_CONTENT_NOT_NEW', False)
    if number(price) is None or price <= 0:
        return deny('REENTRY_UNKNOWN', 'CURRENT_PRICE_INVALID')
    e = engine.evidence(candidate, liq or {}, supply or {})
    health, health_reasons = engine.position_health({}, e, now)
    strong = health == 'STRONG' and all(number(e.get(k)) is not None and e[k] > 0
                                                      for k in ('btc_rel_1h', 'btc_rel_4h', 'rel_accel'))
    observation = {'generation': generation, 'evidence_id': meta['evidence_id'],
                   'times': [t.isoformat() for t in times], 'price': price, 'strong': strong,
                   'signal_hash': current_hash, 'market_hash': market_hash, 'source_sequence': sequence,
                   'cost_audit': execution, 'signal_sources': sources,
                   'validity_times': [t.isoformat() for t in times]+[s['observed_at_utc'] for s in sources.values()]}
    row['reentry_last_observation'] = observation
    row['reentry_setup'] = observation
    row.setdefault('reentry_seen_markets', []).append(market_hash)
    row.setdefault('reentry_seen_generations', []).append(generation)
    row.setdefault('reentry_seen_evidence_ids', []).append(meta['evidence_id'])
    row['post_exit_low'] = min(number(row.get('post_exit_low')) or price, price)
    reason = row.get('last_exit_reason')
    if row.get('risk_lock'):
        risk = state.get('systemic_risk') or {}
        risk_policy = getattr(engine, 'loss_freeze', None)
        cb = risk_policy.circuit_for_admission(state) if risk_policy else (state.get('circuit_breaker') or {})
        observed = time(risk.get('last_observed_at_utc'))
        if (risk.get('level') != 'NORMAL' or risk.get('recovery_mode') or cb.get('status') not in (None, 'NORMAL')
                or not observed or not exited < observed <= now or not engine.fresh(observed.isoformat(), now)
                or not risk.get('last_observation_id') or risk['last_observation_id'] == row.get('risk_lock_observation_id')):
            return deny('REENTRY_REASON_UNRESOLVED', 'NORMAL_POST_EXIT_MARKET_EVIDENCE_REQUIRED')
        # Fatal identity/supply clearance needs explicit verified current review,
        # not omission of the old blocker. Unknown/manual reasons stay locked.
        original = context.get('health_reasons') or []
        if reason != 'HARD_INVALIDATION' or not original:
            return deny('REENTRY_REASON_UNRESOLVED', 'ORIGINAL_RISK_CAUSE_CLEARANCE_UNKNOWN')
        for cause in original:
            if cause in ('CATASTROPHIC_SPREAD', 'CATASTROPHIC_DEPTH'):
                if cause == 'CATASTROPHIC_SPREAD' and (e.get('spread_bps') is None or e['spread_bps'] > engine.HARD_SPREAD_BPS):
                    return deny('REENTRY_REASON_UNRESOLVED', cause)
                if cause == 'CATASTROPHIC_DEPTH' and any(e.get(k) is None or e[k] < engine.HARD_MIN_DEPTH_USDT for k in ('bid_depth_2pct_usdt', 'ask_depth_2pct_usdt')):
                    return deny('REENTRY_REASON_UNRESOLVED', cause)
            elif cause == 'CONFIRMED_MAJOR_NEAR_TERM_SUPPLY_RISK':
                sr = engine.supply_for(supply or {}, candidate['asset']) or {}
                if (not sr.get('tactical_supply_risk_verified') or not sr.get('source') or engine.confirmed_supply_risk(sr)
                        or sr.get('status') not in ('FULLY_UNLOCKED', 'SCHEDULED_LOW_NEAR_TERM', 'CURRENT_SUPPLY_EFFECTIVELY_FULLY_CIRCULATING')
                        or (supply or {}).get('scan_generation_id') != generation
                        or not engine.fresh(sr.get('verified_at_utc'), now)
                        or not time(sr.get('verified_at_utc')) > exited):
                    return deny('REENTRY_REASON_UNRESOLVED', 'SUPPLY_CLEARANCE_UNKNOWN')
            elif cause.startswith('FATAL_IDENTITY_OR_CONTRACT:'):
                ident = ((identity or {}).get('assets') or {}).get(candidate['asset']) or {}
                if ((identity or {}).get('scan_generation_id') != generation or not engine.fresh((identity or {}).get('as_of_utc'), now)
                        or not time((identity or {}).get('as_of_utc')) > exited
                        or ident.get('capital_identity_pass') is not True or ident.get('blockers')):
                    return deny('REENTRY_REASON_UNRESOLVED', 'IDENTITY_CLEARANCE_UNKNOWN')
                if not contract_cause_cleared(cause, context.get('identity_audit') or {}, ident, candidate['asset'], now, exited):
                    return deny('REENTRY_UNKNOWN', 'ORIGINAL_IDENTITY_CAUSE_CLEARANCE_UNPROVEN')
            else:
                return deny('REENTRY_REASON_UNRESOLVED', 'ORIGINAL_RISK_CAUSE_CLEARANCE_UNKNOWN')
    if health != 'STRONG':
        return deny('REENTRY_REASON_UNRESOLVED', 'CURRENT_HEALTH_' + health, False)
    if reason == 'PROFIT_PROTECTION':
        if execution.get('status') != 'SHADOW_RECEIPT_ESTIMATE':
            return deny('REENTRY_UNKNOWN', 'EXIT_BASIS_EXECUTION_UNKNOWN')
        floor = number((old_pos.get('protection_lifecycle') or {}).get('protected_floor_usdt'))
        if floor is None:
            return deny('REENTRY_UNKNOWN', 'ORIGINAL_PROTECTION_FLOOR_MISSING')
        if execution['net_pnl_usdt'] <= floor:
            return deny('REENTRY_REASON_UNRESOLVED', 'ORIGINAL_PROTECTION_STILL_BREACHED_AFTER_COSTS')
    elif reason not in ('HARD_INVALIDATION', 'PROFIT_STAGNATION', 'THESIS_INVALIDATED_PROFIT_EXIT', 'PROFIT_REVIEW_MOMENTUM_FADED'):
        return deny('REENTRY_REASON_UNRESOLVED', 'EXIT_REASON_CLEARANCE_UNKNOWN')
    if not prior or not strong or price <= prior['price']:
        return deny('REENTRY_TREND_NOT_RESTORED', 'ORDERED_POST_EXIT_RECOVERY_REQUIRED', False)
    # No % reset or sell-price breakout prerequisite: actual dip then recovery,
    # or strength continuing across two independent post-exit observations.
    pullback = prior['price'] < (number(row.get('last_exit_price')) or 0) or (prior['price'] == row['post_exit_low'] < price and not prior['strong'])
    continuation = prior['strong']
    if not (pullback or continuation):
        return deny('REENTRY_TREND_NOT_RESTORED', 'POST_EXIT_TREND_NOT_CONFIRMED', False)
    path = 'PULLBACK_RECOVERY' if pullback else 'CONTINUED_STRENGTH'
    # Reassessment is only permission to run every current BUY gate.
    row.update(state='REENTRY_REASSESSMENT_ALLOWED', reentry_path=path,
               last_reassessment={'at': now.isoformat(), 'reason': path, 'cost_audit': execution})
    return True, ['REENTRY_REASSESSMENT_ALLOWED', path]
