"""Shared V1/V2 loss freeze policy. Shadow accounting; no order or persistence APIs.

The tail-risk module remains the independent systemic safety authority.
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

POLICY = "LOSS_AND_MARKET_V1"


def _fresh(value, now):
    try:
        return 0 <= (now - tail.parse(value)).total_seconds() <= 900
    except (TypeError, ValueError, OverflowError):
        return False


def _number(value):
    return None if isinstance(value, bool) else tail.finite(value)


def market_confirmation(evidence, now):
    """B requires one complete, current observation, not a HIGH label."""
    if not isinstance(evidence, dict):
        evidence = {}
    e = evidence
    if any(not isinstance(e.get(k, {}), dict) for k in ("btc_short", "breadth", "liquidity", "stablecoins")):
        e = {}
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
    return {"state": "CONFIRMED_BAD" if rapid and broad else ("NOT_BAD" if complete else "UNKNOWN"),
            "complete": bool(complete), "btc_rapid_drop": rapid,
            "broad_altcoin_weakness": broad, "confirmed": rapid and broad,
            "observation_id": e.get("observation_id")}


def _activate(state, now):
    if state.get("loss_control", {}).get("policy") == POLICY:
        return True
    legacy = state.get("circuit_breaker") or {}
    # Do not relabel/clear an existing loss circuit or manufacture historic B.
    if legacy.get("status") not in (None, "NORMAL") or (
            tail.finite(legacy.get("quarantined_cash_usdt")) or 0) > 0:
        return False
    state["loss_control"] = {
        "policy": POLICY, "activated_at_utc": now.isoformat(),
        "legacy_completion": copy.deepcopy(legacy),
        "consecutive_loss_exits": int(legacy.get("consecutive_loss_exits") or 0),
        "loss_events": copy.deepcopy(legacy.get("loss_events") or []),
        "exit_sequence": 0, "episode_consumed_sequence": 0, "streak_start_sequence": None,
        "pending_principal_usdt": 0.0,
    }
    return True


def _refresh(state, now):
    control = state["loss_control"]
    total = 0.0
    principal = 0.0
    for event in control["loss_events"]:
        try:
            age = (now - tail.parse(event["at_utc"])).total_seconds()
            pnl = float(event["net_pnl_usdt"])
        except (KeyError, TypeError, ValueError):
            continue
        in_window = 0 <= age <= 6 * 3600
        if in_window and pnl < 0:
            total -= pnl
        sequence = int(event.get("loss_sequence") or 0)
        in_streak = (control["consecutive_loss_exits"] >= 2
                     and control.get("streak_start_sequence") is not None
                     and sequence >= control["streak_start_sequence"])
        if (pnl < 0 and sequence > control["episode_consumed_sequence"]
                and (in_window or (age >= 0 and in_streak))):
            principal += float(event.get("released_notional_usdt") or 0)
    control["pending_principal_usdt"] = round(principal, 2)
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
    if (state.get("loss_control") or {}).get("policy") == POLICY:
        return state.get("loss_freeze_episode") or {}
    return state.get("circuit_breaker") or {}


def quarantine(state):
    if (state.get("loss_control") or {}).get("policy") != POLICY:
        row = state.get("circuit_breaker") or {}
    else:
        row = state.get("loss_freeze_episode") or {}
    return max(0.0, tail.finite(row.get("quarantined_cash_usdt")) or 0.0)


def _reset_recovery(episode, now, reason):
    episode["recovery_observations"] = 0
    episode["last_recovery_counted_at_utc"] = None
    episode["last_recovery_counted_observed_at_utc"] = None
    episode["recovery_wait_reason"] = reason
    episode["updated_at_utc"] = now.isoformat()


def _interrupt_unknown(episode, evidence, now):
    """New UNKNOWN breaks consecutive NORMALs; replay cannot alter recovery.

    Invalid source clocks may interrupt but never advance the source watermark
    or earn recovery credit. Older valid source observations are ignored.
    """
    e = evidence if isinstance(evidence, dict) else {}
    oid = e.get("observation_id")
    seen = list(episode.get("recovery_seen_observation_ids") or [])
    if oid and (oid in seen or oid in (
            episode.get("last_recovery_seen_observation_id"),
            episode.get("last_loss_exit_observation_id"))):
        return
    try:
        processing = [tail.parse(episode[k]) for k in
                      ("updated_at_utc", "last_recovery_seen_at_utc", "last_loss_exit_at_utc")
                      if episode.get(k)]
        if processing and now <= max(processing):
            return
    except (TypeError, ValueError):
        return
    try:
        observed = tail.parse(e.get("observed_at_utc"))
        source_times = [tail.parse(episode[k]) for k in
                        ("last_recovery_seen_observed_at_utc", "last_loss_exit_at_utc")
                        if episode.get(k)]
        if source_times and observed <= max(source_times):
            return
    except (TypeError, ValueError):
        observed = None
    if oid:
        seen.append(oid)
        episode["recovery_seen_observation_ids"] = seen
        episode["last_recovery_seen_observation_id"] = oid
    episode["last_recovery_seen_at_utc"] = now.isoformat()
    if observed is not None and observed <= now:
        episode["last_recovery_seen_observed_at_utc"] = observed.isoformat()
    _reset_recovery(episode, now, "LOSS_RECOVERY_REQUIRES_COMPLETE_NORMAL_OBSERVATIONS")


def control_snapshot(state):
    """Expose effective controls without rewriting the legacy portfolio object."""
    activated = (state.get("loss_control") or {}).get("policy") == POLICY
    effective = copy.deepcopy(circuit_for_admission(state))
    effective.setdefault("status", "NORMAL")
    effective.setdefault("quarantined_cash_usdt", 0.0)
    return {
        "circuit_breaker": effective,
        "circuit_breaker_source": "LOSS_FREEZE_EPISODE" if activated else "LEGACY_CIRCUIT",
        "legacy_circuit_breaker": copy.deepcopy(state.get("circuit_breaker")),
        "legacy_circuit_breaker_role": "INACTIVE_HISTORY" if activated else "ACTIVE_LEGACY_RECOVERY",
        "loss_control": copy.deepcopy(state.get("loss_control")),
        "loss_freeze_episode": copy.deepcopy(state.get("loss_freeze_episode")),
        "new_risk_blocked": tail.risk_blocks_new({**state, "circuit_breaker": effective}),
        "effective_quarantined_cash_usdt": quarantine(state),
    }


def record_loss_exit(state, pnl, reason, released_notional, now, cfg):
    if not _activate(state, now):
        return tail.record_loss_exit(state, pnl, reason, released_notional, now, cfg)
    control = state["loss_control"]
    if pnl >= 0:
        control["consecutive_loss_exits"] = 0
        control["streak_start_sequence"] = None
    else:
        episode = state.get("loss_freeze_episode") or {}
        if episode.get("status") in ("TRIPPED", "RECOVERING"):
            _reset_recovery(episode, now, "LOSS_RECOVERY_INTERRUPTED_BY_NEW_LOSS")
            episode["last_loss_exit_at_utc"] = now.isoformat()
            episode["last_loss_exit_observation_id"] = (
                state.get("systemic_risk") or {}).get("last_observation_id")
        control["exit_sequence"] += 1
        if control["consecutive_loss_exits"] == 0:
            control["streak_start_sequence"] = control["exit_sequence"]
        control["consecutive_loss_exits"] += 1
        control["loss_events"].append({
            "loss_sequence": control["exit_sequence"],
            "at_utc": now.isoformat(), "net_pnl_usdt": round(float(pnl), 2),
            "reason": reason, "released_notional_usdt": round(float(released_notional), 2),
        })
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
    # Additional strict freshness forbids future timestamps for qualified loss episodes.
    if episode and episode.get("status") in ("TRIPPED", "RECOVERING"):
        if market_confirmation(evidence, now)["complete"]:
            tail.advance_circuit_breaker(_view(state), systemic, evidence, now, cfg, new_observation)
        else:
            _interrupt_unknown(episode, evidence, now)
    return systemic
