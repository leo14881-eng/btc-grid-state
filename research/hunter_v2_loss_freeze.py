"""V2-only loss freeze policy. Shadow accounting; no order or persistence APIs.

The shared tail-risk module remains the V1 policy and systemic safety authority.
Legacy circuits finish naturally before this policy activates. A qualified loss
episode is separate from loss accounting and never inferred from WATCH.
"""
import copy
import datetime as dt
import hashlib
try:
    from research import hunter_tail_risk as tail
except ModuleNotFoundError as exc:
    if exc.name != "research":
        raise
    import hunter_tail_risk as tail

POLICY = "V2_LOSS_AND_MARKET_V1"


def _fresh(value, now):
    try:
        return 0 <= (now - tail.parse(value)).total_seconds() <= 900
    except (TypeError, ValueError, OverflowError):
        return False


def _number(value):
    return None if isinstance(value, bool) else tail.finite(value)


def market_confirmation(evidence, now):
    """B requires one complete, current observation, not a HIGH label."""
    e = evidence or {}
    btc, breadth = e.get("btc_short") or {}, e.get("breadth") or {}
    values = [_number(btc.get(k)) for k in
              ("return_5m_pct", "return_15m_pct", "return_1h_pct")]
    neg, loss5 = (_number(breadth.get(k)) for k in
                  ("negative_fraction", "loss_5pct_fraction"))
    sample = _number(breadth.get("sample_count"))
    liquidity, stable = e.get("liquidity") or {}, e.get("stablecoins") or {}
    books = _number(liquidity.get("fresh_sample_count"))
    prices = stable.get("prices") or {}
    loss10 = _number(breadth.get("loss_10pct_fraction"))
    catastrophic = _number(liquidity.get("catastrophic_fraction"))
    deviation = _number(stable.get("max_deviation_pct"))
    complete = (
        bool(e.get("observation_id")) and e.get("scan_fresh") is True
        and e.get("missing_or_stale") == []
        and not e.get("btc_fetch_error")
        and all(_fresh(ts, now) for ts in
                (e.get("observed_at_utc"), e.get("scan_as_of_utc"),
                 btc.get("last_completed_close_utc")))
        and all(v is not None for v in values)
        and sample is not None and sample >= 1
        and neg is not None and 0 <= neg <= 1
        and loss5 is not None and 0 <= loss5 <= neg
        and loss10 is not None and 0 <= loss10 <= loss5
        and books is not None and books >= 5
        and catastrophic is not None and 0 <= catastrophic <= 1
        and isinstance(prices, dict) and len(prices) >= 2
        and all(_number(v) is not None and _number(v) > 0 for v in prices.values())
        and deviation is not None and deviation >= 0 and not stable.get("fetch_error")
    )
    rapid = bool(complete and any(v <= threshold for v, threshold in
                                 zip(values, (-2.5, -4.0, -6.0))))
    broad = bool(complete and (neg >= .85 or loss5 >= .50))
    return {"complete": bool(complete), "btc_rapid_drop": rapid,
            "broad_altcoin_weakness": broad, "confirmed": rapid and broad,
            "observation_id": e.get("observation_id")}


def _activate(state, now):
    if state.get("loss_control_v2", {}).get("policy") == POLICY:
        return True
    legacy = state.get("circuit_breaker") or {}
    # Do not relabel/clear an existing loss circuit or manufacture historic B.
    if legacy.get("status") not in (None, "NORMAL") or (
            tail.finite(legacy.get("quarantined_cash_usdt")) or 0) > 0:
        return False
    state["loss_control_v2"] = {
        "policy": POLICY, "activated_at_utc": now.isoformat(),
        "legacy_completion": copy.deepcopy(legacy),
        "consecutive_loss_exits": int(legacy.get("consecutive_loss_exits") or 0),
        "loss_events": copy.deepcopy(legacy.get("loss_events") or []),
        "exit_sequence": 0, "episode_consumed_sequence": 0,
        "pending_principal_usdt": 0.0,
    }
    return True


def _refresh(state, now):
    control = state["loss_control_v2"]
    total = 0.0
    for event in control["loss_events"]:
        try:
            age = (now - tail.parse(event["at_utc"])).total_seconds()
            pnl = float(event["net_pnl_usdt"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= age <= 6 * 3600 and pnl < 0:
            total -= pnl
    control["rolling_realized_loss_usdt"] = round(total, 2)
    control["evaluated_at_utc"] = now.isoformat()
    control["loss_threshold_met"] = (
        control["consecutive_loss_exits"] >= 2 or total >= 1000)
    return control


def _evaluate(state, now):
    if not _activate(state, now):
        return False
    control = _refresh(state, now)
    evidence = (state.get("systemic_risk") or {}).get("evidence") or {}
    confirmation = market_confirmation(evidence, now)
    control["market_confirmation"] = confirmation
    if not (control["loss_threshold_met"] and confirmation["confirmed"]
            and control["exit_sequence"] > control["episode_consumed_sequence"]):
        return True
    old = state.get("loss_freeze_episode") or {}
    active = old.get("status") in ("TRIPPED", "RECOVERING")
    if old and not active:
        state.setdefault("loss_freeze_episode_archive", []).append(copy.deepcopy(old))
    principal = round(control["pending_principal_usdt"] +
                      (float(old.get("quarantined_cash_usdt") or 0) if active else 0), 2)
    identity = f'{control["activated_at_utc"]}:{control["exit_sequence"]}:{confirmation["observation_id"]}'
    episode = {
        "episode_id": hashlib.sha256(identity.encode()).hexdigest(),
        "policy": POLICY, "status": "TRIPPED",
        "qualified_at_utc": now.isoformat(),
        "qualification": {"loss": copy.deepcopy({k: control[k] for k in
            ("consecutive_loss_exits", "rolling_realized_loss_usdt", "exit_sequence")}),
            "market": copy.deepcopy(evidence)},
        "quarantined_cash_usdt": principal, "quarantine_base_usdt": principal,
        "recovery_observations": 0,
        "last_loss_exit_at_utc": now.isoformat(),
        "last_loss_exit_observation_id": confirmation["observation_id"],
        "recovery_seen_observation_ids": list(old.get("recovery_seen_observation_ids") or []),
        "capital_authority": "NONE_SHADOW_ONLY",
    }
    if active:
        episode["prior_qualifications"] = copy.deepcopy(old.get("prior_qualifications") or []) + [copy.deepcopy(old["qualification"])]
    state["loss_freeze_episode"] = episode
    control["episode_consumed_sequence"] = control["exit_sequence"]
    control["pending_principal_usdt"] = 0.0
    return True


def _view(state):
    return {**state, "circuit_breaker": state.get("loss_freeze_episode") or {}}


def risk_blocks_new(state, now):
    if not _evaluate(state, now):
        return tail.risk_blocks_new(state)
    return tail.risk_blocks_new(_view(state))


def circuit_for_admission(state):
    if (state.get("loss_control_v2") or {}).get("policy") == POLICY:
        return state.get("loss_freeze_episode") or {}
    return state.get("circuit_breaker") or {}


def quarantine(state):
    if (state.get("loss_control_v2") or {}).get("policy") != POLICY:
        row = state.get("circuit_breaker") or {}
    else:
        row = state.get("loss_freeze_episode") or {}
    return max(0.0, tail.finite(row.get("quarantined_cash_usdt")) or 0.0)


def record_loss_exit(state, pnl, reason, released_notional, now, cfg):
    if not _activate(state, now):
        return tail.record_loss_exit(state, pnl, reason, released_notional, now, cfg)
    control = state["loss_control_v2"]
    if pnl >= 0:
        control["consecutive_loss_exits"] = 0
    else:
        control["exit_sequence"] += 1
        control["consecutive_loss_exits"] += 1
        control["loss_events"].append({
            "at_utc": now.isoformat(), "net_pnl_usdt": round(float(pnl), 2),
            "reason": reason, "released_notional_usdt": round(float(released_notional), 2),
        })
        control["pending_principal_usdt"] = round(
            control["pending_principal_usdt"] + float(released_notional), 2)
    _evaluate(state, now)


def update_risk_controls(state, evidence, now, cfg):
    if not _activate(state, now):
        systemic = tail.update_risk_controls(state, evidence, now, cfg)
        _activate(state, now)
        return systemic
    view = _view(state)
    systemic, new_observation = tail.update_systemic_risk(view, evidence, now, cfg)
    state["systemic_risk"] = systemic
    _evaluate(state, now)
    episode = state.get("loss_freeze_episode")
    # Use the existing durable-ID and 300-second independent recovery logic.
    # Additional strict freshness forbids future timestamps for V2 episodes.
    if episode and market_confirmation(evidence, now)["complete"]:
        tail.advance_circuit_breaker(_view(state), systemic, evidence, now, cfg, new_observation)
    return systemic
