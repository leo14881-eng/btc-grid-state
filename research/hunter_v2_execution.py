"""Candidate-only full-quantity shadow liquidation; never places orders.

Cash semantics: CASH_INCLUSIVE notional is the entire cash debit (buy fee
already included); FEE_ADDITIONAL notional excludes the quote-denominated fee.
Recorded effective quantity already includes buy spread/slippage/base fees.
Sell VWAP includes spread and depth impact; those costs are diagnostics only,
never deducted again. VALID is a snapshot estimate, not a promised fill.
"""
from datetime import datetime, timezone
import math

MODEL_VERSION = "v2_full_liquidation_candidate_v1"


def _number(value, *, positive=False):
    if isinstance(value, bool) or value is None:
        raise ValueError("INVALID_NUMBER")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError("INVALID_NUMBER") from None
    if not math.isfinite(result) or (result <= 0 if positive else result < 0):
        raise ValueError("INVALID_NUMBER")
    return result


def _time(value):
    if not isinstance(value, str):
        raise ValueError("SOURCE_TIMESTAMP_UNKNOWN")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("INVALID_TIMESTAMP") from None
    if parsed.tzinfo is None:
        raise ValueError("TIMEZONE_REQUIRED")
    return parsed.astimezone(timezone.utc)


def _unknown(reason):
    return {"status": "UNKNOWN", "reason": reason,
            "model_version": MODEL_VERSION, "execution_verified": False,
            "capital_authority": "NONE_SHADOW_ONLY", "real_trading_enabled": False}


def position_cashflows(position, *, allow_legacy_model=False):
    """Normalize immutable tranche cash debit and effective base quantity.

    Legacy derivation exactly reproduces the legacy quantity convention, but
    is separately labelled and cannot claim execution evidence verification.
    No fee defaults, no inferred currency units, and no top-level qty fallback.
    """
    tranches = position.get("tranches")
    if not isinstance(tranches, list) or not tranches:
        raise ValueError("TRANCHES_UNKNOWN")
    quantity = invested = buy_costs = 0.0
    provenance = []
    for tranche in tranches:
        if not isinstance(tranche, dict):
            raise ValueError("INVALID_TRANCHE")
        notional = _number(tranche.get("notional_usdt"), positive=True)
        slip = _number(tranche.get("buy_slippage_bps"))
        if slip >= 10000:
            raise ValueError("INVALID_BUY_SLIPPAGE")
        if "effective_quantity" not in tranche:
            if not allow_legacy_model:
                raise ValueError("EFFECTIVE_QUANTITY_UNKNOWN")
            fee_bps = _number(tranche.get("buy_fee_bps"))
            if fee_bps >= 10000:
                raise ValueError("INVALID_BUY_FEE")
            price = _number(tranche.get("price"), positive=True)
            qty = notional / (price * (1 + (slip + fee_bps) / 10000))
            debit = notional
            fee = notional * fee_bps / 10000
            provenance.append("LEGACY_MODEL_DERIVED")
        else:
            qty = _number(tranche.get("effective_quantity"), positive=True)
            fee = _number(tranche.get("buy_fee_usdt"))
            if fee >= notional:
                raise ValueError("INVALID_BUY_FEE")
            convention = tranche.get("cash_convention")
            if convention == "CASH_INCLUSIVE":
                if fee >= notional:
                    raise ValueError("INVALID_BUY_FEE")
                debit = notional
            elif convention == "FEE_ADDITIONAL":
                debit = notional + fee
            else:
                raise ValueError("CASH_CONVENTION_UNKNOWN")
            if tranche.get("quantity_unit") != "BASE" or tranche.get("fee_unit") != "USDT":
                raise ValueError("TRANCHE_UNITS_UNKNOWN")
            _time(tranche.get("timestamp"))
            provenance.append("RECORDED_SHADOW_EFFECTIVE_QUANTITY")
        quantity += qty
        invested += debit
        buy_costs += fee
    if not all(math.isfinite(x) and x > 0 for x in (quantity, invested)):
        raise ValueError("INVALID_CASHFLOW_TOTAL")
    return {"total_effective_quantity": quantity, "total_invested_cash": invested,
            "cumulative_buy_costs": buy_costs, "quantity_provenance": provenance,
            "buy_costs_already_in_cash_or_quantity": True}


def estimate_full_liquidation(position, order_book, *, decision_timestamp,
                              expected_exchange, expected_symbol,
                              sell_fee_bps, expected_market="spot",
                              max_age_seconds=30, future_tolerance_seconds=2,
                              allow_legacy_model=False):
    """Return VALID/UNKNOWN; consume all base quantity across validated bids.

    Input book requires exchange/market/symbol/side='SELL', price_unit='USDT',
    quantity_unit='BASE', source_timestamp, fetched_at, bids and asks. Timestamp
    is the exchange event/snapshot time; request time cannot substitute for it.
    Partial books are acceptable only if displayed bids cover the full quantity.
    Recorded BUY tranches additionally require historical execution_evidence
    identity, timestamps and effective_quantity; old books need not be fresh now,
    but must have been fresh at their recorded BUY decision. execution_verified
    means validated input evidence, never an exchange fill confirmation.
    """
    try:
        if not isinstance(position, dict) or not isinstance(order_book, dict):
            raise ValueError("INVALID_INPUT")
        if not all(isinstance(x, str) and x for x in (expected_exchange, expected_symbol, expected_market)):
            raise ValueError("EXPECTED_MARKET_IDENTITY_UNKNOWN")
        for key, expected in (("exchange", expected_exchange), ("symbol", expected_symbol),
                              ("market", expected_market), ("side", "SELL")):
            if order_book.get(key) != expected:
                raise ValueError(key.upper() + "_MISMATCH")
        if order_book.get("price_unit") != "USDT" or order_book.get("quantity_unit") != "BASE":
            raise ValueError("BOOK_UNITS_UNKNOWN")
        decision = _time(decision_timestamp)
        source = _time(order_book.get("source_timestamp"))
        fetched = _time(order_book.get("fetched_at"))
        age_limit = _number(max_age_seconds, positive=True)
        tolerance = _number(future_tolerance_seconds)
        if (source - decision).total_seconds() > tolerance or (fetched - decision).total_seconds() > tolerance:
            raise ValueError("FUTURE_EVIDENCE")
        if (source - fetched).total_seconds() > tolerance:
            raise ValueError("SOURCE_AFTER_FETCH")
        if (decision - source).total_seconds() > age_limit:
            raise ValueError("STALE_EXECUTION_EVIDENCE")
        fee_bps = _number(sell_fee_bps)
        if fee_bps >= 10000:
            raise ValueError("INVALID_SELL_FEE")
        cashflow = position_cashflows(position, allow_legacy_model=allow_legacy_model)
        for tranche in position["tranches"]:
            if "timestamp" in tranche and _time(tranche["timestamp"]) > decision:
                raise ValueError("FUTURE_BUY_CASHFLOW")
            if "effective_quantity" in tranche:
                buy_evidence = tranche.get("execution_evidence")
                if not isinstance(buy_evidence, dict):
                    raise ValueError("BUY_EXECUTION_EVIDENCE_UNKNOWN")
                for key, expected in (("exchange", expected_exchange), ("symbol", expected_symbol),
                                      ("market", expected_market), ("side", "BUY"),
                                      ("price_unit", "USDT"), ("quantity_unit", "BASE")):
                    if buy_evidence.get(key) != expected:
                        raise ValueError("BUY_EVIDENCE_" + key.upper() + "_MISMATCH")
                buy_source = _time(buy_evidence.get("source_timestamp"))
                buy_fetched = _time(buy_evidence.get("fetched_at"))
                buy_at = _time(tranche["timestamp"])
                if (buy_source-buy_at).total_seconds() > tolerance or (buy_fetched-buy_at).total_seconds() > tolerance:
                    raise ValueError("FUTURE_BUY_EXECUTION_EVIDENCE")
                if (buy_source-buy_fetched).total_seconds() > tolerance:
                    raise ValueError("BUY_SOURCE_AFTER_FETCH")
                if (buy_at-buy_source).total_seconds() > age_limit:
                    raise ValueError("STALE_BUY_EXECUTION_EVIDENCE")
                evidence_quantity = _number(buy_evidence.get("effective_quantity"), positive=True)
                if not math.isclose(evidence_quantity, float(tranche["effective_quantity"]), rel_tol=1e-10):
                    raise ValueError("BUY_EVIDENCE_QUANTITY_MISMATCH")
        sides = []
        for side in ("bids", "asks"):
            levels = order_book.get(side)
            if not isinstance(levels, list) or not levels:
                raise ValueError("ORDER_BOOK_UNKNOWN")
            parsed = []
            for level in levels:
                if not isinstance(level, (list, tuple)) or len(level) != 2:
                    raise ValueError("INVALID_BOOK_LEVEL")
                parsed.append((_number(level[0], positive=True), _number(level[1], positive=True)))
            if len({p for p, _ in parsed}) != len(parsed):
                raise ValueError("DUPLICATE_BOOK_PRICE")
            sides.append(sorted(parsed, reverse=side == "bids"))
        bids, asks = sides
        best_bid, best_ask = bids[0][0], asks[0][0]
        if best_ask <= best_bid:
            raise ValueError("CROSSED_OR_LOCKED_BOOK")
        quantity = cashflow["total_effective_quantity"]
        remaining, gross, used = quantity, 0.0, []
        for price, depth_quantity in bids:
            sold = min(remaining, depth_quantity)
            gross += sold * price
            used.append({"price": price, "quantity": sold})
            remaining -= sold
            if remaining <= quantity * 1e-12:
                break
        if remaining > quantity * 1e-12:
            raise ValueError("INSUFFICIENT_FULL_LIQUIDATION_DEPTH")
        sell_fee = gross * fee_bps / 10000
        proceeds = gross - sell_fee
        invested = cashflow["total_invested_cash"]
        net = proceeds - invested
        mid = (best_bid + best_ask) / 2
        vwap = gross / quantity
        diagnostics = ((best_ask-best_bid)/mid*10000, (best_bid-vwap)/best_bid*10000,
                       quantity*(mid-best_bid), quantity*best_bid-gross, net/invested*100)
        if not all(math.isfinite(x) for x in (gross, proceeds, net, vwap, *diagnostics)):
            raise ValueError("NONFINITE_LIQUIDATION")
        legacy = "LEGACY_MODEL_DERIVED" in cashflow["quantity_provenance"]
        return {"status": "VALID", "reason": "FULL_QUANTITY_SNAPSHOT_ESTIMATE",
                "model_version": MODEL_VERSION, **cashflow,
                "execution_verified": not legacy, "full_quantity_verified": not legacy,
                "evidence_scope": "LEGACY_MODEL_DERIVED" if legacy else "VERIFIED_BOOK",
                "fill_guaranteed": False,
                "exchange": expected_exchange, "market": expected_market,
                "symbol": expected_symbol, "side": "SELL",
                "source_timestamp": source.isoformat(), "fetched_at": fetched.isoformat(),
                "decision_timestamp": decision.isoformat(), "evidence_age_seconds": max(0, (decision-source).total_seconds()),
                "best_bid": best_bid, "best_ask": best_ask, "spread_bps": (best_ask-best_bid)/mid*10000,
                "estimated_full_liquidation_gross": gross,
                "estimated_full_liquidation_proceeds": proceeds,
                "estimated_sell_vwap": vwap, "estimated_slippage_bps": (best_bid-vwap)/best_bid*10000,
                "spread_cost_vs_mid_usdt": quantity*(mid-best_bid),
                "depth_impact_cost_usdt": quantity*best_bid-gross,
                "sell_fee_usdt": sell_fee, "sell_fee_bps": fee_bps,
                "depth_consumed": used, "net_pnl_usdt": net,
                "net_return_pct": net/invested*100,
                "executable_net_pnl_usdt": net, "executable_net_return_pct": net/invested*100,
                "execution_price": vwap, "evidence_at": source.isoformat(), "quantity": quantity,
                "capital_authority": "NONE_SHADOW_ONLY", "real_trading_enabled": False}
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        return _unknown(str(exc) if isinstance(exc, ValueError) else "INVALID_INPUT")
