"""Read-only common release attestation. Never enables trading or writes state."""
import hashlib
import pathlib
import sys


def component_receipt(engine):
    risk = getattr(engine, 'loss_freeze', None)
    if getattr(risk, 'POLICY', None) != 'LOSS_AND_MARKET_V1':return None
    modules = {'engine': engine, 'reentry': engine.reentry,
               'sources': engine.reentry.provenance, 'loss': risk, 'release': sys.modules[__name__]}
    try:
        hashes = {k: hashlib.sha256(pathlib.Path(v.__file__).read_bytes()).hexdigest() for k,v in modules.items()}
        root = pathlib.Path(engine.__file__).parent
        for name in ('hunter_early_signals.py','hunter_identity_audit.py','hunter_tactical_capital_review.py','results/hunter-shadow-rules.json'):
            hashes[name] = hashlib.sha256((root/name).read_bytes()).hexdigest()
    except (AttributeError, OSError, TypeError):return None
    return {'reentry': engine.reentry.VERSION, 'loss': risk.POLICY, 'code_sha256': hashes}


def annotation(engine, state, context, now):
    """Require actual component bytes plus both independently migrated lanes.

    The deployment owner supplies the common manifest only after approving the
    combined release. This task creates no production manifest. One lane's
    policy string, a mock callable, or the calendar date cannot activate labels.
    """
    context = context or {}
    manifest, lanes = context.get('manifest') or {}, context.get('lanes') or {}
    receipt = component_receipt(engine)
    if (not receipt or manifest.get('schema') != 'hunter_common_release_v1'
            or manifest.get('strategy_version') != engine.reentry.STRATEGY_VERSION
            or manifest.get('components') != receipt or manifest.get('enabled') is not True
            or manifest.get('real_trading_enabled') is not False or manifest.get('real_order_count') != 0
            or manifest.get('capital_authority') != 'NONE_SHADOW_ONLY'):
        return {}
    activated = engine.reentry.time(manifest.get('activated_at_utc'))
    if not activated or activated > now or set(lanes) != {'V1','V2'} or not manifest.get('release_id'):return {}
    if lanes['V1'] is lanes['V2']:return {}
    if state is not lanes.get('V1') and state is not lanes.get('V2'):return {}
    for lane, portfolio in lanes.items():
        control = portfolio.get('loss_control') or {}
        completed = control.get('legacy_completion')
        lane_at = engine.reentry.time(control.get('activated_at_utc'))
        if (portfolio.get('mode') != 'SIMULATION_ONLY_NO_REAL_ORDERS'
                or control.get('policy') != receipt['loss'] or not lane_at or not lane_at <= activated
                or (manifest.get('lane_activation_ids') or {}).get(lane) != control.get('activated_at_utc')
                or not engine.fresh(control.get('evaluated_at_utc'), now)
                or not isinstance(completed, dict) or completed.get('status') not in (None,'NORMAL')
                or (engine.finite(completed.get('quarantined_cash_usdt')) or 0) != 0):
            return {}
    return {'strategy_version': engine.reentry.STRATEGY_VERSION, 'strategy_note': engine.reentry.STRATEGY_NOTE,
            'strategy_components': receipt, 'strategy_release_id': manifest.get('release_id'),
            'strategy_release_activated_at_utc': activated.isoformat()}
