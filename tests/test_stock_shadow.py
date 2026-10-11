# V3 frozen baseline: 2026-10-04 through 2026-11-04; strategy retuning disabled during forward sample window.
import importlib.util
import copy
import json
from pathlib import Path

P=Path("research/stock_shadow/stock_shadow_v1.py")
spec=importlib.util.spec_from_file_location("ss",P); ss=importlib.util.module_from_spec(spec); spec.loader.exec_module(ss)

def test_weighted_average_and_pnl_after_fees():
    p={"tranches":[{"price":100.0,"notional":1000.0},{"price":80.0,"notional":1000.0}]}
    assert 88 < ss.avg(p) < 89
    assert ss.net_pnl(p,90.0) > 0

def test_v1_has_no_global_position_cap():
    assert not hasattr(ss,"MAX_OPEN")
    assert ss.MAX_TRANCHES == 5

def test_profit_protection_is_profit_only():
    p={"tranches":[{"price":100.0,"notional":1000.0}]}
    assert ss.net_pct(p,90.0) < 0
    assert ss.ARM_NET_PCT > ss.PROFIT_FLOOR_NET_PCT


def test_security_type_filter_excludes_non_common_instruments():
    assert ss._plain_common_stock("Example Corporation Common Stock")
    assert not ss._plain_common_stock("Example ETF")
    assert not ss._plain_common_stock("Example Warrant")
    assert not ss._plain_common_stock("Example Preferred Stock")

def test_full_market_discovery_has_no_fixed_61_symbol_constant():
    assert not hasattr(ss, "STOCK_SYMBOLS")
    assert ss.MAX_REQUEST_TARGET_CHARS == 7000
    assert hasattr(ss,"MARKET_CACHE")
    assert ss.ALPACA_BARS_URL.startswith("https://data.alpaca.markets/")


def _m(price=50,dv=50_000_000,r5=4,r20=12,sma20=48,vol=2.5,volume_ratio=1.0,range_pos=0.6):
    return {"price":price,"avg_dollar_volume20":dv,"ret5":r5,"ret20":r20,"sma20":sma20,"daily_volatility20":vol,
            "volume_ratio20":volume_ratio,"range_position20":range_pos,"price_asof":"2026-10-05T04:00:00Z","refresh_received":True}

def test_low_price_is_not_rejected_by_price_alone():
    d=ss.entry_decision(_m(price=1.5,dv=100_000_000,r5=2,r20=7,sma20=1.45,vol=2.0,volume_ratio=1.8,range_pos=0.62))
    assert "PRICE_TOO_LOW" not in d["rejects"]
    assert "LOW_PRICE_INSUFFICIENT_LIQUIDITY" not in d["rejects"]

def test_low_price_requires_stronger_tradeability():
    assert "LOW_PRICE_INSUFFICIENT_LIQUIDITY" in ss.entry_decision(_m(price=1.5,dv=12_000_000))["rejects"]
    assert "LOW_DOLLAR_VOLUME" in ss.entry_decision(_m(dv=500_000))["rejects"]

def test_position_state_does_not_break_on_market_like_pullback():
    spy=_m(r20=-8); qqq=_m(r20=-9)
    m=_m(price=45,r5=-4,r20=-7,sma20=48,volume_ratio=1.1)
    assert ss.position_state_v2(m,spy,qqq)["state"] != "BROKEN"

def test_position_state_requires_structure_and_relative_weakness_to_break():
    spy=_m(r20=4); qqq=_m(r20=5)
    m=_m(price=40,r5=-8,r20=-12,sma20=48,volume_ratio=2.0)
    d=ss.position_state_v2(m,spy,qqq)
    assert d["trend_broken"] and d["relative_weak"] and d["state"]=="BROKEN"

def test_healthy_pullback_is_add_eligible_state():
    spy=_m(r20=3); qqq=_m(r20=4)
    m=_m(price=49,r5=-2,r20=8,sma20=50,volume_ratio=1.0)
    assert ss.position_state_v2(m,spy,qqq)["state"]=="HEALTHY_PULLBACK"

def test_selector_rejects_only_parabolic_chase():
    assert "PARABOLIC_5D" in ss.entry_decision(_m(r5=40,r20=70))["rejects"]

def test_selector_can_approve_liquid_relative_strength():
    spy=_m(r20=3); qqq=_m(r20=4)
    d=ss.entry_decision(_m(price=80,dv=200_000_000,r5=6,r20=18,sma20=75,vol=2.5),spy,qqq)
    assert d["ready"] is True
    assert d["score"] >= ss.MIN_SCORE
    assert "OUTPERFORMS_SPY_QQQ" in d["reasons"]


def test_selector_rejects_only_extreme_medium_term_overextension():
    assert "PARABOLIC_20D" in ss.entry_decision(_m(r5=10,r20=90,sma20=48))["rejects"]
    assert "PARABOLIC_SMA20_EXTENSION" in ss.entry_decision(_m(price=70,r5=10,r20=30,sma20=50))["rejects"]

def test_early_anomaly_can_enter_before_breakout():
    spy=_m(r20=2); qqq=_m(r20=3)
    d=ss.entry_decision(_m(price=51,dv=120_000_000,r5=2,r20=7,sma20=50,vol=2.0,volume_ratio=1.8,range_pos=0.62),spy,qqq)
    assert d["ready"] is True
    assert d["entry_structure"] == "EARLY_ACCUMULATION"
    assert "EARLY_VOLUME_ANOMALY" in d["reasons"]

def test_hybrid_engine_keeps_right_side_entry():
    spy=_m(r20=3); qqq=_m(r20=4)
    d=ss.entry_decision(_m(price=80,dv=200_000_000,r5=6,r20=18,sma20=75,vol=2.5),spy,qqq)
    assert d["ready"] is True
    assert d["entry_structure"] == "MOMENTUM_TREND"


def _load_position_monitor():
    p=Path("research/stock_shadow/stock_position_monitor.py")
    spec=importlib.util.spec_from_file_location("spm",p); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

def test_position_monitor_has_no_buy_or_discovery_capability():
    m=_load_position_monitor()
    assert not hasattr(m,"stock_universe")
    assert not hasattr(m,"entry_decision")
    assert m.MAX_REQUEST_TARGET_CHARS == 7000
    assert not hasattr(m,"BATCH_SIZE")

def test_position_monitor_parses_public_snapshot_price():
    m=_load_position_monitor()
    assert m._parse_price("$123.45") == 123.45
    assert m._parse_price("N/A") is None
    import inspect
    src=inspect.getsource(m.alpaca_snapshot_quotes)
    assert "data.alpaca.markets" in src
    assert "query1.finance.yahoo.com" not in src
    assert "api.nasdaq.com" not in src

def test_position_monitor_market_hours_gate():
    m=_load_position_monitor()
    from datetime import datetime, timezone, time
    regular={"date":"2026-10-05","open":time(9,30),"close":time(16,0)}
    assert m.market_open(datetime(2026,10,5,14,0,tzinfo=timezone.utc),regular) is True
    assert m.market_open(datetime(2026,10,5,21,0,tzinfo=timezone.utc),regular) is False
    early={"date":"2026-11-27","open":time(9,30),"close":time(13,0)}
    assert m.market_open(datetime(2026,11,27,17,30,tzinfo=timezone.utc),early) is True
    assert m.market_open(datetime(2026,11,27,18,30,tzinfo=timezone.utc),early) is False
    assert m.market_open(datetime(2026,10,4,14,0,tzinfo=timezone.utc),None) is False

def test_monitor_executes_v3_profit_protection_sell():
    m=_load_position_monitor()
    import inspect
    src=inspect.getsource(m.main)
    assert '"NET_PROFIT_GIVEBACK_V3"' in src
    assert 'del positions[s]' in src
    assert '"source":"POSITION_MONITOR_5M"' in src
    assert "profit_protection_signal" in src

def test_profit_protection_never_sells_at_a_loss():
    import inspect
    monitor_src=inspect.getsource(_load_position_monitor().main)
    assert "r>0 and r<=floor" in monitor_src
    assert 'if p["profit_protection_signal"]' in monitor_src

def test_broken_position_waits_for_profitable_rebound_exit():
    import inspect
    src=inspect.getsource(ss.main)
    assert 'if r > 0:' in src
    assert '"rebound_exit_pending_v3"' in src
    assert '"REBOUND_PROFIT_EXIT_AFTER_DETERIORATION_V3"' in src
    assert '"STRUCTURE_BROKEN_PROFIT_EXIT_V3"' in src

def test_position_state_separates_market_strength_from_trade_drawdown():
    market={"state":"STRONG"}
    p={"mae_net_pct":-11.0}
    d=ss.position_state_v3(p,market,-10.5)
    assert d["market_state"]=="STRONG"
    assert d["state"]=="DETERIORATING"
    p2={"mae_net_pct":-4.0}
    assert ss.position_state_v3(p2,market,-4.0)["state"]=="UNDERWATER"
    assert ss.position_state_v3({"mae_net_pct":-0.4},market,2.0)["state"]=="PROFITABLE"

def test_v3_add_requires_recovery_not_decline():
    import inspect
    src=inspect.getsource(ss.main)
    assert 'recovery["eligible"]' in src
    assert 'PULLBACK_RECOVERY_ADD_V3' in src
    assert 'pullback <= -3*n' not in src

def test_recovery_add_requires_price_and_relative_improvement():
    p={"swing_high_price":100.0,"pullback_low_price":90.0,"pullback_seen":True,
       "position_state_v2":{"market_relative20":-2.0}}
    m=_m(price=94,r5=-1,r20=5,sma20=93)
    ps={"state":"HEALTHY_PULLBACK","market_relative20":-1.0}
    assert ss.recovery_add_signal(p,m,ps)["eligible"] is True
    p2={"swing_high_price":100.0,"pullback_low_price":90.0,"pullback_seen":True,
        "position_state_v2":{"market_relative20":-1.0}}
    ps2={"state":"UNCERTAIN","market_relative20":-2.0}
    assert ss.recovery_add_signal(p2,m,ps2)["eligible"] is False

def test_broken_state_never_adds_even_if_price_bounces():
    p={"swing_high_price":100.0,"pullback_low_price":90.0,"pullback_seen":True,
       "position_state_v2":{"market_relative20":-3.0}}
    m=_m(price=95,r5=-2,r20=-10,sma20=100)
    ps={"state":"BROKEN","market_relative20":-2.0}
    assert ss.recovery_add_signal(p,m,ps)["eligible"] is False

def test_profit_floor_is_positive_and_tightens_with_mfe():
    assert ss.profit_floor_net_pct(0.5) is None
    assert ss.profit_floor_net_pct(4.0) == 2.0
    assert ss.profit_floor_net_pct(10.0) == 6.5
    assert ss.profit_floor_net_pct(20.0) == 15.0
    assert ss.profit_floor_net_pct(40.0) == 32.0

def test_closed_trade_records_net_return_and_costs():
    import inspect
    main_src=inspect.getsource(ss.main)
    monitor_src=inspect.getsource(_load_position_monitor().main)
    assert '"realized_net_return_pct"' in main_src
    assert '"estimated_total_fees_usdt"' in main_src
    assert '"gross_price_return_pct"' in main_src
    assert 'NET_PROFIT_GIVEBACK_V3' in monitor_src

def test_fresh_buy_cannot_add_in_same_run():
    import inspect
    src=inspect.getsource(ss.main)
    assert "newly_opened=set()" in src
    assert "s not in newly_opened" in src


def test_hourly_daily_bar_engine_does_not_execute_intraday_profit_sell():
    import inspect
    src=inspect.getsource(ss.main)
    assert 'if p["profit_protection_signal"]' not in src
    assert 'exit_reason="STRUCTURE_BROKEN_PROFIT_EXIT_V3"' in src


def test_trade_actions_are_gated_to_actual_exchange_session():
    from datetime import datetime, timezone, time
    regular={"date":"2026-10-05","open":time(9,30),"close":time(16,0)}
    assert ss.trade_action_window(datetime(2026,10,5,14,0,tzinfo=timezone.utc),regular) is True
    early={"date":"2026-11-27","open":time(9,30),"close":time(13,0)}
    assert ss.trade_action_window(datetime(2026,11,27,18,30,tzinfo=timezone.utc),early) is False
    assert ss.trade_action_window(datetime(2026,10,4,14,0,tzinfo=timezone.utc),None) is False

def test_main_benchmarks_no_longer_use_yahoo():
    import inspect
    src=inspect.getsource(ss.main)
    assert 'bench=discovery.get("benchmarks") or {}' in src
    assert '_alpaca_batch_bars(["SPY","QQQ"])' not in src
    assert "_stock_snapshot(idx)" not in src


def test_exchange_calendar_is_dynamic_and_fail_closed():
    import inspect
    assert "paper-api.alpaca.markets/v2/calendar" in inspect.getsource(ss._alpaca_exchange_session)
    assert "weekday()<5" not in inspect.getsource(ss.trade_action_window)
    m=_load_position_monitor()
    assert "paper-api.alpaca.markets/v2/calendar" in inspect.getsource(m._alpaca_exchange_session)


def _load_fundamentals_observer():
    p=Path("research/stock_shadow/fundamentals_observer.py")
    spec=importlib.util.spec_from_file_location("fo",p); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

def test_fundamentals_observer_remains_observation_only():
    import inspect
    m=_load_fundamentals_observer()
    src=inspect.getsource(m)
    assert '"strategy_effect":False' in src
    assert "BUY" not in inspect.getsource(m.classify_evidence)
    assert "SELL" not in inspect.getsource(m.classify_evidence)

def test_financial_evidence_extracts_trends_and_dilution():
    m=_load_fundamentals_observer()
    def rows(a,b,unit="USD"):
        return {unit:[{"end":"2025-12-31","val":a,"form":"10-K","filed":"2026-02-01"},
                      {"end":"2026-06-30","val":b,"form":"10-Q","filed":"2026-08-01"}]}
    facts={"facts":{"us-gaap":{
      "RevenueFromContractWithCustomerExcludingAssessedTax":{"units":rows(100,120)},
      "NetIncomeLoss":{"units":rows(10,12)},
      "NetCashProvidedByUsedInOperatingActivities":{"units":rows(20,25)},
      "CashAndCashEquivalentsAtCarryingValue":{"units":rows(30,35)},
      "LongTermDebt":{"units":rows(50,45)},
      "CommonStockSharesOutstanding":{"units":rows(100,106,"shares")},
      "PaymentsToAcquirePropertyPlantAndEquipment":{"units":rows(5,6)}
    }}}
    ev=m.financial_evidence(facts)
    assert ev["revenue"]["trend"]=="IMPROVING"
    assert ev["operating_cash_flow"]["trend"]=="IMPROVING"
    assert ev["free_cash_flow"]["values"][-1]["val"]==19.0
    assert ev["share_dilution_pct_latest"]==6.0
    assert m.classify_evidence(ev,{"material_8k_present":"NOT_VERIFIED"})=="WATCH"

def test_severe_risk_evidence_can_flag_critical_without_strategy_effect():
    m=_load_fundamentals_observer()
    assert m.classify_evidence({},{"bankruptcy_restructuring":"VERIFIED_PRESENT"})=="CRITICAL"


def test_fundamentals_hourly_uses_frames_then_bounded_sec_json_not_multigb_bulk():
    import inspect
    m=_load_fundamentals_observer()
    src=inspect.getsource(m.main)
    assert "download_bulk_zip(BULK_COMPANYFACTS)" not in src
    assert "download_bulk_zip(BULK_SUBMISSIONS)" not in src
    assert "frame_evidence_by_cik(previous_batch_index+1)" in src
    assert "sec_companyfacts" in src
    assert "refresh_budget=4" in src
    assert "DISABLED_IN_HOURLY_CI_MULTI_GB_ARCHIVE" in src
    assert "SEC_FRAMES_PLUS_RESIDUAL_GAP_BACKFILL" in src
    assert "FMP_FREE_BULK_UNAVAILABLE" in src
    assert '"evidence_complete"' in src and '"evidence_pending"' in src

def test_bulk_zip_lookup_accepts_sec_cik_filename_forms():
    import io,zipfile,json
    m=_load_fundamentals_observer()
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,"w") as z:
        z.writestr("CIK0000000123.json",json.dumps({"cik":123}))
    with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as z:
        assert m.bulk_json_by_cik(z,{123})[123]["cik"]==123


def test_ledger_invariants_reject_losing_sell_and_too_many_tranches():
    import pytest
    assert ss.validate_ledger({"positions":{"A":{"tranches":[{"price":1,"notional":1}]*5}},"closed":[]},[]) is True
    with pytest.raises(RuntimeError):
        ss.validate_ledger({"positions":{"A":{"tranches":[{"price":1,"notional":1}]*6}},"closed":[]},[])
    with pytest.raises(RuntimeError):
        ss.validate_ledger({"positions":{},"closed":[{"realized_net_pnl_usdt":-1}]},[])

def test_fundamental_trend_handles_negative_values_directionally():
    m=_load_fundamentals_observer()
    assert m._trend([{"val":-43},{"val":-55}])=="DETERIORATING"
    assert m._trend([{"val":-55},{"val":-43}])=="IMPROVING"

def test_result_writers_are_atomic_and_versioned():
    import inspect
    assert ".tmp" in inspect.getsource(ss.save)
    assert "SOURCE_COMMIT" in inspect.getsource(ss.main)
    pm=_load_position_monitor()
    assert ".tmp" in inspect.getsource(pm.save)
    assert "SOURCE_COMMIT" in inspect.getsource(pm.main)

# State continuity regression coverage: manual reset markers are forbidden.

# SERIALIZED_STATE_ACCEPTANCE_20261004


def test_fundamental_frame_budget_and_merge():
    m=_load_fundamentals_observer()
    assert m.FRAME_REQUEST_BUDGET == 12
    prior={"revenue":{"values":[{"val":100}],"trend":"STABLE"},"cash":{"values":[{"val":20}],"trend":"STABLE"}}
    cur={"net_income":{"values":[{"val":5}],"trend":"IMPROVING"},"share_dilution_pct_latest":None}
    merged=m.merge_financial_evidence(prior,cur)
    assert merged["revenue"]["values"][-1]["val"]==100
    assert merged["net_income"]["values"][-1]["val"]==5
    assert m.evidence_sufficient({"revenue":{"values":[1]},"net_income":{"values":[1]},"cash":{"values":[1]}}) is True


def test_manual_reset_markers_are_rejected_by_ledger_invariant():
    import pytest
    with pytest.raises(RuntimeError, match="manual_reset_marker_present"):
        ss.validate_ledger({"positions":{},"closed":[],"reset_reason":"ANY_MANUAL_RESET"},[])

def test_off_session_main_has_state_continuity_guard():
    import inspect
    src=inspect.getsource(ss.main)
    assert "starting_positions" in src
    assert "off_session_position_count_changed" in src
    assert "off_session_event_count_changed" in src
    assert "off_session_closed_count_changed" in src


def test_monitor_rejects_manual_reset_and_guards_off_session_continuity():
    import inspect
    m=_load_position_monitor()
    with __import__("pytest").raises(RuntimeError, match="manual_reset_marker_present"):
        m.validate_ledger({"positions":{},"closed":[],"reset_reason":"BAD_RESET"},[])
    src=inspect.getsource(m.main)
    assert "starting_positions" in src
    assert "off_session_position_count_changed" in src
    assert "off_session_event_count_changed" in src


def test_forward_cohort_fingerprint_detects_same_count_symbol_replacement():
    state={"positions":{
      "AAA":{"opened_at":"x","tranches":[{"at":"x","price":10,"notional":1000,"reason":"SELECTIVE_ENTRY_V1"}]},
      "BBB":{"opened_at":"y","tranches":[{"at":"y","price":20,"notional":1000,"reason":"SELECTIVE_ENTRY_V1"}]}
    },"closed":[]}
    events=[
      {"type":"BUY","symbol":"AAA","at":"x","price":10,"notional":1000,"reason":"SELECTIVE_ENTRY_V1"},
      {"type":"BUY","symbol":"BBB","at":"y","price":20,"notional":1000,"reason":"SELECTIVE_ENTRY_V1"}]
    before=ss.continuity_fingerprint(state,events)
    mutated={"positions":dict(state["positions"]),"closed":[]}
    mutated["positions"].pop("BBB")
    mutated["positions"]["CCC"]={"opened_at":"y","tranches":[{"at":"y","price":20,"notional":1000,"reason":"SELECTIVE_ENTRY_V1"}]}
    assert len(mutated["positions"])==len(state["positions"])
    assert ss.continuity_fingerprint(mutated,events)!=before

def test_monitor_forward_cohort_fingerprint_allows_quote_updates_but_detects_identity_change():
    m=_load_position_monitor()
    state={"positions":{"AAA":{"opened_at":"x","last_price":10,"tranches":[{"at":"x","price":10,"notional":1000,"reason":"SELECTIVE_ENTRY_V1"}]}},"closed":[]}
    events=[{"type":"BUY","symbol":"AAA","at":"x","price":10,"notional":1000,"reason":"SELECTIVE_ENTRY_V1"}]
    before=m.continuity_fingerprint(state,events)
    state["positions"]["AAA"]["last_price"]=11
    state["positions"]["AAA"]["net_pnl_usdt"]=96
    assert m.continuity_fingerprint(state,events)==before
    state["positions"]["AAA"]["tranches"][0]["price"]=9
    assert m.continuity_fingerprint(state,events)!=before


def test_unverified_semantic_risks_are_unknown_not_false():
    import importlib.util
    path=Path("research/stock_shadow/fundamentals_observer.py")
    spec=importlib.util.spec_from_file_location("fundamentals_observer_test",path)
    mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    r=mod.filing_risk_evidence([])
    assert r["going_concern"] is None
    assert r["bankruptcy_restructuring"] is None
    assert r["delisting_risk"] is None
    assert r["semantic_risk_state"]=="UNKNOWN_PENDING_TEXT_REVIEW"
    assert r["semantic_review_status"]=="NOT_YET_TEXT_VERIFIED"

def test_market_health_separates_transport_from_insufficient_history():
    src=Path("research/stock_shadow/stock_shadow_v1.py").read_text()
    assert 'history_coverage_status=("COMPLETE" if insufficient_history_count==0 else "PARTIAL_HISTORY")' in src
    assert '"market_data_status":data_status,"history_coverage_status":history_coverage_status' in src


def test_unverified_filing_risks_are_unknown_not_false():
    import importlib.util
    path=Path("research/stock_shadow/fundamentals_observer.py")
    spec=importlib.util.spec_from_file_location("fund_obs_test",path)
    m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    r=m.filing_risk_evidence([{"form":"8-K"}])
    assert r["going_concern"] is None
    assert r["bankruptcy_restructuring"] is None
    assert r["delisting_risk"] is None
    assert r["material_8k_risk"] is None
    assert r["semantic_risk_state"]=="UNKNOWN_PENDING_TEXT_REVIEW"
    assert m.classify_evidence({},r)=="WATCH"


def test_fundamentals_main_migrates_legacy_unverified_false_flags():
    import inspect
    m=_load_fundamentals_observer()
    src=inspect.getsource(m.main)
    assert 'semantic_review_status")=="NOT_YET_TEXT_VERIFIED"' in src
    assert '("going_concern","bankruptcy_restructuring","delisting_risk","material_8k_risk")' in src
    assert '_risk[_key]=None' in src or 'NOT_VERIFIED' in inspect.getsource(m.filing_risk_evidence)
    assert 'UNKNOWN_PENDING_TEXT_REVIEW' in src




def test_fmp_bulk_fundamentals_provider_is_observation_only(monkeypatch):
    import inspect
    m=_load_fundamentals_observer()
    def fake(endpoint,year,period,key):
        if endpoint=="income-statement-bulk":
            return [{"symbol":"AAA","date":f"{year}-01-01","filingDate":f"{year}-02-01","period":period,"revenue":"100","netIncome":"10","weightedAverageShsOut":"50"}]
        if endpoint=="balance-sheet-statement-bulk":
            return [{"symbol":"AAA","date":f"{year}-01-01","filingDate":f"{year}-02-01","period":period,"cashAndCashEquivalents":"20","totalDebt":"5"}]
        return [{"symbol":"AAA","date":f"{year}-01-01","filingDate":f"{year}-02-01","period":period,"operatingCashFlow":"15","freeCashFlow":"12"}]
    monkeypatch.setattr(m,"fmp_bulk_csv",fake)
    out,status=m.fmp_bulk_evidence(["AAA"],"test-key")
    assert status["provider"]=="FMP_BULK"
    assert status["matched_symbols"]==1
    assert "max_workers=3" in inspect.getsource(m.fmp_bulk_evidence)
    assert m.evidence_sufficient(out["AAA"])
    assert out["AAA"]["revenue"]["concept"]=="FMP_NORMALIZED"
    assert "strategy" not in status


def test_fundamentals_frames_full_batch_not_per_company_financial_loop():
    src=(Path(__file__).parents[1]/"research"/"stock_shadow"/"fundamentals_observer.py").read_text()
    assert "tasks=all_tasks[start:start+FRAME_REQUEST_BUDGET]" in src
    assert "refresh_budget=4" in src
    assert 'provider":"SEC_FRAMES_PLUS_RESIDUAL_GAP_BACKFILL"' in src
    assert "FMP_FREE_BULK_UNAVAILABLE" in src
    # Per-company reads are restricted to the tiny true-gap queue, never the full 308 cohort.
    worker=src[src.index("def fetch_sec_pair"):src.index("with ThreadPoolExecutor",src.index("def fetch_sec_pair"))]
    assert "sec_companyfacts(" in worker
    assert "sec_submission(" in worker
    assert "if sym in semantic_refresh_set" in worker
    assert "refresh_budget=4" in src


def _load_replay():
    p=Path("research/stock_shadow/stock_replay.py")
    spec=importlib.util.spec_from_file_location("stock_replay",p); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

def test_replay_is_chronological_and_observation_only():
    import inspect
    m=_load_replay(); src=inspect.getsource(m)
    assert '"mode":"REPLAY_OBSERVATION_ONLY"' in src
    assert '"strategy_effect":False' in src
    assert '"future_data_prohibited":True' in src
    fn=inspect.getsource(m.chronological_trace)
    assert "for clock in decision_clocks(session)" in fn
    assert "if cutoff_at and clock>_dt(cutoff_at): break" in fn
    decision_src=inspect.getsource(m._decision)
    assert "bars_available_at" in decision_src
    assert "entry_decision" in decision_src
    assert '"EARLY_ACCUMULATION"' in fn

def test_replay_full_universe_screen_then_intraday_only_movers():
    import inspect
    m=_load_replay(); src=inspect.getsource(m.main)
    assert "discover_us_common_stocks()" in src
    assert "ss.MARKET_CACHE" in src
    assert "gain>=SURGE_PCT" in src
    assert 'wanted=list(dict.fromkeys([x[0] for x in movers]+["SPY","QQQ"]))' in src
    assert '"5Min"' in src


def test_replay_reports_early_actual_buy_and_missed_gates():
    import inspect
    m=_load_replay(); src=inspect.getsource(m.main)
    assert '"first_early_signal"' in src
    assert '"actual_buy_before_high"' in src
    assert '"remaining_upside_after_early_pct"' in src
    assert '"missed_gate_reasons"' in src
    assert 'e.get("type")=="BUY"' in src


def test_fmp_unavailable_bulk_is_not_retried_in_production():
    import inspect
    m=_load_fundamentals_observer()
    src=inspect.getsource(m.main)
    assert "FMP_FREE_BULK_UNAVAILABLE" in src
    assert "fmp_bulk_evidence(symbols,api_key)" not in src


def test_replay_reuses_cached_benchmarks_and_reports_early_lateness():
    import inspect
    m=_load_replay(); src=inspect.getsource(m.main)
    assert "ss.MARKET_CACHE" in src
    assert 'alpaca(["SPY","QQQ"],"1Day"' not in src
    assert '"benchmark_daily_explicit":True' in src
    assert '"gain_before_early_pct"' in src
    assert '"first_buy_signal"' in src
    assert '"gate_snapshot_at_last_pre_high"' in src
    assert '"future_leakage_detected":bool(lookahead_violations)' in src

def test_replay_cutoff_excludes_post_high_bars_from_decisions():
    import inspect
    m=_load_replay(); fn=inspect.getsource(m.chronological_trace)
    assert "for clock in decision_clocks(session)" in fn
    assert "if cutoff_at and clock>_dt(cutoff_at): break" in fn
    assert "_decision(" in fn


def test_replay_excludes_pre_and_post_market_using_exchange_calendar():
    from datetime import time
    m=_load_replay()
    session={"date":"2026-10-02","open":time(9,30),"close":time(16,0),"source":"TEST"}
    bars=[
      {"t":"2026-10-02T12:00:00Z","c":1},  # 08:00 NY, premarket
      {"t":"2026-10-02T13:30:00Z","c":2},  # 09:30 NY
      {"t":"2026-10-02T19:55:00Z","c":3},  # 15:55 NY
      {"t":"2026-10-02T20:00:00Z","c":4},  # 16:00 NY, post-session
    ]
    kept=m.regular_session_bars(bars,session)
    assert [x["c"] for x in kept]==[2,3]

def test_replay_uses_dynamic_exchange_calendar_not_hardcoded_weekday_hours():
    import inspect
    m=_load_replay(); src=inspect.getsource(m.main)
    assert "ss._alpaca_exchange_session" in src
    assert '"exchange_session"' in src


def test_replay_output_has_acceptance_metrics():
    src=(Path(__file__).parents[1]/"research"/"stock_shadow"/"stock_replay.py").read_text()
    for key in ('"actual_buy_before_high"','"early_before_high"','"missed_before_high"','"future_data_prohibited"'):
        assert key in src


def test_replay_uses_live_hourly_decision_clock_and_20m_delay():
    from datetime import time
    m=_load_replay()
    session={"date":"2026-10-02","open":time(9,30),"close":time(16,0),"source":"TEST"}
    clocks=m.decision_clocks(session)
    assert clocks
    assert all(x.minute==23 for x in clocks)
    bars=[
      {"t":"2026-10-02T14:00:00Z","o":1,"h":1,"l":1,"c":1,"v":1},
      {"t":"2026-10-02T14:05:00Z","o":2,"h":2,"l":2,"c":2,"v":1},
    ]
    usable,cutoff=m.bars_available_at(bars,__import__("datetime").datetime(2026,10,2,14,23,tzinfo=__import__("datetime").timezone.utc))
    assert cutoff.isoformat().startswith("2026-10-02T14:03")
    assert usable==[]  # 14:00-14:05 bar is not closed by the delayed 14:03 cutoff

def test_replay_future_mutation_invariance_is_real_not_hardcoded():
    import inspect
    m=_load_replay()
    src=inspect.getsource(m)
    assert "class LookaheadViolation" in src
    assert "future_mutation_invariance" in src
    assert "future_mutation_changed_past_decision" in src
    assert '"future_leakage_detected":False' not in src
    assert '"lookahead_violations"' in src

def test_replay_output_contract_matches_acceptance_design():
    import inspect
    m=_load_replay(); src=inspect.getsource(m.main)
    for key in ('"big_movers_total"','"early_detected"','"early_detection_rate"','"buy_detected"',
                '"buy_detection_rate"','"late_early_count"','"missed_by_gate"','"decision_clock"',
                '"strategy_version"','"gain_before_early_pct"','"lookahead_check"','"gate_trace"'):
        assert key in src


def test_semantic_filing_risk_is_tri_state_and_text_verified():
    m=_load_fundamentals_observer()
    r=m.semantic_risk_evidence([{"form":"10-K","filing_date":"2026-02-01","accession":"x","primary_document":"a.htm",
        "transport":"TEST","text":"Management concluded there is substantial doubt about our ability to continue as a going concern."}])
    assert r["going_concern"]=="VERIFIED_PRESENT"
    assert r["bankruptcy_restructuring"]=="VERIFIED_ABSENT"
    assert r["semantic_review_status"]=="TEXT_VERIFIED"
    assert r["semantic_risk_state"]=="VERIFIED_PRESENT"

def test_semantic_review_rotates_independently_from_financial_gap_backfill():
    import inspect
    m=_load_fundamentals_observer(); src=inspect.getsource(m.main)
    assert "semantic_refresh_set" in src
    assert "semantic_refresh_budget=4" in src
    assert "refresh_set|semantic_refresh_set" in src
    assert "sec_submission(cik)" in src
    assert "sec_filing_text" in src
    assert '"semantic_verified"' in src and '"semantic_pending"' in src

def test_foreign_issuer_forms_and_ifrs_are_supported():
    import inspect
    m=_load_fundamentals_observer()
    src=inspect.getsource(m._fact_series)
    assert '"20-F"' in src and '"6-K"' in src
    assert '"ifrs-full"' in src
    main_src=inspect.getsource(m.main)
    assert '"20-F"' in main_src and '"6-K"' in main_src


def test_frames_pacing_and_ifrs_market_batch_coverage():
    import inspect
    m=_load_fundamentals_observer()
    src=inspect.getsource(m.frame_evidence_by_cik)
    assert m.PROXY_START_INTERVAL_SECONDS >= 2.0
    assert m.FRAME_REQUEST_BUDGET == 12
    assert '"CashFlowsFromUsedInOperatingActivities"' in src
    assert '"CashAndCashEquivalents"' in src
    assert '"Borrowings"' in src
    main_src=inspect.getsource(m.main)
    assert "MARKET_BATCH_SCHEMA_VERSION" in main_src


def test_monitor_batches_by_request_target_and_reuses_one_calendar_session():
    import inspect
    m=_load_position_monitor()
    src=inspect.getsource(m)
    assert "MAX_REQUEST_TARGET_CHARS=7000" in src
    assert "BATCH_SIZE=40" not in src
    main_src=inspect.getsource(m.main)
    assert "session=_alpaca_exchange_session()" in main_src
    assert "is_open=market_open(session=session)" in main_src
    assert "trade_actions_enabled=is_open" in main_src

def test_daily_market_cache_is_incremental_and_benchmarks_are_shared():
    import inspect
    src=inspect.getsource(ss)
    assert "MARKET_CACHE" in src
    assert "DAILY_CACHE_KEEP_BARS = 24" in src
    assert "timedelta(days=45 if bootstrap else 7)" in src
    main_src=inspect.getsource(ss.main)
    assert 'bench=discovery.get("benchmarks") or {}' in main_src
    assert '_alpaca_batch_bars(["SPY","QQQ"])' not in main_src

def test_stockfit_is_field_precise_residual_fallback():
    import inspect
    m=_load_fundamentals_observer()
    src=inspect.getsource(m.stockfit_evidence)
    assert "missing_fields" in src
    assert "if not (need & fields): continue" in src
    main_src=inspect.getsource(m.main)
    assert "systemic_fields" in main_src
    assert "stockfit_gap_evidence" in main_src
    assert "stockfit_batch_evidence" not in main_src

def test_semantic_refresh_failure_preserves_prior_verified_evidence():
    import inspect
    m=_load_fundamentals_observer()
    src=inspect.getsource(m.main)
    assert "if reviewed:" in src
    assert "risk_flags=prior_risk" in src


def test_monitor_batches_by_request_size_and_reuses_one_calendar_session():
    import inspect
    m=_load_position_monitor()
    src=inspect.getsource(m)
    main_src=inspect.getsource(m.main)
    assert "MAX_REQUEST_TARGET_CHARS=7000" in src
    assert "BATCH_SIZE=40" not in src
    assert "timedelta(days=3)" in inspect.getsource(m.alpaca_snapshot_quotes)
    assert "session=_alpaca_exchange_session()" in main_src
    assert "trade_actions_enabled=is_open" in main_src


def test_production_fundamentals_disables_known_fmp_402_and_stockfit_is_residual_only():
    import inspect
    m=_load_fundamentals_observer()
    main_src=inspect.getsource(m.main)
    assert "FMP_FREE_BULK_UNAVAILABLE" in main_src
    assert "fmp_bulk_evidence(symbols,api_key)" not in main_src
    assert "systemic_fields" in main_src
    assert "residual_gaps" in main_src
    assert "stockfit_gap_evidence(residual_gaps,stockfit_key)" in main_src
    assert "missing_fields" in inspect.signature(m.stockfit_evidence).parameters
    assert "need=set(missing_fields or ())" in inspect.getsource(m.stockfit_evidence)


def test_stock_shadow_daily_market_cache_is_incremental_and_benchmarks_are_shared():
    import inspect
    src=inspect.getsource(ss.stock_universe)
    assert "MARKET_CACHE" in src
    assert "INCREMENTAL_7D" in src
    assert 'symbols+["SPY","QQQ"]' in src
    assert 'cached.get(idx,[])' in src
    assert "DAILY_CACHE_KEEP_BARS = 24" in inspect.getsource(ss)


def test_ifrs_companyfacts_functional_currency_mapping_is_supported():
    import inspect
    m=_load_fundamentals_observer()
    fact_src=inspect.getsource(m._fact_series)
    evidence_src=inspect.getsource(m.financial_evidence)
    assert 'unit not in {"shares","pure"}' in fact_src
    assert '"Revenue"' in evidence_src
    assert '"PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"' in evidence_src
    assert m.MARKET_BATCH_SCHEMA_VERSION >= 5


def test_history_gap_recovery_is_residual_only_and_classified():
    import inspect
    src=inspect.getsource(ss.stock_universe)
    assert "gap_symbols=[s for s in symbols if len(cached.get(s) or [])<22]" in src
    assert "gap_start=end-timedelta(days=120)" in src
    assert "history_gap_recovery" in src
    assert "SOURCE_HISTORY_1_21_BARS_AFTER_120D_RECOVERY" in src
    assert "NO_RECENT_DAILY_BARS_AFTER_120D_RECOVERY" in src
    assert "SOURCE_NO_BARS_AFTER_120D_RECOVERY" not in src
    assert "SOURCE_HISTORY_1_21_BARS_AFTER_120D_RECOVERY" in src
    # The deep read must target only the residual queue, never the whole discovered universe.
    assert '_pack_alpaca_symbol_batches(gap_symbols,"1Day",gap_start,end)' in src


def test_zero_bar_residuals_are_universe_exclusions_not_indicator_history_gaps():
    import inspect
    universe_src=inspect.getsource(ss.stock_universe)
    main_src=inspect.getsource(ss.main)
    assert 'universe_exclusion="NO_RECENT_DAILY_BARS_AFTER_120D_RECOVERY"' in universe_src
    assert 'classification=None' in universe_src
    assert '"universe_exclusion":universe_exclusion' in universe_src
    assert 'history_gap_classification")=="SOURCE_HISTORY_1_21_BARS_AFTER_120D_RECOVERY"' in main_src
    assert '"universe_exclusion_count":universe_exclusion_count' in main_src
    # Do not hard-code present-day symbol exceptions; replay must use the same PIT rule.
    assert '"SVA"' not in universe_src
    assert '"MMEDV"' not in universe_src


def test_exchange_calendar_uses_same_day_persisted_cache_and_fail_closed():
    import inspect
    src=inspect.getsource(ss._alpaca_exchange_session)
    assert "CALENDAR_CACHE" in src
    assert 'cached.get("date")==day' in src
    assert 'cached.get("closed") is True' in src
    assert "ALPACA_EXCHANGE_CALENDAR_CACHE" in src
    assert 'API_USAGE["alpaca_calendar"]["http_requests"]+=1' in src
    # No weekday/hard-coded session fallback is allowed.
    assert "weekday()" not in src


def test_position_monitor_reuses_same_calendar_cache_and_reports_usage():
    import inspect
    m=_load_position_monitor()
    src=inspect.getsource(m._alpaca_exchange_session)
    main_src=inspect.getsource(m.main)
    assert "CALENDAR_CACHE" in src
    assert 'cached.get("date")==day' in src
    assert "ALPACA_EXCHANGE_CALENDAR_CACHE" in src
    assert "CALENDAR_USAGE" in src
    assert "calendar_api_usage" in main_src


def test_historical_replay_calendar_lookup_does_not_mutate_persisted_today_cache():
    import inspect
    src=inspect.getsource(ss._alpaca_exchange_session)
    assert "current_day=" in src
    assert "cacheable=(day==current_day)" in src
    assert "if cacheable: save(CALENDAR_CACHE" in src


def test_history_gap_classifier_never_infers_listing_age_from_cached_first_bar():
    import inspect
    src=inspect.getsource(ss.stock_universe)
    assert "SOURCE_HISTORY_1_21_BARS_AFTER_120D_RECOVERY" in src
    assert "POSSIBLE_NEW_LISTING" not in src
    assert "(end-first_dt).days" not in src
    assert 'deep_window_days"]=120' in src
    assert '"bars_available":len(rows)' in src
    assert '"first_bar_at":first_bar_at' in src
    assert '"last_bar_at":last_bar_at' in src
    assert '"recovery_window_days":120 if (classification or universe_exclusion) else None' in src
    assert "sorted({" in inspect.getsource(ss.main)


def test_ledger_rejects_weekend_trade_events():
    import pytest
    state={"version":2,"simulation_only":True,"positions":{},"closed":[]}
    events=[{"type":"BUY","symbol":"TEST","at":"2026-10-03T19:37:39+00:00","price":10.0,"notional":1000.0}]
    with pytest.raises(RuntimeError, match="ledger_invariant:weekend_trade_event"):
        ss.validate_ledger(state,events)


def test_ledger_accepts_weekday_trade_event_shape():
    state={"version":2,"simulation_only":True,"positions":{},"closed":[]}
    events=[{"type":"BUY","symbol":"TEST","at":"2026-10-02T19:37:39+00:00","price":10.0,"notional":1000.0}]
    assert ss.validate_ledger(state,events) is True


def test_final_trade_mutations_recheck_exchange_session():
    import inspect
    src=inspect.getsource(ss.main)
    assert 'if actions_enabled and trade_action_window(datetime.fromisoformat(event_at),session=session) and s not in state["positions"]:' in src
    assert 'if actions_enabled and trade_action_window(datetime.fromisoformat(event_at),session=session) and s not in newly_opened' in src
    assert 'if exit_reason and actions_enabled and trade_action_window(datetime.fromisoformat(event_at),session=session):' in src

def test_weekend_and_off_session_trade_gate_is_fail_closed():
    from datetime import datetime, timezone, time
    regular={"date":"2026-10-05","open":time(9,30),"close":time(16,0)}
    assert ss.trade_action_window(datetime(2026,10,3,19,37,tzinfo=timezone.utc), None) is False
    assert ss.trade_action_window(datetime(2026,10,4,19,37,tzinfo=timezone.utc), None) is False
    assert ss.trade_action_window(datetime(2026,10,5,13,29,tzinfo=timezone.utc), regular) is False
    assert ss.trade_action_window(datetime(2026,10,5,13,30,tzinfo=timezone.utc), regular) is True
    assert ss.trade_action_window(datetime(2026,10,5,20,0,tzinfo=timezone.utc), regular) is False


# P0 execution acceptance: run the actual entry points with clocks and providers
# controlled, preserving the input portfolio/ledger files throughout the tests.
import pytest
from datetime import datetime as real_datetime, timezone as utc_timezone, time as clock_time

@pytest.mark.parametrize('stamp,day,opened,closed,allowed', [
    ('2026-10-03T19:37:00+00:00','2026-10-03','09:30','16:00',False),
    ('2026-10-04T09:26:25.677007+00:00','2026-10-04','09:30','16:00',False),
    ('2026-07-03T15:00:00+00:00',None,None,None,False),
    ('2026-10-05T13:29:59+00:00','2026-10-05','09:30','16:00',False),
    ('2026-10-05T13:30:00+00:00','2026-10-05','09:30','16:00',True),
    ('2026-10-05T17:00:00+00:00','2026-10-05','09:30','16:00',True),
    ('2026-10-05T19:59:59+00:00','2026-10-05','09:30','16:00',True),
    ('2026-10-05T20:00:00+00:00','2026-10-05','09:30','16:00',False),
    ('2026-10-05T22:00:00+00:00','2026-10-05','09:30','16:00',False),
    ('2026-11-27T17:59:59+00:00','2026-11-27','09:30','13:00',True),
    ('2026-11-27T18:00:00+00:00','2026-11-27','09:30','13:00',False),
    ('2026-12-07T14:30:00+00:00','2026-12-07','09:30','16:00',True),
])
def test_p0_both_engines_share_calendar_boundary_gate(monkeypatch,stamp,day,opened,closed,allowed):
    session={'date':day,'open':clock_time.fromisoformat(opened),'close':clock_time.fromisoformat(closed)} if day else None
    monitor=_load_position_monitor()
    for engine,gate in [(ss,ss.trade_action_window),(monitor,monitor.market_open)]:
        monkeypatch.setattr(engine,'_alpaca_exchange_session',lambda ts=None: session)
        assert gate(real_datetime.fromisoformat(stamp),session=session) is allowed

@pytest.mark.parametrize('session', [None,{}, {'date':'2026-10-05','open':'09:30','close':'16:00'},
    {'date':'2026-10-04','open':clock_time(9,30),'close':clock_time(16)},
    {'date':'2026-10-05','open':clock_time(16),'close':clock_time(9,30)}])
def test_p0_missing_or_malformed_session_never_allows_trade(session):
    from research.stock_shadow.market_session import session_allows_trade
    assert not session_allows_trade(real_datetime.fromisoformat('2026-10-05T17:00:00+00:00'),session)


def _p0_book(engine,monkeypatch):
    import copy
    state={'positions':{symbol:{'symbol':symbol,'opened_at':'2026-10-02T15:00:00+00:00',
        'tranches':[{'at':'2026-10-02T15:00:00+00:00','price':90.0,'notional':1000.0,'reason':'SELECTIVE_ENTRY_V1'}],
        'mfe_net_pct':10.0} for symbol in ['ADD','EXIT']},'closed':[]}
    store={engine.STATE:state,engine.EVENTS:[]}
    monkeypatch.setattr(engine,'load',lambda path,default: copy.deepcopy(store.get(path,default)))
    monkeypatch.setattr(engine,'save',lambda path,data: store.__setitem__(path,copy.deepcopy(data)))
    return store


def _p0_clock(engine,monkeypatch,initial,event_stamp):
    class Clock(real_datetime):
        @classmethod
        def now(cls,tz=None):
            return real_datetime.fromisoformat(initial).astimezone(tz or utc_timezone.utc)
    monkeypatch.setattr(engine,'datetime',Clock)
    monkeypatch.setattr(engine,'now',lambda:event_stamp)

def _fresh_benchmarks():
    return {symbol: {**_m(r20=0), 'base': symbol} for symbol in ('SPY', 'QQQ')}

@pytest.mark.parametrize('initial,event_stamp,expect_trade', [
    ('2026-10-04T09:00:00+00:00','2026-10-04T09:26:25+00:00',False),
    ('2026-10-05T19:59:59+00:00','2026-10-05T20:00:00+00:00',False),
    ('2026-10-05T17:00:00+00:00','2026-10-05T17:00:01+00:00',True),
])
def test_p0_full_scan_buy_add_structural_sell_and_close_crossing(monkeypatch,initial,event_stamp,expect_trade):
    book=_p0_book(ss,monkeypatch)
    before=ss.continuity_fingerprint(book[ss.STATE],book[ss.EVENTS])
    _p0_clock(ss,monkeypatch,initial,event_stamp)
    day=initial[:10]
    monkeypatch.setattr(ss,'_alpaca_exchange_session',lambda ts=None:{'date':day,'open':clock_time(9,30),'close':clock_time(16)})
    market={symbol:{**_m(price=100),'base':symbol} for symbol in ['NEW','ADD','EXIT']}
    monkeypatch.setattr(ss,'stock_universe',lambda:(market,[],{'discovered':3,'source_errors':[], 'benchmarks':_fresh_benchmarks()}))
    monkeypatch.setattr(ss,'entry_decision',lambda m,*a:{'ready':m['base']!='EXIT','score':80,'entry_structure':'MOMENTUM_TREND','reasons':[],'rejects':[],'metrics':{}})
    monkeypatch.setattr(ss,'position_state_v2',lambda m,*a:{'state':'BROKEN' if m['base']=='EXIT' else 'STRONG','market_relative20':1})
    monkeypatch.setattr(ss,'recovery_add_signal',lambda p,m,ps:{'eligible':m['base']=='ADD'})
    ss.main()
    if expect_trade:
        assert {e['type'] for e in book[ss.EVENTS]}=={'BUY','ADD','SELL'}
        assert all(e['at']==event_stamp for e in book[ss.EVENTS])
    else:
        assert ss.continuity_fingerprint(book[ss.STATE],book[ss.EVENTS])==before
    assert book[ss.SUMMARY]['candidates_ready']==(0 if initial.startswith('2026-10-04') else 2)

@pytest.mark.parametrize('price,tranche_prices,pending,actions_enabled,quoted,expected', [
    (90.0, [100.0], False, True, True, ['ADD']),
    (100.5, [100.0], False, True, True, ['ADD']),  # ADD fees cross zero.
    (110.0, [90.0, 100.0], False, True, True, ['ADD']),
    (110.0, [100.0], True, True, True, ['ADD', 'SELL']),
    (110.0, [100.0] * 5, False, True, True, []),
    (110.0, [100.0], False, False, True, []),
    (110.0, [100.0], False, True, False, []),
])
def test_post_add_reporting_preserves_decisions_and_history(
        monkeypatch, price, tranche_prices, pending, actions_enabled, quoted, expected):
    from research.stock_shadow.server.persist import reporting_audit

    book = _p0_book(ss, monkeypatch)
    old_closed = book[ss.STATE]['positions'].pop('EXIT')
    old_closed.update(realized_net_pnl_usdt=12.0, closed_at='2026-10-02T16:00:00+00:00')
    book[ss.STATE]['closed'] = [old_closed]
    p = book[ss.STATE]['positions']['ADD']
    p['tranches'] = [{**copy.deepcopy(p['tranches'][0]), 'price': value}
                     for value in tranche_prices]
    p.update(mae_net_pct=-1.0, last_price=100.0, net_pnl_usdt=-4.0, net_return_pct=-0.4)
    if pending:
        p['rebound_exit_pending_v3'] = {'armed_at': '2026-10-02T15:00:00+00:00',
                                      'lowest_net_return_pct': -2.0}
    book[ss.EVENTS] = [{'type': 'BUY', 'symbol': 'ADD', **copy.deepcopy(p['tranches'][0])}]
    before = copy.deepcopy(book)
    before_pnl, before_return = ss.net_pnl(p, price), ss.net_pct(p, price)
    stamp = '2026-10-05T17:00:01+00:00' if actions_enabled else '2026-10-05T20:00:01+00:00'
    _p0_clock(ss, monkeypatch, stamp, stamp)
    monkeypatch.setattr(ss, '_alpaca_exchange_session', lambda ts=None:
                        {'date': '2026-10-05', 'open': clock_time(9, 30), 'close': clock_time(16)})
    market = {'ADD': {**_m(price=price), 'base': 'ADD'}} if quoted else {}
    monkeypatch.setattr(ss, 'stock_universe', lambda: (market, [], {'discovered': 1, 'source_errors': [], 'benchmarks': _fresh_benchmarks()}))
    monkeypatch.setattr(ss, 'entry_decision', lambda *args:
                        {'ready': True, 'score': 80, 'entry_structure': 'MOMENTUM_TREND',
                         'reasons': [], 'rejects': [], 'metrics': {}})
    monkeypatch.setattr(ss, 'position_state_v2', lambda *args:
                        {'state': 'STRONG', 'market_relative20': 1})
    monkeypatch.setattr(ss, 'recovery_add_signal', lambda *args: {'eligible': True})

    ss.main()

    state, events = book[ss.STATE], book[ss.EVENTS]
    assert events[:1] == before[ss.EVENTS]
    assert state['closed'][:1] == before[ss.STATE]['closed']
    assert [event['type'] for event in events[1:]] == expected
    saved = state['closed'][-1] if 'SELL' in expected else state['positions']['ADD']
    assert saved['tranches'][:len(tranche_prices)] == before[ss.STATE]['positions']['ADD']['tranches']
    assert len(saved['tranches']) == len(tranche_prices) + ('ADD' in expected)
    if not quoted:
        assert {k:v for k,v in saved.items() if k not in {'quote_status','quote_checked_at','valuation_status','quote_actions_allowed'}} == before[ss.STATE]['positions']['ADD']
        assert saved['quote_status']=='MISSING_QUOTE'
        return
    # Extrema and all V3 lifecycle/exit inputs retain the pre-ADD observation.
    assert saved['mfe_net_pct'] == max(10.0, before_return)
    assert saved['mae_net_pct'] == min(-1.0, before_return)
    assert saved['position_state_v3']['net_return_pct'] == round(before_return, 6)
    if 'SELL' in expected:
        assert events[-1]['reason'] == 'REBOUND_PROFIT_EXIT_AFTER_DETERIORATION_V3'
        assert events[-1]['net_pnl_usdt'] == round(before_pnl - 4.0, 6)
        assert saved['net_pnl_usdt'] == round(before_pnl, 6)  # Closed history is not rewritten.
    else:
        assert saved['net_pnl_usdt'] == round(ss.net_pnl(saved, price), 6)
        assert saved['net_return_pct'] == round(ss.net_pct(saved, price), 6)
        assert reporting_audit(state)['status'] == 'MATCH'
        if 'ADD' in expected:
            assert saved['net_pnl_usdt'] == round(before_pnl - 4.0, 6)
            assert saved['avg_price'] == ss.avg(saved)
    assert book[ss.SUMMARY]['realized_net_pnl_usdt'] == round(
        sum(item['realized_net_pnl_usdt'] for item in state['closed']), 6)


@pytest.mark.parametrize('initial,event_stamp,expect_sell', [
    ('2026-10-04T09:00:00+00:00','2026-10-04T09:26:25+00:00',False),
    ('2026-10-05T19:59:59+00:00','2026-10-05T20:00:00+00:00',False),
    ('2026-10-05T17:00:00+00:00','2026-10-05T17:00:01+00:00',True),
])
def test_p0_forced_monitor_cannot_bypass_weekend_or_final_close_gate(monkeypatch,initial,event_stamp,expect_sell):
    monitor=_load_position_monitor();book=_p0_book(monitor,monkeypatch)
    before=monitor.continuity_fingerprint(book[monitor.STATE],book[monitor.EVENTS])
    _p0_clock(monitor,monkeypatch,initial,event_stamp)
    monkeypatch.setattr(monitor,'_alpaca_exchange_session',lambda ts=None:{'date':initial[:10],'open':clock_time(9,30),'close':clock_time(16)})
    monkeypatch.setattr(monitor,'alpaca_snapshot_quotes',lambda symbols:({s:{"price":94.0,"price_asof":(real_datetime.fromisoformat(event_stamp)-__import__("datetime").timedelta(minutes=25)).isoformat()} for s in symbols},[],1,1))
    monitor.main(force=True)
    if expect_sell:
        assert len(book[monitor.EVENTS])==2
        assert all(e['type']=='SELL' and e['at']==event_stamp for e in book[monitor.EVENTS])
    else:
        assert monitor.continuity_fingerprint(book[monitor.STATE],book[monitor.EVENTS])==before
    assert book[monitor.HEALTH]['positions_updated']==2


def _p0_git_pair(tmp_path):
    import subprocess
    def git(where,*args):
        return subprocess.run(['git',*args],cwd=where,check=True,capture_output=True,text=True)
    remote=tmp_path/'remote.git';work=tmp_path/'work';writer=tmp_path/'writer'
    git(tmp_path,'init','--bare',str(remote))
    git(tmp_path,'init','-b','main',str(work))
    for key,value in [('user.name','Test'),('user.email','test@example.invalid')]: git(work,'config',key,value)
    result=work/'research/results/stock-shadow';result.mkdir(parents=True)
    (result/'portfolio-v1.json').write_text('{"positions":{},"closed":[],"simulation_only":true}\n')
    (result/'trades-v1.json').write_text('[]\n')
    (work/'.github').mkdir()
    (work/'.github/stock-runtime.json').write_text(
        '{"schema":"stock_shadow_runtime_v1","owner":"github","epoch":1,'
        '"jobs":["main","monitor","replay"],"shadow_only":true,"automatic_failover":false}\n')
    (work/'unrelated.txt').write_text('initial\n')
    git(work,'add','.');git(work,'commit','-m','base');git(work,'remote','add','origin',str(remote));git(work,'push','origin','main')
    git(tmp_path,'clone','-b','main',str(remote),str(writer))
    for key,value in [('user.name','Other'),('user.email','other@example.invalid')]: git(writer,'config',key,value)
    return git,remote,work,writer


def _p0_persist_environment(work, job='main', run_id='fixture-test.1'):
    import hashlib, json, os, subprocess
    source=subprocess.run(['git','rev-parse','HEAD'],cwd=work,check=True,capture_output=True,text=True).stdout.strip()
    config=json.loads(subprocess.run(['git','show','HEAD:.github/stock-runtime.json'],cwd=work,check=True,capture_output=True,text=True).stdout)
    owner=config['owner'];epoch=config['epoch']
    identity=f'{source}:{owner}:{epoch}:{job}:{run_id}:1'
    env={k:v for k,v in os.environ.items() if not k.startswith('STOCK_SHADOW_')}
    env.update({'STOCK_SHADOW_SOURCE_COMMIT':source,'STOCK_SHADOW_WRITER':owner,
                'STOCK_SHADOW_EPOCH':str(epoch),'STOCK_SHADOW_JOB':job,
                'STOCK_SHADOW_RUN_ID':run_id,'STOCK_SHADOW_ATTEMPT':'1',
                'STOCK_SHADOW_GENERATION':hashlib.sha256(identity.encode()).hexdigest()})
    return env


def _p0_run_persister(work, mode, env=None):
    import json, subprocess
    env=env or _p0_persist_environment(work, 'main' if mode=='health' else mode)
    job=env['STOCK_SHADOW_JOB']
    if job in ('main','monitor'):
        health={'source_commit':env['STOCK_SHADOW_SOURCE_COMMIT'],'run_id':env['STOCK_SHADOW_RUN_ID'],
                'status':'FAILED' if mode=='health' else 'SUCCESS','simulation_only':True,'real_orders':False}
        (work/f'research/results/stock-shadow/{job}-run-health-v1.json').write_text(json.dumps(health)+'\n')
    return subprocess.run(['bash',str(Path(__file__).resolve().parents[1]/'research/stock_shadow/persist_results.sh'),mode],
                          cwd=work,env=env,capture_output=True,text=True)


def test_p0_persistence_preserves_other_system_commits(tmp_path):
    import subprocess
    git,remote,work,writer=_p0_git_pair(tmp_path)
    (work/'research/results/stock-shadow/summary-v1.json').write_text('{"fixture":"NEW"}\n')
    (writer/'unrelated.txt').write_text('other system latest\n')
    git(writer,'add','.');git(writer,'commit','-m','other update');git(writer,'push','origin','main')
    result=_p0_run_persister(work,'main')
    assert result.returncode==0,result.stdout+result.stderr
    assert git(work,'show','origin/main:unrelated.txt').stdout=='other system latest\n'
    assert 'NEW' in git(work,'show','origin/main:research/results/stock-shadow/summary-v1.json').stdout


def test_p0_persistence_refuses_to_overwrite_a_newer_portfolio(tmp_path):
    import subprocess
    git,remote,work,writer=_p0_git_pair(tmp_path)
    path='research/results/stock-shadow/portfolio-v1.json'
    (work/'research/results/stock-shadow/summary-v1.json').write_text('{"fixture":"STALE"}\n')
    (writer/path).write_text('{"positions":{"FRESH":{}}}\n')
    git(writer,'add','.');git(writer,'commit','-m','newer portfolio');git(writer,'push','origin','main')
    result=_p0_run_persister(work,'monitor')
    assert result.returncode==43,result.stdout+result.stderr
    assert 'FRESH' in git(work,'show','origin/main:'+path).stdout
    assert 'STALE' not in git(work,'show','origin/main:'+path).stdout


def test_p0_push_race_retries_only_own_outputs(tmp_path):
    import subprocess,shlex
    git,remote,work,writer=_p0_git_pair(tmp_path)
    (work/'research/results/stock-shadow/summary-v1.json').write_text('{"fixture":"NEW"}\n')
    (writer/'unrelated.txt').write_text('race winner\n')
    git(writer,'add','.');git(writer,'commit','-m','race update')
    hook=work/'.git/hooks/pre-push'
    hook.write_text('#!/bin/bash\nif [ ! -f .git/race-injected ]; then\n touch .git/race-injected\n git -C '+shlex.quote(str(writer))+' push origin main\nfi\n')
    hook.chmod(0o755)
    result=_p0_run_persister(work,'main')
    assert result.returncode==0,result.stdout+result.stderr
    assert 'race winner' in git(work,'show','origin/main:unrelated.txt').stdout
    assert 'NEW' in git(work,'show','origin/main:research/results/stock-shadow/summary-v1.json').stdout


@pytest.mark.parametrize('field', ['swing_high_price','pullback_low_price'])
def test_recovery_optional_prices_accept_null_and_missing(field):
    for value in [None, 'MISSING']:
        p={'swing_high_price':100.0,'pullback_seen':False}
        if value != 'MISSING': p[field]=value
        out=ss.recovery_add_signal(p,{'price':98.0},{'state':'STRONG','market_relative20':1.0})
        assert out['eligible'] is False
        assert p['swing_high_price'] >= 98.0


def test_add_reset_then_new_pullback_requires_new_recovery():
    p={'swing_high_price':100.0,'pullback_low_price':None,'pullback_seen':False,
       'position_state_v2':{'market_relative20':0.0}}
    ps={'state':'STRONG','market_relative20':1.0}
    first=ss.recovery_add_signal(p,{'price':98.0},ps)
    assert p['pullback_low_price']==98.0 and not first['eligible']
    assert not ss.recovery_add_signal(p,{'price':97.0},ps)['eligible']
    assert p['pullback_low_price']==97.0
    assert ss.recovery_add_signal(p,{'price':98.0},ps)['eligible']


@pytest.mark.parametrize('field,value', [('swing_high_price',0),('pullback_low_price',-1),
    ('pullback_low_price','bad'),('pullback_low_price',float('nan')),('swing_high_price',float('inf'))])
def test_recovery_rejects_invalid_numbers(field,value):
    p={'swing_high_price':100.0,field:value}
    with pytest.raises(ValueError,match='stock_state:'):
        ss.recovery_add_signal(p,{'price':98.0},{'state':'STRONG','market_relative20':1.0})


def test_missing_relative_strength_never_adds():
    p={'swing_high_price':100.0,'pullback_low_price':90.0,'pullback_seen':True,
       'position_state_v2':{'market_relative20':0.0}}
    assert not ss.recovery_add_signal(p,{'price':98.0},{'state':'STRONG','market_relative20':None})['eligible']
    assert ss.position_state_v3({'mae_net_pct':None},{'state':'STRONG'},-4)['mae_net_pct']==-4


def test_full_scan_null_derived_state_and_pending_rebound(monkeypatch):
    book=_p0_book(ss,monkeypatch)
    _p0_clock(ss,monkeypatch,'2026-10-05T17:00:00+00:00','2026-10-05T17:00:01+00:00')
    monkeypatch.setattr(ss,'_alpaca_exchange_session',lambda ts=None:None)
    for p in book[ss.STATE]['positions'].values():
        p.update(mfe_net_pct=None,mae_net_pct=None,swing_high_price=100.0,pullback_low_price=None,
                 rebound_exit_pending_v3=None)
    monkeypatch.setattr(ss,'stock_universe',lambda:({s:{**_m(price=80,r5=-10,r20=-20,sma20=100),'base':s} for s in ['ADD','EXIT']},[],{'discovered':2,'source_errors':[], 'benchmarks':_fresh_benchmarks()}))
    ss.main()
    assert book[ss.EVENTS]==[]
    for p in book[ss.STATE]['positions'].values():
        assert p['pullback_low_price']==80.0
        assert p['rebound_exit_pending_v3']['lowest_net_return_pct'] < 0


def test_failure_health_keeps_authoritative_book_untouched(monkeypatch):
    import copy
    book=_p0_book(ss,monkeypatch);book[ss.STATE]['positions']['ADD']['tranches'][0]['price']=None
    before=copy.deepcopy(book)
    health=ss.ROOT/'main-run-health-v1.json'
    with pytest.raises(ValueError,match='ADD.tranches'):
        ss.run_with_health(ss.main,health,'commit','run',ss.save)
    assert book[ss.STATE]==before[ss.STATE] and book[ss.EVENTS]==before[ss.EVENTS]
    assert book[health]['status']=='FAILED'
    assert 'ADD.tranches' in book[health]['error']


def test_monitor_null_extremes_and_summary_binding(monkeypatch):
    monitor=_load_position_monitor();book=_p0_book(monitor,monkeypatch)
    for p in book[monitor.STATE]['positions'].values(): p.update(mfe_net_pct=None,mae_net_pct=None)
    _p0_clock(monitor,monkeypatch,'2026-10-05T17:00:00+00:00','2026-10-05T17:00:01+00:00')
    monkeypatch.setattr(monitor,'_alpaca_exchange_session',lambda ts=None:None)
    monkeypatch.setattr(monitor,'alpaca_snapshot_quotes',lambda symbols:({s:{"price":89.0,"price_asof":"2026-10-05T16:35:00Z"} for s in symbols},[],1,1))
    summary=monitor.ROOT/'summary-v1.json';book[summary]={'run_id':'old','updated_at':'old scan'}
    monitor.main(force=True)
    assert book[summary]['open_positions']==2
    assert book[summary]['portfolio_run_id']==monitor.RUN_ID
    assert book[summary]['updated_at']=='old scan' and book[summary]['scan_state_stale'] is True
    assert book[monitor.EVENTS]==[]


def test_health_only_persistence_rejects_a_newer_stock_snapshot(tmp_path):
    import subprocess
    git,remote,work,writer=_p0_git_pair(tmp_path)
    path='research/results/stock-shadow/portfolio-v1.json'
    (work/path).write_text('{"positions":{"PARTIAL":{}}}\n')
    health='research/results/stock-shadow/main-run-health-v1.json'
    (work/health).write_text('{"status":"FAILED"}\n')
    (writer/path).write_text('{"positions":{"FRESH":{}}}\n')
    git(writer,'add','.');git(writer,'commit','-m','fresh book');git(writer,'push','origin','main')
    result=_p0_run_persister(work,'health')
    assert result.returncode==43,result.stdout+result.stderr
    assert 'FRESH' in git(work,'show','origin/main:'+path).stdout
    assert git(work,'ls-tree','--name-only','origin/main','--',health).stdout==''


def test_isolation_guard_really_fails_for_tracked_and_untracked_files(tmp_path):
    import subprocess
    git,remote,work,writer=_p0_git_pair(tmp_path)
    guard=str(Path('research/stock_shadow/isolation_guard.sh').resolve())
    (work/'research/results/stock-shadow/new-health.json').write_text('{}')
    cache=work/'research/stock_shadow/__pycache__';cache.mkdir(parents=True)
    (cache/'stock_shadow_v1.cpython-312.pyc').write_bytes(b'cache')
    assert subprocess.run(['bash',guard],cwd=work,capture_output=True).returncode==0
    (work/'unrelated.txt').write_text('changed')
    assert subprocess.run(['bash',guard],cwd=work,capture_output=True).returncode==1
    git(work,'checkout','--','unrelated.txt')
    (work/'untracked.py').write_text('unexpected')
    assert subprocess.run(['bash',guard],cwd=work,capture_output=True).returncode==1


# Data retrieval and acceptance are fail-closed; engine/scoring behavior is unchanged.
def _replay_bar(stamp, high=110.0):
    return {'t': stamp, 'o': 100.0, 'h': high, 'l': 99.0, 'c': 105.0, 'v': 1_000_000}


def _replay_data_fixture(monkeypatch, tmp_path, *, as_of='2026-10-06T20:28:00+00:00',
                         target='2026-10-06', close=clock_time(16), mover=True):
    from datetime import timedelta
    m = _load_replay()
    now = real_datetime.fromisoformat(as_of)
    calls = {'now': 0, 'pack': [], 'provider': [], 'calendar': []}

    class FrozenDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            calls['now'] += 1
            return now.astimezone(tz) if tz is not None else now.replace(tzinfo=None)

    monkeypatch.setattr(m, 'datetime', FrozenDateTime)
    midnight = real_datetime.fromisoformat(target + 'T00:00:00+00:00')
    history = [_replay_bar((midnight - timedelta(days=n)).isoformat(), 104.0)
               for n in range(35, 0, -1)]
    daily = {symbol: copy.deepcopy(history) + [_replay_bar(target + 'T04:00:00Z', high)]
             for symbol, high in [('MOVER', 110.0 if mover else 107.0),
                                  ('SPY', 102.0), ('QQQ', 103.0), ('QRVO', 102.0)]}
    # A cached historical surge and a current holding do not join today's mover cohort.
    daily['OLD_SURGE'] = copy.deepcopy(history)
    daily['OLD_SURGE'][-1]['h'] = 150.0
    market_cache = tmp_path / 'market-daily-cache-v1.json'
    market_cache.write_text(json.dumps({'bars': daily}) + '\n')
    trades = tmp_path / 'trades-v1.json'
    trades.write_bytes(b'[  ]\n')
    portfolio = tmp_path / 'portfolio-v1.json'
    portfolio.write_bytes(b'{"positions":{"HOLDING_ONLY":{}},"closed":[],"simulation_only":true}\n')
    calendar_cache = tmp_path / 'calendar-session-cache-v1.json'
    calendar_cache.write_bytes(b'{"fixture":"read-only"}\n')
    monkeypatch.setattr(m, 'OUT', tmp_path / 'replay-v1.json')
    monkeypatch.setattr(m, 'TRADES', trades)
    monkeypatch.setattr(m.ss, 'STATE', portfolio)
    monkeypatch.setattr(m.ss, 'MARKET_CACHE', market_cache)
    monkeypatch.setattr(m.ss, 'CALENDAR_CACHE', calendar_cache)
    monkeypatch.setattr(m.ss, 'discover_us_common_stocks',
                        lambda: (['MOVER', 'QRVO', 'OLD_SURGE', 'HOLDING_ONLY'], []))
    session = {'date': target, 'open': clock_time(9, 30), 'close': close, 'source': 'TEST_CALENDAR'}

    def get_session(at):
        calls['calendar'].append(at)
        return session

    monkeypatch.setattr(m.ss, '_alpaca_exchange_session', get_session)
    real_pack = m.ss._pack_alpaca_symbol_batches

    def pack(symbols, timeframe, start, end):
        calls['pack'].append((list(symbols), timeframe, start, end))
        return real_pack(symbols, timeframe, start, end)

    monkeypatch.setattr(m.ss, '_pack_alpaca_symbol_batches', pack)
    session_open = real_datetime.combine(midnight.date(), session['open'], m.ss.NY).astimezone(utc_timezone.utc)
    bars = [_replay_bar((session_open + timedelta(minutes=5 * n)).isoformat(), 105.0 + n / 100)
            for n in range(int((real_datetime.combine(midnight.date(), close, m.ss.NY).astimezone(utc_timezone.utc)
                                - session_open).total_seconds() // 300))]
    data = {symbol: copy.deepcopy(bars) for symbol in ('MOVER', 'SPY', 'QQQ')}

    def provider(symbols, timeframe, start, end):
        calls['provider'].append((list(symbols), timeframe, start, end))
        return {symbol: copy.deepcopy(data[symbol]) for symbol in symbols if symbol in data}

    monkeypatch.setattr(m, 'alpaca', provider)
    monkeypatch.setattr(m.urllib.request, 'urlopen',
                        lambda *args, **kwargs: pytest.fail('replay test attempted live network access'))
    original_bytes = {path: path.read_bytes() for path in (trades, portfolio, market_cache, calendar_cache)}
    return m, calls, data, original_bytes


def _assert_replay_inputs_unchanged(original_bytes):
    assert {path: path.read_bytes() for path in original_bytes} == original_bytes


@pytest.mark.parametrize('target,as_of,expected_end', [
    ('2026-10-02', '2026-10-06T20:28:00+00:00', '2026-10-03T00:00:00+00:00'),
    ('2026-10-06', '2026-10-06T20:28:00+00:00', '2026-10-06T20:08:00+00:00'),
    ('2026-10-07', '2026-10-06T20:28:00+00:00', '2026-10-06T20:08:00+00:00'),
    ('2026-10-06', '2026-10-06T16:28:00-04:00', '2026-10-06T20:08:00+00:00'),
    ('2026-10-06', '2026-10-07T00:20:00+00:00', '2026-10-07T00:00:00+00:00'),
    ('2026-10-06', '2026-10-06T00:10:00+00:00', '2026-10-05T23:50:00+00:00'),
])
def test_replay_data_window_caps_utc_bounds(target, as_of, expected_end):
    m = _load_replay()
    start, end = m.replay_data_window(target, real_datetime.fromisoformat(as_of))
    assert start == real_datetime.fromisoformat(target + 'T00:00:00+00:00')
    assert end == real_datetime.fromisoformat(expected_end)
    assert start.tzinfo == utc_timezone.utc and end.tzinfo == utc_timezone.utc


def test_replay_data_window_rejects_naive_clock():
    m = _load_replay()
    with pytest.raises((ValueError, m.ReplayDataError)):
        m.replay_data_window('2026-10-06', real_datetime(2026, 10, 6, 20, 28))


@pytest.mark.parametrize('as_of,target,expected_end', [
    ('2026-10-06T20:28:00+00:00', '2026-10-06', '2026-10-06T20:08:00+00:00'),
    ('2026-10-06T20:28:00+00:00', '2026-10-02', '2026-10-03T00:00:00+00:00'),
])
def test_replay_main_uses_single_captured_clock_for_pack_and_provider(
        monkeypatch, tmp_path, as_of, target, expected_end):
    m, calls, _, before = _replay_data_fixture(monkeypatch, tmp_path, as_of=as_of, target=target)
    m.main()
    out = json.loads(m.OUT.read_text())
    assert out['acceptance_status'] == 'ACCEPTED'
    assert out['data_health']['status'] == 'COMPLETE'
    assert out['data_health']['session_complete'] is True
    assert out['query_window']
    assert m._dt(out['query_window']['as_of_utc']) == real_datetime.fromisoformat(as_of)
    assert calls['pack'] and calls['provider']
    expected = real_datetime.fromisoformat(expected_end)
    for _, timeframe, start, end in calls['pack']:
        assert timeframe == '5Min'
        assert start == real_datetime.fromisoformat(target + 'T00:00:00+00:00')
        assert end == expected
    for _, timeframe, start, end in calls['provider']:
        assert timeframe == '5Min'
        assert m._dt(start) == real_datetime.fromisoformat(target + 'T00:00:00+00:00')
        assert m._dt(end) == expected
    assert out['results'][0]['high_at']
    assert out['results'][0]['lookahead_check'] == 'PASS'
    assert out['future_leakage_detected'] is False
    assert out['lookahead_violations'] == []
    _assert_replay_inputs_unchanged(before)


def test_replay_regular_bars_obey_exact_twenty_minute_closed_bar_boundary():
    m = _load_replay()
    session = {'date': '2026-10-06', 'open': clock_time(9, 30), 'close': clock_time(16)}
    # At 14:20, the delayed cutoff is 14:00; the 13:55 bar is closed, 14:00 is not.
    now = real_datetime(2026, 10, 6, 14, 20, tzinfo=utc_timezone.utc)
    _, query_end = m.replay_data_window(session['date'], now)
    bars = [_replay_bar('2026-10-06T13:55:00Z'), _replay_bar('2026-10-06T14:00:00Z')]
    visible, cutoff = m.bars_available_at(m.regular_session_bars(bars, session), now)
    assert cutoff == query_end and visible == bars[:1]


def test_replay_response_bar_after_query_end_never_enters_health_or_outcome(monkeypatch, tmp_path):
    m, calls, data, before = _replay_data_fixture(monkeypatch, tmp_path, as_of='2026-10-06T14:20:00+00:00')
    # Even an over-generous provider response must be clipped before acceptance.
    data['MOVER'][-1]['h'] = 9_999.0
    with pytest.raises(m.ReplayDataError):
        m.main()
    out = json.loads(m.OUT.read_text())
    assert out['acceptance_status'] == 'REJECTED_DATA'
    assert out['results'] == []
    assert out['data_health']['status'] == 'WAITING_FOR_SESSION_DATA'
    assert out['data_health']['regular_session_bar_counts'] == {'MOVER': 6, 'SPY': 6, 'QQQ': 6}
    assert out['data_health']['session_complete'] is False
    for _, _, _, end in calls['provider']:
        assert m._dt(end) == real_datetime.fromisoformat('2026-10-06T14:00:00+00:00')
    _assert_replay_inputs_unchanged(before)


@pytest.mark.parametrize('target,as_of,close,expected_complete,expected_count', [
    ('2026-03-06', '2026-03-06T21:20:00+00:00', clock_time(16), True, 78),
    ('2026-03-09', '2026-03-09T20:20:00+00:00', clock_time(16), True, 78),
    ('2026-11-27', '2026-11-27T18:20:00+00:00', clock_time(13), True, 42),
    ('2026-11-27', '2026-11-27T18:19:59+00:00', clock_time(13), False, 41),
])
def test_replay_exchange_close_follows_dst_and_early_close(
        monkeypatch, tmp_path, target, as_of, close, expected_complete, expected_count):
    m, _, _, before = _replay_data_fixture(monkeypatch, tmp_path, target=target, as_of=as_of, close=close)
    if expected_complete:
        m.main()
    else:
        with pytest.raises(m.ReplayDataError):
            m.main()
    out = json.loads(m.OUT.read_text())
    assert out['data_health']['session_complete'] is expected_complete
    assert set(out['data_health']['regular_session_bar_counts'].values()) == {expected_count}
    assert out['acceptance_status'] == ('ACCEPTED' if expected_complete else 'REJECTED_DATA')
    _assert_replay_inputs_unchanged(before)


@pytest.mark.parametrize('status_code', [403, 429])
def test_replay_provider_http_errors_write_rejected_diagnostic_then_raise(
        monkeypatch, tmp_path, status_code):
    import urllib.error
    m, _, _, before = _replay_data_fixture(monkeypatch, tmp_path)

    def provider(*args):
        raise urllib.error.HTTPError('https://data.alpaca.markets/v2/stocks/bars', status_code,
                                     'offline fixture', {}, None)

    monkeypatch.setattr(m, 'alpaca', provider)
    with pytest.raises(m.ReplayDataError):
        m.main()
    out = json.loads(m.OUT.read_text())
    assert out['acceptance_status'] == 'REJECTED_DATA'
    assert out['results'] == []
    assert out['data_health']['status'] == 'ERROR'
    assert out['errors'] and str(status_code) in out['errors'][0]['message']
    assert set(out['data_health']['missing_symbols']) == {'MOVER', 'SPY', 'QQQ'}
    _assert_replay_inputs_unchanged(before)


@pytest.mark.parametrize('missing', [('MOVER',), ('SPY',), ('QQQ',), ('MOVER', 'SPY', 'QQQ')])
def test_replay_missing_required_bars_is_partial_not_an_accepted_missed_opportunity(
        monkeypatch, tmp_path, missing):
    m, _, data, before = _replay_data_fixture(monkeypatch, tmp_path)
    for symbol in missing:
        data[symbol] = []
    with pytest.raises(m.ReplayDataError):
        m.main()
    out = json.loads(m.OUT.read_text())
    assert out['acceptance_status'] == 'REJECTED_DATA'
    assert out['results'] == []
    assert out['data_health']['status'] == 'PARTIAL'
    assert set(out['data_health']['missing_symbols']) == set(missing)
    assert all(out['data_health']['regular_session_bar_counts'][symbol] == 0 for symbol in missing)
    _assert_replay_inputs_unchanged(before)


def test_replay_only_regular_session_bars_satisfy_required_data(monkeypatch, tmp_path):
    m, _, data, _ = _replay_data_fixture(monkeypatch, tmp_path)
    data['MOVER'] = [_replay_bar('2026-10-06T12:00:00Z'), _replay_bar('2026-10-06T20:00:00Z')]
    with pytest.raises(m.ReplayDataError):
        m.main()
    out = json.loads(m.OUT.read_text())
    assert out['data_health']['missing_symbols'] == ['MOVER']
    assert out['data_health']['regular_session_bar_counts']['MOVER'] == 0
    assert out['results'] == []


def test_replay_required_cohort_excludes_nonmovers_old_surges_and_holdings(monkeypatch, tmp_path):
    m, calls, _, before = _replay_data_fixture(monkeypatch, tmp_path)
    m.main()
    out = json.loads(m.OUT.read_text())
    assert set(out['data_health']['required_symbols']) == {'MOVER', 'SPY', 'QQQ'}
    assert out['data_health']['cohort_status'] == 'QUALIFYING_MOVERS'
    assert {s for symbols, *_ in calls['provider'] for s in symbols} == {'MOVER', 'SPY', 'QQQ'}
    assert [r['symbol'] for r in out['results']] == ['MOVER']
    _assert_replay_inputs_unchanged(before)


@pytest.mark.parametrize('missing_benchmark', [None, 'SPY', 'QQQ'])
def test_replay_no_qualifying_movers_is_distinct_from_missing_required_data(
        monkeypatch, tmp_path, missing_benchmark):
    m, _, data, before = _replay_data_fixture(monkeypatch, tmp_path, mover=False)
    if missing_benchmark:
        data[missing_benchmark] = []
        with pytest.raises(m.ReplayDataError):
            m.main()
    else:
        m.main()
    out = json.loads(m.OUT.read_text())
    assert out['data_health']['cohort_status'] == 'NO_QUALIFYING_MOVERS'
    assert set(out['data_health']['required_symbols']) == {'SPY', 'QQQ'}
    assert out['results'] == []
    assert out['acceptance_status'] == ('REJECTED_DATA' if missing_benchmark else 'ACCEPTED')
    assert out['data_health']['missing_symbols'] == ([missing_benchmark] if missing_benchmark else [])
    _assert_replay_inputs_unchanged(before)


@pytest.mark.parametrize('target,as_of,status,no_request', [
    ('2026-10-07', '2026-10-06T20:28:00+00:00', 'ERROR', True),
    ('2026-10-06', '2026-10-06T00:10:00+00:00', 'ERROR', True),
    ('2026-10-06', '2026-10-06T13:40:00+00:00', 'WAITING_FOR_SESSION_DATA', False),
])
def test_replay_future_or_predata_session_is_never_accepted(
        monkeypatch, tmp_path, target, as_of, status, no_request):
    m, calls, _, before = _replay_data_fixture(monkeypatch, tmp_path, target=target, as_of=as_of)
    with pytest.raises(m.ReplayDataError):
        m.main()
    out = json.loads(m.OUT.read_text())
    assert out['acceptance_status'] == 'REJECTED_DATA'
    assert out['results'] == []
    assert out['data_health']['status'] == status
    if no_request:
        assert calls['provider'] == []
    _assert_replay_inputs_unchanged(before)


def test_replay_lookahead_failure_never_keeps_accepted_status(monkeypatch, tmp_path):
    m, _, _, before = _replay_data_fixture(monkeypatch, tmp_path)

    def fail_invariance(*args):
        raise m.LookaheadViolation('offline future-mutation failure')

    monkeypatch.setattr(m, 'future_mutation_invariance', fail_invariance)
    with pytest.raises(m.LookaheadViolation):
        m.main()
    out = json.loads(m.OUT.read_text())
    assert out['acceptance_status'] != 'ACCEPTED'
    assert out['future_leakage_detected'] is True
    assert out['lookahead_violations']
    _assert_replay_inputs_unchanged(before)


def test_replay_qualifying_qrvo_is_required_without_symbol_specific_exception(monkeypatch, tmp_path):
    m, calls, _, before = _replay_data_fixture(monkeypatch, tmp_path)
    cache = json.loads(m.ss.MARKET_CACHE.read_text())
    cache['bars']['QRVO'][-1]['h'] = 108.0  # The same eight-percent boundary as every stock.
    m.ss.MARKET_CACHE.write_text(json.dumps(cache) + '\n')
    before[m.ss.MARKET_CACHE] = m.ss.MARKET_CACHE.read_bytes()
    with pytest.raises(m.ReplayDataError):
        m.main()
    out = json.loads(m.OUT.read_text())
    assert out['acceptance_status'] == 'REJECTED_DATA'
    assert set(out['data_health']['required_symbols']) == {'MOVER', 'QRVO', 'SPY', 'QQQ'}
    assert out['data_health']['missing_symbols'] == ['QRVO']
    assert {s for symbols, *_ in calls['provider'] for s in symbols} == {'MOVER', 'QRVO', 'SPY', 'QQQ'}
    assert out['results'] == []
    _assert_replay_inputs_unchanged(before)


def test_fundamentals_main_rotates_persisted_frames_through_every_batch(tmp_path, monkeypatch):
    """A persisted zero cursor must advance; every concept gets a turn without network."""
    m = _load_fundamentals_observer()
    state_path = tmp_path / "portfolio-v1.json"
    out_path = tmp_path / "fundamentals-observer-v1.json"
    state_path.write_text(json.dumps({"positions": {"TEST": {"tranches": [{"price": 10, "notional": 1000}]}}}))
    original_state = state_path.read_bytes()
    monkeypatch.setattr(m, "STATE", state_path)
    monkeypatch.setattr(m, "OUT", out_path)
    monkeypatch.delenv("STOCKFIT_API_KEY", raising=False)
    monkeypatch.setattr(m, "ticker_map", lambda: ({"TEST": {"cik_str": 123}}, []))
    monkeypatch.setattr(m, "sec_companyfacts", lambda cik: ({}, "TEST_ONLY"))
    monkeypatch.setattr(m, "sec_submission", lambda cik: ({}, "TEST_ONLY"))

    def no_network(*args, **kwargs):
        raise AssertionError("observer regression must not make live requests")

    monkeypatch.setattr(m.urllib.request, "urlopen", no_network)
    requested = []

    def frame(taxonomy, concept, unit, period):
        requested.append((taxonomy, concept, unit, period))
        return {"data": []}, "TEST_ONLY"

    monkeypatch.setattr(m, "frame_json", frame)
    batches = []
    for expected_index in (0, 1, 2, 3, 4, 0):
        if out_path.exists():
            old = json.loads(out_path.read_text())
            old["market_batch_refreshed_at"] = "2000-01-01T00:00:00+00:00"
            old["market_batch_attempted_at"] = "2000-01-01T00:00:00+00:00"
            out_path.write_text(json.dumps(old))
        requested.clear()
        m.main()
        result = json.loads(out_path.read_text())
        assert result["frames_transport"]["batch_index"] == expected_index
        assert result["frames_transport"]["requests"] <= m.FRAME_REQUEST_BUDGET
        assert result["strategy_effect"] is False
        assert result["evidence_complete"] == 0
        assert result["status"] == "PARTIAL"
        assert state_path.read_bytes() == original_state
        batches.append(set(requested))

    assert [len(batch) for batch in batches] == [12, 12, 12, 12, 10, 12]
    assert len(set().union(*batches[:5])) == 58
    assert sum(len(batch) for batch in batches[:5]) == 58
    assert batches[5] == batches[0]


def test_fundamentals_http_diagnostics_allowlist_metadata_without_reading_body():
    import io
    from email.message import Message
    m = _load_fundamentals_observer()
    headers = Message()
    headers["WWW-Authenticate"] = 'Bearer realm="PRIVATE", error_description="SECRET"'
    headers["X-Request-ID"] = "12345678-abcd-1234-abcd-123456789abc"
    headers["CF-Ray"] = "0123456789abcdef-LHR"
    headers["Retry-After"] = "30"
    headers["Set-Cookie"] = "SECRET"
    body = io.BytesIO(b"SECRET RESPONSE BODY")
    error = m.urllib.error.HTTPError(
        "https://r.jina.ai/https://data.sec.gov/api?token=SECRET",
        401, "SECRET REASON", headers, body,
    )
    details = m.transport_error_details(error)
    assert details == {
        "type": "HTTPError", "http_status": 401, "endpoint": "JINA_READER",
        "auth_challenge_scheme": "bearer",
        "request_id": "12345678-abcd-1234-abcd-123456789abc",
        "cf_ray": "0123456789abcdef-LHR", "retry_after_seconds": 30,
    }
    assert body.tell() == 0
    assert "SECRET" not in json.dumps(details)
    for name in ("X-Request-ID", "CF-Ray", "Retry-After", "WWW-Authenticate"):
        headers.replace_header(name, "SECRET")
    assert m.transport_error_details(error) == {
        "type": "HTTPError", "http_status": 401, "endpoint": "JINA_READER",
    }
    assert m.transport_error_details(ValueError("SECRET")) == {"type": "ValueError"}


def test_fundamentals_frames_keep_failure_task_and_http_status(monkeypatch):
    m = _load_fundamentals_observer()
    def fail(*args):
        raise m.urllib.error.HTTPError("https://r.jina.ai/https://data.sec.gov/example", 401, "SECRET", {}, None)
    monkeypatch.setattr(m, "frame_json", fail)
    evidence, status = m.frame_evidence_by_cik(1)
    assert evidence == {}
    assert status["requests"] == 12
    assert len(status["errors"]) == 12
    assert len({tuple(sorted(e["task"].items())) for e in status["errors"]}) == 12
    assert all(e["http_status"] == 401 and e["endpoint"] == "JINA_READER" for e in status["errors"])
    assert "SECRET" not in json.dumps(status)


def test_fundamentals_main_keeps_cik_http_status_without_secrets(tmp_path, monkeypatch):
    m = _load_fundamentals_observer()
    state_path = tmp_path / "portfolio-v1.json"
    out_path = tmp_path / "fundamentals-observer-v1.json"
    state_path.write_text(json.dumps({"positions": {"TEST": {}}}))
    original_state = state_path.read_bytes()
    monkeypatch.setattr(m, "STATE", state_path)
    monkeypatch.setattr(m, "OUT", out_path)
    monkeypatch.delenv("STOCKFIT_API_KEY", raising=False)
    calls = []
    def denied(request, timeout):
        calls.append(request.full_url)
        code = 401 if request.host == "r.jina.ai" else 403
        raise m.urllib.error.HTTPError(request.full_url, code, "SECRET", {}, None)
    monkeypatch.setenv("SEC_USER_AGENT", "Observer Fixture contact@example.invalid")
    monkeypatch.setenv("SEC_TRANSPORT_STATE", str(tmp_path / "sec-budget.sqlite3"))
    monkeypatch.setattr(m, "_sec_urlopen", denied)
    monkeypatch.setattr(m.urllib.request, "urlopen", lambda req, **kwargs: denied(req, kwargs["timeout"]))
    monkeypatch.setattr(m.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(m, "frame_evidence_by_cik", lambda batch: ({}, {"errors": [], "batch_index": batch}))
    m.main()
    result = json.loads(out_path.read_text())
    assert len(calls) == 3  # repeated denied SEC host is suppressed; proxy/EFTS unchanged
    assert [e["http_status"] for e in result["mapping_errors"]] == [403, 403, 401]
    assert result["mapping_errors"][-1]["endpoint"] == "JINA_READER"
    assert result["errors"] == [{
        "symbol": "TEST", "stage": "CIK_RESOLUTION", "type": "HTTPError",
        "http_status": 403, "endpoint": "SEC_EFTS",
    }]
    assert result["status"] == "PARTIAL" and result["strategy_effect"] is False
    assert state_path.read_bytes() == original_state
    assert "SECRET" not in json.dumps(result)


import pytest

def _sec_fixture_module(monkeypatch, tmp_path):
    m = _load_fundamentals_observer()
    monkeypatch.setenv("SEC_USER_AGENT", "Observer Fixture contact@example.invalid")
    monkeypatch.setenv("SEC_TRANSPORT_STATE", str(tmp_path / "sec-budget.sqlite3"))
    monkeypatch.setattr(m, "SEC_REQUEST_INTERVAL", 0)
    return m


def _sec_fixture_response(body, encoding="identity", media="application/json", status=200, length=None):
    import io
    from email.message import Message
    response = io.BytesIO(body)
    response.status = status
    response.headers = Message()
    response.headers["Content-Type"] = media
    response.headers["Content-Encoding"] = encoding
    if length is not None:
        response.headers["Content-Length"] = str(length)
    return response


@pytest.mark.parametrize("encoding", ["identity", "gzip", "deflate", "deflate-raw"])
def test_sec_contact_and_compressed_json_roundtrip(monkeypatch, tmp_path, encoding):
    import gzip, zlib
    m = _sec_fixture_module(monkeypatch, tmp_path)
    expected = {"data": [{"cik": 123, "val": 42, "end": "2026-06-30"}]}
    plain = json.dumps(expected).encode()
    if encoding == "gzip": body = gzip.compress(plain)
    elif encoding == "deflate": body = zlib.compress(plain)
    elif encoding == "deflate-raw":
        compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
        body = compressor.compress(plain) + compressor.flush()
    else: body = plain
    seen = []
    def request(req, timeout):
        seen.append(req)
        return _sec_fixture_response(body, "deflate" if encoding == "deflate-raw" else encoding)
    monkeypatch.setattr(m, "_sec_urlopen", request)
    assert m.get("https://data.sec.gov/api/xbrl/frames/us-gaap/Test/USD/CY2026Q2.json") == expected
    assert seen[0].get_header("User-agent") == "Observer Fixture contact@example.invalid"
    assert seen[0].get_header("Accept-encoding") == "gzip, deflate"
    assert "contact@" not in m.UA  # Other providers never receive the SEC contact.


def test_sec_missing_invalid_contact_and_redirect_do_not_send(monkeypatch, tmp_path):
    m = _sec_fixture_module(monkeypatch, tmp_path)
    seen = []
    monkeypatch.setattr(m, "_sec_urlopen", lambda *args: seen.append(args))
    monkeypatch.delenv("SEC_USER_AGENT")
    with pytest.raises(m.SECTransportError, match="SEC_CONTACT_NOT_CONFIGURED"):
        m.get("https://www.sec.gov/files/company_tickers.json")
    for value in ("not-a-contact", "Fixture contact@example.invalid\r\nX-Test: injected"):
        monkeypatch.setenv("SEC_USER_AGENT", value)
        with pytest.raises(m.SECTransportError, match="SEC_CONTACT_INVALID"):
            m.get("https://www.sec.gov/files/company_tickers.json")
    monkeypatch.setenv("SEC_USER_AGENT", "Observer Fixture contact@example.invalid")
    monkeypatch.setenv("SEC_TRANSPORT_STATE", str(tmp_path / "sec-budget.sqlite3"))
    for url in ("https://example.invalid/", "http://www.sec.gov/", "https://www.sec.gov.evil.invalid/"):
        with pytest.raises(m.SECTransportError, match="SEC_URL_NOT_ALLOWED"):
            m._sec_open(url, "application/json")
        req = m.urllib.request.Request("https://www.sec.gov/files/company_tickers.json")
        with pytest.raises(m.SECTransportError, match="SEC_URL_NOT_ALLOWED"):
            m._SECRedirectHandler().redirect_request(req, None, 302, "", {}, url)
    assert seen == []


def test_sec_compressed_and_json_failures_are_bounded_and_classified(monkeypatch, tmp_path):
    import gzip
    m = _sec_fixture_module(monkeypatch, tmp_path)
    monkeypatch.setattr(m, "SEC_MAX_WIRE_BYTES", 128)
    monkeypatch.setattr(m, "SEC_MAX_DECODED_BYTES", 256)
    good = b'{"data":[]}'
    zipped = gzip.compress(good)
    cases = [
        (b"<html>denied</html>", "identity", "text/html", 200, None, "SEC_JSON_CONTENT_TYPE_INVALID"),
        (b"<html>denied</html>", "identity", "application/json", 200, None, "SEC_JSON_HTML_RESPONSE"),
        (b"not json", "identity", "application/json", 200, None, "SEC_JSON_INVALID"),
        (b"[]", "identity", "application/json", 200, None, "SEC_JSON_SCHEMA_INVALID"),
        (b'{"error":"denied"}', "identity", "application/json", 200, None, "SEC_JSON_SCHEMA_INVALID"),
        (good, "br", "application/json", 200, None, "SEC_CONTENT_ENCODING_UNSUPPORTED"),
        (good, "identity", "application/json", 206, None, "SEC_HTTP_STATUS_UNEXPECTED"),
        (b"x"*129, "identity", "application/json", 200, None, "SEC_WIRE_SIZE_LIMIT"),
        (good, "identity", "application/json", 200, 129, "SEC_WIRE_SIZE_LIMIT"),
        (good, "identity", "application/json", 200, 30, "SEC_WIRE_LENGTH_MISMATCH"),
        (gzip.compress(b"x"*4096), "gzip", "application/json", 200, None, "SEC_DECODED_SIZE_LIMIT"),
        (zipped[:-2], "gzip", "application/json", 200, None, "SEC_COMPRESSED_STREAM_INVALID"),
        (zipped+b"trailing", "gzip", "application/json", 200, None, "SEC_COMPRESSED_STREAM_INVALID"),
    ]
    for body, encoding, media, status, length, expected in cases:
        monkeypatch.setattr(m, "_sec_urlopen", lambda req, timeout: _sec_fixture_response(body, encoding, media, status, length))
        with pytest.raises(m.SECTransportError) as caught:
            m.get("https://data.sec.gov/api/xbrl/frames/us-gaap/Test/USD/CY2026Q2.json")
        assert m.transport_error_details(caught.value) == {"type": "SECTransportError", "code": expected}


def test_sec_request_pacing_and_cached_host_denial(monkeypatch, tmp_path):
    m = _sec_fixture_module(monkeypatch, tmp_path)
    clock = [0.0]
    waits = []
    monkeypatch.setattr(m, "SEC_REQUEST_INTERVAL", 0.2)
    monkeypatch.setattr(m.time, "time", lambda: clock[0])
    def sleep(seconds):
        waits.append(seconds)
        clock[0] += seconds
    monkeypatch.setattr(m.time, "sleep", sleep)
    for _ in range(3): m._sec_start("https://www.sec.gov/files/company_tickers.json")
    assert waits == pytest.approx([0.2, 0.2])
    calls = []
    def denied(req, timeout):
        calls.append(req.full_url)
        raise m.urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)
    monkeypatch.setattr(m, "_sec_urlopen", denied)
    for _ in range(2):
        with pytest.raises(m.urllib.error.HTTPError) as caught:
            m.get("https://efts.sec.gov/LATEST/search-index?q=TEST")
    assert len(calls) == 1
    assert m.transport_error_details(caught.value)["request_sent"] is False


@pytest.mark.parametrize("payload", [None, {"data": [None]},
    {"data": [{"cik": 123, "val": 9, "end": "2026-06-30"}, None]}, {"data": []}])
def test_sec_failed_refresh_preserves_evidence_age_and_attempt_cooldown(tmp_path, monkeypatch, payload):
    m = _sec_fixture_module(monkeypatch, tmp_path)
    state = tmp_path / "portfolio-v1.json"
    out = tmp_path / "fundamentals-observer-v1.json"
    state.write_text(json.dumps({"positions": {"TEST": {}}}))
    state_before = state.read_bytes()
    stamp = "2000-01-01T00:00:00+00:00"
    prior = {"cash": {"values": [{"end": "1999-12-31", "val": 123}]}}
    out.write_text(json.dumps({
        "market_batch_schema_version": m.MARKET_BATCH_SCHEMA_VERSION,
        "market_batch_refreshed_at": stamp, "frames_refreshed_at": stamp,
        "frames_transport": {"batch_index": 0},
        "companies": {"TEST": {"financial_evidence": prior, "updated_at": stamp,
                                  "gap_refresh_at": stamp, "source_evidence_at": stamp}},
    }))
    monkeypatch.setattr(m, "STATE", state)
    monkeypatch.setattr(m, "OUT", out)
    monkeypatch.delenv("STOCKFIT_API_KEY", raising=False)
    monkeypatch.setattr(m, "ticker_map", lambda: ({"TEST": {"cik_str": 123}}, []))
    calls = []
    def invalid(*args):
        calls.append(args)
        raise m.SECTransportError("SEC_JSON_HTML_RESPONSE")
    monkeypatch.setattr(m, "frame_json", invalid if payload is None else lambda *args: (payload, "FIXTURE"))
    monkeypatch.setattr(m, "sec_companyfacts", invalid)
    monkeypatch.setattr(m, "sec_submission", invalid)
    m.main()
    result = json.loads(out.read_text())
    assert result["market_batch_refreshed_at"] == stamp
    assert result["frames_refreshed_at"] == stamp
    assert result["market_batch_attempted_at"] != stamp
    assert result["frames_transport"]["batch_index"] == 1
    assert result["frames_transport"]["successful_requests"] == 0
    assert result["companies"]["TEST"]["financial_evidence"]["cash"] == prior["cash"]
    assert result["status"] == "PARTIAL"
    assert result["refreshed_this_run"] == 0
    for field in ("updated_at", "gap_refresh_at", "source_evidence_at"):
        assert result["companies"]["TEST"][field] == stamp
    assert result["companies"]["TEST"]["processed_at"] != stamp
    assert state.read_bytes() == state_before
    saved = out.read_bytes()
    calls.clear()
    m.main()
    assert calls == [] and out.read_bytes() == saved


def test_sec_contact_is_excluded_from_preflight_environment():
    from research.stock_shadow.server.runner import test_environment
    env = {"SEC_USER_AGENT": "Observer Fixture contact@example.invalid",
           "SEC_TRANSPORT_STATE": "/fixture/stock-budget.sqlite3", "PATH": "/fixture"}
    assert test_environment(env) == {"PATH": "/fixture"}


@pytest.mark.parametrize("rows", [[None], [{"cik": 123, "val": 1, "end": "2026-06-30"}, None],
    [{"cik": True, "val": 1, "end": "2026-06-30"}],
    [{"cik": 123, "val": float("nan"), "end": "2026-06-30"}],
    [{"cik": 123, "val": 1, "end": "2026-02-30"}], []])
def test_sec_frame_rows_are_atomic_and_empty_is_not_refresh(monkeypatch, rows):
    m = _load_fundamentals_observer()
    monkeypatch.setattr(m, "frame_json", lambda *args: ({"data": rows}, "FIXTURE"))
    evidence, status = m.frame_evidence_by_cik()
    assert evidence == {} and status["successful_requests"] == 0
    assert status["status"] == "PARTIAL"
    if rows:
        assert len(status["errors"]) == 12
        assert all(e["code"] == "SEC_FRAME_ROW_INVALID" for e in status["errors"])
    else:
        assert status["errors"] == [] and status["empty_requests"] == 12


def test_sec_valid_frame_rows_are_counted_after_validation(monkeypatch):
    m = _load_fundamentals_observer()
    monkeypatch.setattr(m, "frame_json", lambda *args: (
        {"data": [{"cik": 123, "val": 42, "end": "2026-06-30"}]}, "FIXTURE"))
    evidence, status = m.frame_evidence_by_cik()
    assert 123 in evidence and status["successful_requests"] == 12
    assert status["status"] == "OK" and not status["errors"]


@pytest.mark.parametrize("failure", ["frames", "mapping"])
def test_sec_transport_failure_keeps_complete_cache_partial(tmp_path, monkeypatch, failure):
    m = _load_fundamentals_observer()
    state = tmp_path / "portfolio.json"
    out = tmp_path / "observer.json"
    state.write_text(json.dumps({"positions": {"TEST": {}}}))
    stamp = "2000-01-01T00:00:00+00:00"
    prior = {field: {"values": [{"end": "1999-12-31", "val": 1}]}
             for field in ("revenue", "net_income", "operating_cash_flow", "cash", "total_debt")}
    out.write_text(json.dumps({"market_batch_schema_version": m.MARKET_BATCH_SCHEMA_VERSION,
        "market_batch_refreshed_at": stamp, "frames_refreshed_at": stamp,
        "companies": {"TEST": {"updated_at": stamp, "financial_evidence": prior,
            "risk_evidence": {"semantic_review_status": "TEXT_VERIFIED"}}}}))
    monkeypatch.setattr(m, "STATE", state)
    monkeypatch.setattr(m, "OUT", out)
    monkeypatch.delenv("STOCKFIT_API_KEY", raising=False)
    monkeypatch.setattr(m, "ticker_map", lambda: ({"TEST": {"cik_str": 123}},
        [{"type": "SECTransportError"}] if failure == "mapping" else []))
    monkeypatch.setattr(m, "frame_evidence_by_cik", lambda *args: ({},
        {"status": "PARTIAL", "errors": [{"code": "SEC_FRAME_ROW_INVALID"}]}
        if failure == "frames" else {"status": "OK", "errors": []}))
    monkeypatch.setattr(m, "sec_submission", lambda *args: ({"filings": {"recent": {}}}, "FIXTURE"))
    monkeypatch.setattr(m, "sec_companyfacts", lambda *args: pytest.fail("complete cache queried facts"))
    m.main()
    result = json.loads(out.read_text())
    assert result["evidence_complete"] == 1 and result["errors"] == []
    assert result["status"] == "PARTIAL" and result["refreshed_this_run"] == 0
    assert result["companies"]["TEST"]["updated_at"] == stamp


def test_sec_shared_budget_missing_corrupt_and_persisted_denial(tmp_path, monkeypatch):
    m = _sec_fixture_module(monkeypatch, tmp_path)
    monkeypatch.setattr(m, "_sec_urlopen", lambda *args: pytest.fail("unexpected network"))
    monkeypatch.delenv("SEC_TRANSPORT_STATE")
    with pytest.raises(m.SECTransportError, match="SEC_SHARED_BUDGET_NOT_CONFIGURED"):
        m.get("https://www.sec.gov/files/company_tickers.json")
    path = tmp_path / "budget.sqlite3"
    monkeypatch.setenv("SEC_TRANSPORT_STATE", str(path))
    path.write_bytes(b"invalid sqlite")
    with pytest.raises(m.SECTransportError, match="SEC_SHARED_BUDGET_UNAVAILABLE"):
        m._sec_start("https://www.sec.gov/")
    path.unlink()
    clock = [10000.0]
    monkeypatch.setattr(m.time, "time", lambda: clock[0])
    m._sec_deny("https://www.sec.gov/")
    restarted = _load_fundamentals_observer()
    with pytest.raises(restarted.urllib.error.HTTPError) as denied:
        restarted._sec_start("https://www.sec.gov/")
    assert restarted.transport_error_details(denied.value)["request_sent"] is False
    clock[0] += m.SEC_DENIAL_BACKOFF_SECONDS + 1
    restarted._sec_start("https://www.sec.gov/")


def test_sec_budget_is_shared_by_concurrent_processes_and_restart(tmp_path):
    import os, subprocess, sys
    path = tmp_path / "shared.sqlite3"
    script = """
import importlib.util, sys, time
spec=importlib.util.spec_from_file_location("observer","research/stock_shadow/fundamentals_observer.py")
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.urllib.request.urlopen=lambda *a,**k: (_ for _ in ()).throw(AssertionError("network forbidden"))
if sys.argv[1]=="deny":
    m._sec_deny("https://www.sec.gov/")
else:
    try:
        m._sec_start("https://www.sec.gov/")
        print(time.time())
    except m.urllib.error.HTTPError as error:
        print("CACHED_DENIAL",m.transport_error_details(error)["request_sent"])
"""
    env = dict(os.environ, SEC_TRANSPORT_STATE=str(path))
    processes = [subprocess.Popen([sys.executable, "-c", script, "start"], env=env,
                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(3)]
    times = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 0, stderr
        times.append(float(stdout.strip()))
    times.sort()
    assert all(b-a >= 0.15 for a,b in zip(times,times[1:]))
    subprocess.run([sys.executable, "-c", script, "deny"], env=env, check=True, timeout=15)
    result = subprocess.run([sys.executable, "-c", script, "start"], env=env,
                            capture_output=True, text=True, check=True, timeout=15)
    assert result.stdout.strip() == "CACHED_DENIAL False"


@pytest.mark.parametrize("payload,code", [
    (b'{"data":[null]}', "SEC_FRAME_ROW_INVALID"),
    (b'{"error":"blocked","data":[]}', "SEC_JSON_SCHEMA_INVALID"),
    (b'Error fetching: {"data":[]}', "SEC_JSON_INVALID"),
    (b'x'*129, "SEC_WIRE_SIZE_LIMIT")])
def test_sec_proxy_json_uses_same_bounds_and_schema(tmp_path, monkeypatch, payload, code):
    m = _sec_fixture_module(monkeypatch, tmp_path)
    monkeypatch.setattr(m, "SEC_MAX_WIRE_BYTES", 128)
    monkeypatch.setattr(m.urllib.request, "urlopen",
                        lambda *a, **k: _sec_fixture_response(payload, media="text/plain"))
    with pytest.raises(m.SECTransportError, match=code):
        m.proxy_json("https://data.sec.gov/api/xbrl/frames/Test.json")


@pytest.mark.parametrize("payload,code", [(b'x'*129, "SEC_WIRE_SIZE_LIMIT"),
    (b'{"error":"blocked"}', "SEC_TEXT_PAYLOAD_INVALID"),
    (b'Error: access denied', "SEC_TEXT_PAYLOAD_INVALID")])
def test_sec_filing_fallback_is_bounded_and_rejects_error_payloads(tmp_path, monkeypatch, payload, code):
    m = _sec_fixture_module(monkeypatch, tmp_path)
    monkeypatch.setattr(m, "SEC_MAX_WIRE_BYTES", 128)
    def denied(*args):
        raise m.urllib.error.HTTPError("https://www.sec.gov/", 403, "denied", {}, None)
    monkeypatch.setattr(m, "_sec_response", denied)
    monkeypatch.setattr(m.urllib.request, "urlopen",
                        lambda *a, **k: _sec_fixture_response(payload, media="text/plain"))
    with pytest.raises(m.SECTransportError, match=code):
        m.sec_filing_text(123, "fixture", "filing.htm")
