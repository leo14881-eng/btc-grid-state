"""Evidence-backed shadow model identity; never infer historical fills or venues."""
import datetime as dt
import hashlib
import json
import math

try:
    from research.hunter_lifecycle_state import liquidation
except ModuleNotFoundError as exc:
    if exc.name != 'research':
        raise
    from hunter_lifecycle_state import liquidation


def identity_fields(pos, book, execution, now, generation, source_sha, fee_bps,
                    *, source_kind, formal=False, entry=False, max_age=600):
    """Only a complete, matching current Binance model receipt admits metadata.

    This is not a BUY/SELL gate. A caller owns any portfolio mutation and its CAS.
    Explicit/partial identities are immutable here. Bybit availability is not
    proof of execution, and existing historical trades remain untouched.
    """
    if any(pos.get(k) for k in ('execution_venue', 'market_symbol', 'market_type')):
        return {}
    try:
        if not source_sha or not generation:
            return {}
        observed = dt.datetime.fromisoformat(book['fetched_at'].replace('Z', '+00:00'))
        if observed.tzinfo is None or not 0 <= (now-observed).total_seconds() <= max_age:
            return {}
        if execution.get('fetched_at') != book['fetched_at'] or execution.get('fee_bps') != fee_bps:
            return {}
        replay = liquidation(pos, book, observed, fee_bps)
        if replay['status'] != 'SHADOW_RECEIPT_ESTIMATE' or execution.get('status') != replay['status']:
            return {}
        for key in ('net_pnl_usdt', 'quantity', 'vwap', 'capital'):
            if not math.isclose(float(execution[key]), float(replay[key]), rel_tol=1e-9, abs_tol=1e-7):
                return {}
        fields = dict(execution_venue='BINANCE_SPOT', market_symbol=book['symbol'],
                      market_type='spot', execution_fee_bps=fee_bps,
                      execution_identity_scope='CURRENT_SHADOW_EXECUTION_MODEL',
                      historical_entry_execution_venue='UNKNOWN',
                      execution_identity_proof={
                          'source_main_sha': source_sha, 'generation_id': generation,
                          'source_kind': source_kind, 'receipt_at': book['fetched_at'],
                          'admitted_at': now.isoformat(),
                          'raw_book_sha256': hashlib.sha256(json.dumps(book, sort_keys=True).encode()).hexdigest(),
                          'historical_execution_verified': False,
                          'formal_portfolio_mutated': formal})
        if entry:
            fields['shadow_entry_execution_venue'] = 'BINANCE_SPOT'
            fields['shadow_entry_model_observed_at_utc'] = book['fetched_at']
        return fields
    except (KeyError, ValueError, TypeError, OverflowError, ZeroDivisionError):
        return {}
