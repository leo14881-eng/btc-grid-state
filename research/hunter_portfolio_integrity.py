"""Fail closed on authoritative shadow-ledger read failures and empty resets."""
import json

PORTFOLIO_NAMES = frozenset(("hunter-shadow-portfolio.json", "hunter-shadow-v2-portfolio.json"))
REQUIRED_LISTS = ("open_positions", "closed_positions", "events", "decisions")
HISTORY_LISTS = REQUIRED_LISTS + ("closed_trade_archive", "ever_entered_assets",
    "excluded_non_crypto_positions", "excluded_non_crypto_closed_positions",
    "excluded_non_crypto_events", "excluded_non_crypto_decisions")


def validate_portfolio(doc):
    if (not isinstance(doc, dict) or
            doc.get("schema") != "hunter_shadow_v2_portfolio_v2" or
            doc.get("mode") != "SIMULATION_ONLY_NO_REAL_ORDERS" or
            any(not isinstance(doc.get(k), list) for k in REQUIRED_LISTS)):
        raise RuntimeError("SHADOW_PORTFOLIO_SCHEMA_INVALID")
    return doc


def load_portfolio(path):
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        # A missing/corrupt SSOT is an operational failure, never a new cohort.
        raise RuntimeError("SHADOW_PORTFOLIO_READ_FAILED " + path.name) from exc
    return validate_portfolio(doc)


def require_nonempty_history_transition(path, candidate):
    validate_portfolio(candidate)
    previous = load_portfolio(path)
    if any(previous.get(k) for k in HISTORY_LISTS) and not any(candidate.get(k) for k in HISTORY_LISTS):
        raise RuntimeError("SHADOW_PORTFOLIO_EMPTY_RESET_REJECTED " + path.name)
