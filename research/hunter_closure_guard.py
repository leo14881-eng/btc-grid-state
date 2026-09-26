#!/usr/bin/env python3
"""Deterministic Hunter candidate-closure guard (v2.17.3).

No trading, GitHub mutation, network requests, or live-price claims. Feed fresh
observations explicitly. Missing observations NEVER imply that a candidate
passed its capital gate. Exit code 2 means a triggered review is overdue.
"""
import argparse
import datetime as dt
import json
from pathlib import Path

FATAL = {"DATA_BLOCKED_FATAL", "SOURCE_UNAVAILABLE_ESCALATED"}
CLOSED = {"PROPOSAL_READY", "WAIT_PRICE_ONLY", "REJECT_FORWARD_ODDS",
          "REJECT_PERMANENT_LOSS"}


def timestamp(value):
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def audit(state, market, now):
    queue = state.get("closure_queue", {}).get("items", [])
    observations = market.get("observations", {})
    rows = []
    for item in queue:
        asset = item["asset"]
        observation = observations.get(asset, {})
        triggered = timestamp(item["triggered_at"])
        elapsed_h = (now - triggered).total_seconds() / 3600
        status = item["decision_status"]
        current = observation.get("price")
        reference = observation.get("reference_price")
        fresh_at = observation.get("as_of")
        fresh = bool(fresh_at and abs((now - timestamp(fresh_at)).total_seconds()) <= 3600)
        change = ((current / reference - 1) * 100
                  if fresh and isinstance(current, (int, float))
                  and isinstance(reference, (int, float)) and reference > 0
                  else None)
        repricing = (change is not None and abs(change) >= 8)
        overdue = elapsed_h > 1 and status not in CLOSED
        row = {
            "asset": asset, "decision_status": status,
            "exact_missing_fact": item.get("exact_missing_fact"),
            "remediation_source": item.get("remediation_source", []),
            "hours_since_trigger": round(elapsed_h, 2),
            "sla": ("SLA_TIME_EXCEEDED_SCHEDULE_NOT_VERIFIED" if overdue
                    else "CLOSED_OR_WITHIN_WINDOW"),
            "fresh_market_observation": fresh,
            "price_change_pct": round(change, 2) if change is not None else None,
            "repricing_review_trigger": repricing,
            "latency_alert": bool(change is not None and change >= 15 and status not in CLOSED),
            "process_failure_review": bool(change is not None and change >= 25
                                           and status not in CLOSED),
            "next_action": ("ESCALATE_PRIMARY_SOURCE_UNAVAILABLE_OR_RESOLVE_FATAL_FACT"
                            if overdue and status in FATAL else
                            item.get("next_check_condition")),
            "capital_action": "NO_NEW_ORDER_UNTIL_ALL_GATES_PASS"
        }
        rows.append(row)
    return {
        "schema": "hunter_closure_guard_v2.17.3",
        "as_of": now.isoformat(),
        "live_data_source": "EXPLICIT_INPUT_ONLY",
        "queue": rows,
        "overdue_count": sum(x["sla"].startswith("SLA_TIME_EXCEEDED") for x in rows),
        "capital_decision": "NO_AUTOMATIC_TRADING",
        "source_conflicts_resolved": False
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--state", required=True, type=Path)
    p.add_argument("--market", type=Path, help="Optional fresh observed prices JSON")
    p.add_argument("--now", required=True, help="Offset-aware ISO timestamp")
    args = p.parse_args()
    now = timestamp(args.now)
    if now.tzinfo is None:
        p.error("--now requires a timezone offset")
    state = json.loads(args.state.read_text(encoding="utf-8"))
    market = json.loads(args.market.read_text(encoding="utf-8")) if args.market else {}
    result = audit(state, market, now)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result["overdue_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
