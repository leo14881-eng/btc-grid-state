"""Offline candidate protection engine. Never imports or writes a live portfolio.

Parameters are frozen research hypotheses, not production recommendations. Absolute
USDT floors survive ADD; percentages describe the basis when the floor was set.
Persistence/CAS belongs to the caller: only persist the returned whole transition
under its original generation guard. This module does not advertise persistence.
"""
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import math

STATES = {"UNARMED", "PROFIT_PROTECTION_ARMED", "RUNNER", "EXIT_PENDING", "CLOSED"}
ENGINE_VERSION = "persistent_v1_candidate"


@dataclass(frozen=True)
class ProtectionParams:
    arm_pct: float = 2.0
    runner_pct: float = 8.0
    arm_basis: str = "net"
    floor_mode: str = "capture_ratio"
    capture_low: float = 0.25
    capture_mid: float = 0.5
    capture_runner: float = 0.6
    giveback_pct: float = 1.5
    max_age_seconds: float = 30.0
    allow_assumption_only: bool = False

    def __post_init__(self):
        nums = [self.arm_pct, self.runner_pct, self.capture_low, self.capture_mid,
                self.capture_runner, self.giveback_pct, self.max_age_seconds]
        if not all(math.isfinite(x) for x in nums):
            raise ValueError("NONFINITE_PARAMETER")
        if (self.arm_basis not in {"net", "raw"} or
                self.floor_mode not in {"capture_ratio", "giveback_pct"} or
                not 0 < self.arm_pct < 4 < self.runner_pct or
                not 0 <= self.capture_low <= self.capture_mid <= self.capture_runner <= 1 or
                self.giveback_pct <= 0 or self.max_age_seconds <= 0):
            raise ValueError("INVALID_CANDIDATE_PARAMETERS")


def _time(value):
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("TIMESTAMP_MUST_HAVE_TIMEZONE")
    return result.astimezone(timezone.utc)


def _id(*parts):
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:24]


def initialize(position):
    p = deepcopy(position)
    if not p.get("position_id"):
        raise ValueError("POSITION_ID_REQUIRED")
    p.setdefault("protection_state", "UNARMED")
    if p["protection_state"] not in STATES:
        raise ValueError("INVALID_PROTECTION_STATE")
    for field in ("armed_at_utc", "armed_generation_id", "peak_executable_net_pnl_usdt",
                  "peak_executable_net_return_pct", "peak_executable_price",
                  "peak_executable_at_utc", "last_executable_net_pnl_usdt",
                  "last_executable_price", "last_execution_evidence_at_utc",
                  "runner_entered_at_utc", "exit_pending_since_utc", "exit_pending_reason",
                  "last_transition_id", "last_observation_at_utc"):
        p.setdefault(field, None)
    p.setdefault("protected_net_pnl_floor_usdt", 0.0)
    p.setdefault("protected_net_return_floor_pct", 0.0)
    p.setdefault("protected_floor_basis_cash_usdt", None)
    p.setdefault("state_version", 0)
    p.setdefault("protection_incidents", [])
    p.setdefault("protection_engine_version", ENGINE_VERSION)
    # Historical raw MFE stays available to research, never seeds executable peaks.
    return p


def _incident(p, kind, now, generation, execution, reason):
    key = _id(p["position_id"], p.get("armed_generation_id"),
              p.get("last_transition_id"), kind)
    if any(item["incident_id"] == key for item in p["protection_incidents"]):
        return key
    p["protection_incidents"].append({
        "incident_id": key, "position_id": p["position_id"], "asset": p.get("asset"),
        "run_id": execution.get("run_id"), "generation_id": generation,
        "root_cause_class": kind, "incident_type": "MISSED_PROFIT_EXIT" if
        kind in {"PROTECTION_GAP_THROUGH", "MISSED_OBSERVATION", "DECISION_NOT_TRIGGERED",
                 "EXIT_DECISION_EXECUTION_FAILED", "PERSISTENCE_FAILED",
                 "CAS_OR_GENERATION_REJECTED", "UNVERIFIABLE"} else kind,
        "armed_at": p.get("armed_at_utc"),
        "peak_executable_net_pnl": p.get("peak_executable_net_pnl_usdt"),
        "peak_executable_at": p.get("peak_executable_at_utc"),
        "protected_floor": p["protected_net_pnl_floor_usdt"],
        "last_valid_observation_at": p.get("last_execution_evidence_at_utc"),
        "incident_detected_at": now,
        "reference_price_then": p.get("last_executable_price"),
        "reference_price_now": execution.get("execution_price"),
        "execution_evidence": deepcopy(execution), "reason": reason,
        "decision_result": "HOLD", "execution_result": "NOT_EXECUTED",
        "persistence_result": "NOT_ATTEMPTED", "readback_result": "NOT_VERIFIED",
        "recovered": False, "recovered_at": None,
    })
    return key


def record_incident(position, kind, now, generation, execution=None, reason=None):
    """Record caller-observed failures without fabricating fills or recovery."""
    p = initialize(position)
    _time(now)
    _incident(p, kind, now, generation, execution or {}, reason)
    return p


def _transition(p, state, now, generation):
    previous = p["protection_state"]
    allowed = {"UNARMED": {"PROFIT_PROTECTION_ARMED"},
               "PROFIT_PROTECTION_ARMED": {"RUNNER", "EXIT_PENDING"},
               "RUNNER": {"EXIT_PENDING"}, "EXIT_PENDING": {"CLOSED"}, "CLOSED": set()}
    if state not in allowed[previous]:
        raise ValueError("ILLEGAL_PROTECTION_TRANSITION")
    p["state_version"] += 1
    p["last_transition_id"] = _id(p["position_id"], previous, state,
                                   p["state_version"], generation, now)
    p["protection_state"] = state


def _valid(execution, now, params):
    scope = execution.get("evidence_scope", "VERIFIED_BOOK")
    assumption = scope == "ASSUMPTION_ONLY" and params.allow_assumption_only
    if scope not in {"VERIFIED_BOOK", "ASSUMPTION_ONLY"}:
        return False
    if execution.get("status") != ("ASSUMPTION_ONLY" if assumption else "VALID"):
        return False
    if scope == "ASSUMPTION_ONLY" and not assumption:
        return False
    if scope == "VERIFIED_BOOK" and execution.get("execution_verified") is not True:
        return False
    try:
        age = (_time(now) - _time(execution["source_timestamp"])).total_seconds()
        values = [float(execution[k]) for k in
                  ("executable_net_pnl_usdt", "executable_net_return_pct", "execution_price",
                   "quantity", "total_invested_cash")]
        return (-2 <= age <= params.max_age_seconds and all(math.isfinite(v) for v in values)
                and values[2] > 0 and values[3] > 0 and values[4] > 0
                and (assumption or execution.get("full_quantity_verified") is True))
    except (KeyError, ValueError, TypeError):
        return False


def _runner_evidence_valid(signal, now, params):
    """Fresh means provider evidence age, never just a caller's fresh boolean."""
    if not signal.get("source") or not signal.get("evidence_id"):
        return False
    try:
        source = _time(signal["source_timestamp"])
        fetched = _time(signal["fetched_at"])
        current = _time(now)
        return (-2 <= (current - source).total_seconds() <= params.max_age_seconds and
                source <= fetched <= current and signal.get("fresh") is True)
    except (KeyError, ValueError, TypeError):
        return False


def advance(position, execution, signal, now, generation, params=None):
    """Return (new position, decision); no fill, event, or live state is written.

The estimate contract requires full-size fresh verified book evidence. Replay may
explicitly opt into ASSUMPTION_ONLY; such state is marked and cannot close through
complete_exit. Fresh runner signals are explicit positive facts, never UNKNOWN.
"""
    params = params or ProtectionParams()
    p = initialize(position)
    _time(now)
    decision = {"action": "HOLD", "reason": "PROTECTION_NOT_TRIGGERED",
                "engine_version": ENGINE_VERSION, "generation_id": generation,
                "position_id": p["position_id"], "evidence_scope": execution.get("evidence_scope")}
    if p["protection_state"] == "CLOSED":
        decision["reason"] = "ALREADY_CLOSED"
        return p, decision
    if p.get("last_observation_at_utc") and _time(now) <= _time(p["last_observation_at_utc"]):
        decision["reason"] = "STALE_OR_DUPLICATE_OBSERVATION"
        return p, decision
    if not _valid(execution, now, params):
        _incident(p, "DATA_DEGRADED", now, generation, execution, "EXECUTION_EVIDENCE_UNKNOWN")
        decision["reason"] = "EXECUTION_EVIDENCE_UNKNOWN"
        return p, decision
    pnl = float(execution["executable_net_pnl_usdt"])
    ret = float(execution["executable_net_return_pct"])
    cash = float(execution["total_invested_cash"])
    qty = float(execution["quantity"])
    previous_cash = p.get("protection_cash_basis_usdt")
    old_price = p.get("last_executable_price")
    old_at = p.get("last_execution_evidence_at_utc")
    if (previous_cash is not None and cash > previous_cash + 1e-8 and
            p["protection_state"] != "UNARMED" and
            signal.get("add_preflight_verified") is not True):
        p["protection_add_review_required"] = True
        _incident(p, "ADD_PROTECTION_MIGRATION_REQUIRED", now, generation, execution,
                  "UNVERIFIED_ADD_MUST_NOT_CREATE_MATHEMATICAL_SELL")
    p.update(last_executable_net_pnl_usdt=pnl,
             last_executable_price=float(execution["execution_price"]),
             last_execution_evidence_at_utc=execution["source_timestamp"],
             last_observation_at_utc=now, protection_cash_basis_usdt=cash,
             total_effective_quantity=qty, total_invested_cash=cash)
    if execution.get("evidence_scope") == "ASSUMPTION_ONLY":
        p["protection_evidence_scope"] = "ASSUMPTION_ONLY"
    if p.get("peak_executable_net_pnl_usdt") is None or pnl > p["peak_executable_net_pnl_usdt"]:
        p.update(peak_executable_net_pnl_usdt=pnl, peak_executable_net_return_pct=ret,
                 peak_executable_price=float(execution["execution_price"]),
                 peak_executable_at_utc=execution["source_timestamp"])
    for item in p["protection_incidents"]:
        if item["root_cause_class"] == "DATA_DEGRADED" and not item["recovered"]:
            item.update(recovered=True, recovered_at=now)
    arm_value = ret if params.arm_basis == "net" else signal.get("observed_price_return_pct")
    if (p["protection_state"] == "UNARMED" and isinstance(arm_value, (int, float)) and
            math.isfinite(arm_value) and arm_value >= params.arm_pct and pnl > 0):
        _transition(p, "PROFIT_PROTECTION_ARMED", now, generation)
        p.update(armed_at_utc=now, armed_generation_id=generation)
    if p["protection_state"] == "UNARMED":
        return p, decision
    peak_pct = p["peak_executable_net_return_pct"]
    runner_ok = (ret >= params.runner_pct and _runner_evidence_valid(signal, now, params) and
                 all(signal.get(k) is True for k in
                     ("btc_relative_positive", "relative_1h_positive", "relative_4h_positive",
                      "acceleration_positive", "liquidity_valid")))
    if p["protection_state"] == "PROFIT_PROTECTION_ARMED" and runner_ok:
        _transition(p, "RUNNER", now, generation)
        p["runner_entered_at_utc"] = now
        p["runner_qualification_evidence"] = deepcopy(signal)
    ratio = (params.capture_runner if p["protection_state"] == "RUNNER" else
             params.capture_mid if peak_pct >= 4 else params.capture_low)
    proposal = (p["peak_executable_net_pnl_usdt"] * ratio if params.floor_mode == "capture_ratio"
                else p["peak_executable_net_pnl_usdt"] - cash * params.giveback_pct / 100)
    if proposal > p["protected_net_pnl_floor_usdt"]:
        p["protected_net_pnl_floor_usdt"] = max(0.0, proposal)
        # Freeze the cash basis each time the absolute floor actually tightens.
        # ADD alone cannot dilute this recorded floor percentage; the absolute
        # USDT floor is the enforceable profit protection strength.
        p["protected_floor_basis_cash_usdt"] = cash
        p["protected_net_return_floor_pct"] = p["protected_net_pnl_floor_usdt"] / cash * 100
    # Previously unreviewed external ADD remains held until net profit recovers to
    # the preserved floor; never sells solely due to changed cash/quantity basis.
    if p.get("protection_add_review_required"):
        if pnl >= p["protected_net_pnl_floor_usdt"]:
            p["protection_add_review_required"] = False
        else:
            decision["reason"] = "ADD_PROTECTION_REVIEW_REQUIRED"
            return p, decision
    if pnl <= p["protected_net_pnl_floor_usdt"]:
        if p["protection_state"] != "EXIT_PENDING":
            _transition(p, "EXIT_PENDING", now, generation)
            p.update(exit_pending_since_utc=now, exit_pending_reason="PROTECTED_NET_FLOOR_BREACHED")
        if pnl <= 0:
            key = _incident(p, "PROTECTION_GAP_THROUGH", now, generation, execution,
                            "NET_NONPOSITIVE_NO_AUTOMATIC_LOSS_EXIT")
            incident = next(i for i in p["protection_incidents"] if i["incident_id"] == key)
            incident.update(reference_price_then=old_price, last_valid_observation_at=old_at)
            decision["reason"] = "MISSED_PROFIT_EXIT_NET_NONPOSITIVE"
        else:
            decision.update(action="SELL_INTENT", reason="PROTECTED_NET_FLOOR_BREACHED")
    elif p["protection_state"] == "EXIT_PENDING":
        decision.update(action="SELL_INTENT" if pnl > 0 else "HOLD",
                        reason="PERSISTENT_EXIT_PENDING")
    decision["transition_id"] = p.get("last_transition_id")
    return p, decision


def check_add(position, before_execution, after_execution, now, params=None):
    """Preflight projected full-position execution before authorizing an ADD."""
    params = params or ProtectionParams()
    p = initialize(position)
    if not _valid(before_execution, now, params) or not _valid(after_execution, now, params):
        return {"allowed": False, "reason": "EXECUTION_EVIDENCE_UNKNOWN"}
    if (p["protection_state"] != "UNARMED" and
            after_execution["executable_net_pnl_usdt"] < p["protected_net_pnl_floor_usdt"]):
        return {"allowed": False, "reason": "PROTECTION_FLOOR_WOULD_BE_BREACHED"}
    return {"allowed": True, "reason": "PROTECTION_PRESERVED",
            "cash_after": after_execution["total_invested_cash"],
            "quantity_after": after_execution["quantity"]}


def complete_exit(position, receipt, now, generation):
    """Close only after caller's valid shadow execution receipt, never a quote.

Receipt admission is a structural check, not proof of an external execution. The
caller must atomically persist its shadow SELL and this transition with CAS.
"""
    p = initialize(position)
    if p["protection_state"] == "CLOSED":
        return p, {"action": "NOOP", "reason": "ALREADY_CLOSED"}
    if p["protection_state"] != "EXIT_PENDING":
        raise ValueError("EXIT_NOT_PENDING")
    if (p.get("protection_evidence_scope") == "ASSUMPTION_ONLY" or
            receipt.get("status") != "SHADOW_EXECUTED" or
            receipt.get("position_id") != p["position_id"] or
            receipt.get("generation_id") != generation or
            receipt.get("transition_id") != p["last_transition_id"] or
            not receipt.get("execution_id") or receipt.get("full_quantity") is not True or
            receipt.get("evidence_scope") != "VERIFIED_BOOK" or
            not isinstance(receipt.get("net_pnl_usdt"), (int, float)) or
            not math.isfinite(receipt["net_pnl_usdt"]) or receipt["net_pnl_usdt"] <= 0):
        raise ValueError("INVALID_OR_NONPOSITIVE_SHADOW_EXECUTION_RECEIPT")
    _time(now)
    _transition(p, "CLOSED", now, generation)
    p.update(closed_at_utc=now, closed_execution_id=receipt["execution_id"])
    return p, {"action": "CLOSED_TRANSITION", "transition_id": p["last_transition_id"],
               "execution_id": receipt["execution_id"], "persist_verified": False}
