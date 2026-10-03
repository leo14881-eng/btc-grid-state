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


def _m(price=50,dv=50_000_000,r5=4,r20=12,sma20=48,vol=2.5,volume_ratio=1.0,range_pos=0.6):
    return {"price":price,"avg_dollar_volume20":dv,"ret5":r5,"ret20":r20,"sma20":sma20,"daily_volatility20":vol,
            "volume_ratio20":volume_ratio,"range_position20":range_pos}

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

def test_position_monitor_market_hours_gate():
    m=_load_position_monitor()
    from datetime import datetime, timezone
    assert m.market_open(datetime(2026,10,5,14,0,tzinfo=timezone.utc)) is True
    assert m.market_open(datetime(2026,10,4,14,0,tzinfo=timezone.utc)) is False
