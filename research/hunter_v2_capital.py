"""Candidate-only V2 capital assessment. Never imported by the live trader.

Thresholds are PARAMETER_CANDIDATE, not approved trading policy. Assessment is
pure: no ledger, cash, position or execution mutation. Tail stress calibration
is reported separately from actual market/systemic admission controls.
"""
from __future__ import annotations

import copy
import datetime as dt
import math

MODEL_VERSION = "v2-capital-candidate-1"
TOTAL_CAP = 20_000.0
ORDINARY_CAP = 17_000.0
RESERVE_CAP = 3_000.0
DEFAULT_PARAMS = {
    "version": "PARAMETER_CANDIDATE_1",
    "max_evidence_age_seconds": 300,
    "market_utilization": {"RISK_OFF": .60, "NEUTRAL": .75,
                           "CONSTRUCTIVE": .85, "STRONG": .95},
    "strategic_min_rr": 2.6,
    "strategic_min_btc_rel_4h": .5,
    "strategic_min_acceleration": 0.0,
}


def number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def timestamp(value):
    try:
        result = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result if result.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def assess_capital(portfolio, proposal, evidence, params=None):
    """Return admission, independent reasons, provenance and tranche allocation.

    Inputs: canonical open_positions[].tranches[].notional_usdt (legacy notional supported); cash_usdt must be
    available settled cash, not account equity. Every admission requires fresh
    market/risk/concentration/marginal PASS evidence. Reserve additionally
    requires all eight qualification domains. Evidence rows need status PASS,
    source, source_timestamp and evidence_id. Execution must certify full size.
    Caller supplies decision_timestamp; future timestamps are rejected. Legacy
    unclassified tranches are counted as ordinary, never presumed strategic.
    """
    evidence = {key: value if isinstance(value, dict) else {}
                for key, value in evidence.items()}
    cfg = dict(DEFAULT_PARAMS)
    if params:
        cfg.update(params)
    reasons, warnings, audit = [], [], {}
    now = timestamp(proposal.get("decision_timestamp"))
    max_age = number(cfg.get("max_evidence_age_seconds"))
    if now is None or max_age is None or max_age <= 0:
        reasons.append("EVIDENCE_FRESHNESS_UNKNOWN")

    def check(name):
        row = evidence.get(name)
        row = row if isinstance(row, dict) else {}
        asof = timestamp(row.get("source_timestamp"))
        age = (now - asof).total_seconds() if now and asof else None
        passed = (row.get("status") == "PASS" and bool(row.get("source"))
                  and bool(row.get("evidence_id")) and age is not None
                  and max_age is not None and 0 <= age <= max_age)
        audit[name] = {"passed": passed, "status": row.get("status", "UNKNOWN"),
                       "source": row.get("source"), "source_timestamp": row.get("source_timestamp"),
                       "evidence_id": row.get("evidence_id"), "age_seconds": age,
                       "values": copy.deepcopy(row)}
        return passed

    amount = number(proposal.get("notional_usdt"))
    cash = number(portfolio.get("cash_usdt"))
    used = ordinary = reserve = 0.0
    unclassified = 0
    positions = portfolio.get("open_positions")
    if not isinstance(positions, list):
        reasons.append("PORTFOLIO_EXPOSURE_UNKNOWN")
        positions = []
    ids = set()
    for position in positions:
        if not isinstance(position, dict) or not isinstance(position.get("tranches"), list):
            reasons.append("PORTFOLIO_EXPOSURE_UNKNOWN")
            continue
        for tranche in position["tranches"]:
            if not isinstance(tranche, dict):
                reasons.append("PORTFOLIO_EXPOSURE_UNKNOWN")
                continue
            value = number(tranche.get("notional_usdt")) if "notional_usdt" in tranche else number(tranche.get("notional"))
            if "notional_usdt" in tranche and "notional" in tranche:
                legacy = number(tranche.get("notional"))
                if legacy is None or value is None or legacy != value:
                    reasons.append("TRANCHE_NOTIONAL_CONFLICT")
            if value is None or value < 0:
                reasons.append("PORTFOLIO_EXPOSURE_UNKNOWN")
                continue
            tranche_id = tranche.get("tranche_id")
            if tranche_id and tranche_id in ids:
                reasons.append("DUPLICATE_TRANCHE_ID")
            if tranche_id:
                ids.add(tranche_id)
            used += value
            if tranche.get("capital_class") == "STRATEGIC_RESERVE":
                reserve += value
                if not tranche.get("qualification_evidence_id"):
                    reasons.append("EXISTING_RESERVE_QUALIFICATION_UNKNOWN")
            else:
                ordinary += value
                if tranche.get("capital_class") != "ORDINARY":
                    unclassified += 1
    if reserve > RESERVE_CAP:
        reasons.append("STRATEGIC_RESERVE_CAP_REJECT")
    if amount is None or amount <= 0 or proposal.get("action") not in ("BUY", "ADD") or not proposal.get("asset"):
        reasons.append("INVALID_PROPOSAL")
    valid_amount = amount if amount is not None and amount > 0 else 0.0
    after = used + valid_amount
    if after > TOTAL_CAP:
        reasons.append("TOTAL_20K_CAP_REJECT")
    if cash is None or cash < valid_amount:
        reasons.append("AVAILABLE_CASH_REJECT")
    if not check("market"):
        reasons.append("MARKET_REGIME_EVIDENCE_UNKNOWN")
    regime = (evidence.get("market") or {}).get("regime")
    utilization = number(cfg.get("market_utilization", {}).get(regime))
    market_cap = TOTAL_CAP * utilization if utilization is not None and 0 <= utilization <= 1 else None
    if market_cap is None:
        reasons.append("MARKET_REGIME_EVIDENCE_UNKNOWN")
    elif after > market_cap:
        reasons.append("MARKET_REGIME_CAP_REJECT")
    if not check("systemic") or (evidence.get("systemic") or {}).get("freeze") is not False:
        reasons.append("SYSTEMIC_RISK_FREEZE")
    if not check("circuit_breaker") or (evidence.get("circuit_breaker") or {}).get("freeze") is not False:
        reasons.append("CIRCUIT_BREAKER_FREEZE")
    if not check("marginal_edge"):
        reasons.append("MARGINAL_EDGE_REJECT")
    if not check("concentration"):
        reasons.append("CONCENTRATION_REJECT")

    execution = evidence.get("execution") or {}
    covered = number(execution.get("verified_notional_usdt"))
    executable = (check("execution") and execution.get("full_size_valid") is True
                  and covered is not None and covered >= valid_amount
                  and execution.get("asset") == proposal.get("asset")
                  and execution.get("action") == proposal.get("action"))
    if not executable:
        reasons.append("EXECUTION_EVIDENCE_UNKNOWN")
    strategic = proposal.get("use_strategic_reserve") is True
    strategic_pass = False
    qualification = {}
    if strategic:
        domains = ("identity", "execution", "liquidity", "scenario", "btc_relative", "freshness", "concentration", "supply")
        qualification = {name: check(name) for name in domains}
        execution = evidence.get("execution") or {}
        covered = number(execution.get("verified_notional_usdt"))
        qualification["full_size_execution"] = (
            execution.get("full_size_valid") is True and covered is not None and covered >= valid_amount
            and execution.get("asset") == proposal.get("asset")
            and execution.get("action") == proposal.get("action"))
        rr = number((evidence.get("scenario") or {}).get("rr"))
        relative = evidence.get("btc_relative") or {}
        r4, accel = number(relative.get("return_4h_pct")), number(relative.get("acceleration"))
        rr_min, rel_min, accel_min = (number(cfg.get(k)) for k in (
            "strategic_min_rr", "strategic_min_btc_rel_4h", "strategic_min_acceleration"))
        qualification["scenario_rr"] = rr is not None and rr_min is not None and rr >= rr_min
        qualification["relative_momentum"] = (r4 is not None and rel_min is not None and r4 >= rel_min
                                               and accel is not None and accel_min is not None and accel > accel_min)
        identity = evidence.get("identity") or {}
        qualification["identity_verified"] = (identity.get("verified") is True
                                                and identity.get("mismatch") is False)
        qualification["supply_material_risk"] = (evidence.get("supply") or {}).get("material_risk") is False
        strategic_pass = all(qualification.values())
        if not strategic_pass:
            reasons.append("STRATEGIC_RESERVE_NOT_QUALIFIED")
    elif after > ORDINARY_CAP or ordinary + valid_amount > ORDINARY_CAP:
        reasons.append("ORDINARY_17K_CAP_REJECT")
        reasons.append("STRATEGIC_RESERVE_NOT_QUALIFIED")

    # Reserve is the total-exposure band above17K, not free capacity ordinary
    # trades can reuse while reserve remains deployed. This conservatively also
    # protects unclassified legacy tranches; existing reserve cannot silently
    # turn the next ordinary BUY into reserve usage.
    ordinary_part = min(valid_amount, max(0.0, ORDINARY_CAP - used))
    reserve_part = valid_amount - ordinary_part
    if reserve + reserve_part > RESERVE_CAP:
        reasons.append("STRATEGIC_RESERVE_CAP_REJECT")
    tail = number((evidence.get("tail_stress") or {}).get("calibration_cap_usdt"))
    if tail is not None and after > tail:
        warnings.append("TAIL_STRESS_WARNING")
    reasons = list(dict.fromkeys(reasons))
    return {"allowed": not reasons, "reject_codes": reasons, "warnings": warnings,
            "model_version": MODEL_VERSION, "parameter_version": cfg.get("version"),
            "parameter_status": "PARAMETER_CANDIDATE", "input_version": portfolio.get("generation_id"),
            "decision_timestamp": proposal.get("decision_timestamp"),
            "capital_authority": "NONE_SHADOW_ONLY", "real_trading_enabled": False,
            "total_cap_usdt": TOTAL_CAP, "ordinary_cap_usdt": ORDINARY_CAP, "reserve_cap_usdt": RESERVE_CAP,
            "used_usdt": used, "ordinary_used_usdt": ordinary, "reserve_used_usdt": reserve,
            "unclassified_tranches_count": unclassified, "used_after_usdt": after,
            "market_cap_usdt": market_cap, "available_cash_usdt": cash,
            "parameter_values": cfg, "execution_full_size_verified": executable,
            "strategic_qualified": strategic_pass, "qualification": qualification, "evidence_audit": audit,
            "proposed_allocation": {"ordinary_usdt": ordinary_part, "reserve_usdt": reserve_part}
            if not reasons else None}
