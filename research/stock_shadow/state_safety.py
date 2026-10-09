"""Input validation and derived reporting only; no order or strategy authority."""
import json
import math
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo


def number(value, field, default=None, positive=False):
    if value is None:
        if default is None:
            raise ValueError(f"stock_state:{field}:missing_number")
        value = default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"stock_state:{field}:invalid_number")
    value = float(value)
    if not math.isfinite(value) or (positive and value <= 0):
        raise ValueError(f"stock_state:{field}:invalid_number")
    return value


def optional_number(value, field, positive=False):
    return None if value is None else number(value, field, positive=positive)


def validate_inputs(state, events):
    if not isinstance(state, dict) or not isinstance(events, list):
        raise ValueError("stock_state:invalid_book_shape")
    positions = state.get("positions")
    closed = state.get("closed")
    if not isinstance(positions, dict) or not isinstance(closed, list):
        raise ValueError("stock_state:invalid_book_containers")
    for symbol, p in list(positions.items()) + [(x.get("symbol", "CLOSED"), x) for x in closed if isinstance(x, dict)]:
        if not isinstance(p, dict) or not isinstance(p.get("tranches"), list) or not p["tranches"]:
            raise ValueError(f"stock_state:{symbol}:invalid_tranches")
        for i, tr in enumerate(p["tranches"]):
            if not isinstance(tr, dict):
                raise ValueError(f"stock_state:{symbol}:invalid_tranche")
            for field in ("price", "notional"):
                number(tr.get(field), f"{symbol}.tranches[{i}].{field}", positive=True)
        for field in ("mfe_net_pct", "mae_net_pct"):
            optional_number(p.get(field), f"{symbol}.{field}")
        for field in ("swing_high_price", "pullback_low_price"):
            optional_number(p.get(field), f"{symbol}.{field}", positive=True)
        previous = p.get("position_state_v2")
        if previous is not None:
            if not isinstance(previous, dict):
                raise ValueError(f"stock_state:{symbol}.position_state_v2:invalid_shape")
            optional_number(previous.get("market_relative20"), f"{symbol}.market_relative20")
        pending = p.get("rebound_exit_pending_v3")
        if pending is not None:
            if not isinstance(pending, dict):
                raise ValueError(f"stock_state:{symbol}.rebound_exit_pending_v3:invalid_shape")
            optional_number(pending.get("lowest_net_return_pct"), f"{symbol}.lowest_net_return_pct")
    for x in closed:
        if not isinstance(x, dict):
            raise ValueError("stock_state:invalid_closed_record")
        number(x.get("realized_net_pnl_usdt"), f"{x.get('symbol')}.realized_net_pnl_usdt")
    for e in events:
        if not isinstance(e, dict):
            raise ValueError("stock_state:invalid_event")
        if e.get("type") in {"BUY", "ADD", "SELL"}:
            number(e.get("price"), f"{e.get('symbol')}.event.price", positive=True)
            if e["type"] in {"BUY", "ADD"}:
                number(e.get("notional"), f"{e.get('symbol')}.event.notional", positive=True)
            else:
                number(e.get("net_pnl_usdt"), f"{e.get('symbol')}.event.net_pnl_usdt")


def portfolio_statistics(state, events):
    validate_inputs(state, events)
    closed = state["closed"]
    valid = {s: p for s, p in state["positions"].items() if p.get("quote_status") == "VALID"}
    unavailable = sorted(set(state["positions"]) - set(valid))
    known_pnl = round(sum(number(p.get("net_pnl_usdt"), f"{s}.net_pnl_usdt") for s, p in valid.items()), 6)
    return {"valuation_coverage_status": "PARTIAL" if unavailable else "COMPLETE",
            "valuation_checked_at": state.get("updated_at"),
            "valuation_basis": "VALIDATED_SOURCE_BARS_NOT_REALTIME_QUOTES",
            "unvalued_symbols": unavailable, "valued_positions": len(valid),
            "unrealized_net_pnl_valued_subset_usdt": known_pnl,
            "unrealized_net_pnl_usdt": None if unavailable else known_pnl,
            "open_positions": len(state["positions"]), "closed_positions": len(closed),
            "events": len(events), "wins": sum(x["realized_net_pnl_usdt"] > 0 for x in closed),
            "losses": sum(x["realized_net_pnl_usdt"] <= 0 for x in closed),
            "realized_net_pnl_usdt": round(sum(x["realized_net_pnl_usdt"] for x in closed), 6),
            "portfolio_updated_at": state.get("updated_at"), "portfolio_run_id": state.get("run_id"),
            "portfolio_source_commit": state.get("source_commit")}


def run_with_health(fn, path, source_commit, run_id, save, **kwargs):
    started = datetime.now(timezone.utc).isoformat()
    report = {"started_at": started, "source_commit": source_commit, "run_id": run_id,
              "simulation_only": True, "real_orders": False}
    try:
        result = fn(**kwargs)
    except Exception as exc:
        report.update(status="FAILED", error_type=type(exc).__name__, error=str(exc)[:240],
                      updated_at=datetime.now(timezone.utc).isoformat(),
                      portfolio_trust="KEEP_LAST_VALID_BOOK_CHECK_RUN_BINDING")
        save(path, report)
        raise
    report.update(status="SUCCESS", updated_at=datetime.now(timezone.utc).isoformat())
    save(path, report)
    return result


# Data validity gates, not V3 trading thresholds. SIP requests end 20 minutes
# behind wall clock; allow one 5m bar plus 10m transport/scheduling tolerance.
DELAYED_BAR_MAX_AGE_SECONDS = 35 * 60
NY = ZoneInfo("America/New_York")


def parse_asof(value):
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return stamp.astimezone(timezone.utc) if stamp.tzinfo else None
    except (ValueError, TypeError):
        return None


@lru_cache(maxsize=1)
def security_events():
    return json.loads(Path(__file__).with_name("security-events.json").read_text())


def security_block(symbol, checked_at):
    # Verified security events are effective-dated and never rename a ledger.
    for record in security_events():
        if record["symbol"] == symbol and checked_at >= parse_asof(record["effective_at"]):
            return record
    return None


def quote_validity(symbol, observation, checked_at, kind):
    event = security_block(symbol, checked_at)
    if event:
        return event["status"]
    if not isinstance(observation, dict):
        return "MISSING_QUOTE"
    price = observation.get("price")
    if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
        return "INVALID_PRICE"
    stamp = parse_asof(observation.get("price_asof"))
    if stamp is None:
        return "MISSING_OR_INVALID_TIMESTAMP"
    age = (checked_at - stamp).total_seconds()
    if age < 0:
        return "FUTURE_TIMESTAMP"
    if stamp.astimezone(NY).date() != checked_at.astimezone(NY).date():
        return "STALE_SESSION"
    if kind == "5Min" and age > DELAYED_BAR_MAX_AGE_SECONDS:
        return "STALE_BAR"
    if kind == "1Day" and observation.get("refresh_received") is not True:
        return "DAILY_REFRESH_UNCONFIRMED"
    return "VALID"


def mark_quote(p, status, checked_at, observation=None):
    # Last-known price, extrema, lifecycle state, tranches and ledger stay intact.
    p["quote_status"] = status
    p["quote_checked_at"] = checked_at.isoformat()
    p["valuation_status"] = "VALID_DELAYED" if status == "VALID" else "UNAVAILABLE_LAST_KNOWN_ONLY"
    p["quote_actions_allowed"] = status == "VALID"
    if status == "VALID":
        p["price_asof"] = observation["price_asof"]
        p["quote_source"] = observation.get("source")
