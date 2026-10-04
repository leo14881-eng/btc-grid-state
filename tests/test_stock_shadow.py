# V3 frozen baseline: 2026-10-04 through 2026-11-04; strategy retuning disabled during forward sample window.
import importlib.util
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
    assert ss.MAX_REQUEST_TARGET_CHARS == 7000\n    assert hasattr(ss,"MARKET_CACHE")
    assert ss.ALPACA_BARS_URL.startswith("https://data.alpaca.markets/")


def _m(price=50,dv=50_000_000,r5=4,r20=12,sma20=48,vol=2.5,volume_ratio=1.0,range_pos=0.6):
    return {"price":price,"avg_dollar_volume20":dv,"ret5":r5,"ret20":r20,"sma20":sma20,"daily_volatility20":vol,
            "volume_ratio20":volume_ratio,"range_position20":range_pos}

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
    assert m.BATCH_SIZE >= 20
    assert m.MAX_BATCHES <= 12

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
    assert '_alpaca_batch_bars(["SPY","QQQ"])' in src
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
    assert '"1Day"' in src
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
    assert "DAILY_CACHE_KEEP_BARS = 35" in src
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
