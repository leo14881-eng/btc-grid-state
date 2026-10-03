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
    assert ss.MAX_MARKET_WORKERS >= 8


def _m(price=50,dv=50_000_000,r5=4,r20=12,sma20=48,vol=2.5):
    return {"price":price,"avg_dollar_volume20":dv,"ret5":r5,"ret20":r20,"sma20":sma20,"daily_volatility20":vol}

def test_selector_rejects_penny_and_illiquid_noise():
    assert "PRICE_TOO_LOW" in ss.entry_decision(_m(price=0.5))["rejects"]
    assert "LOW_DOLLAR_VOLUME" in ss.entry_decision(_m(dv=500_000))["rejects"]

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
