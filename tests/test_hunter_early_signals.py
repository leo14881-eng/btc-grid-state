import datetime as dt
from research.hunter_early_signals import score_row,build

def test_relative_signal_beats_absolute_gain():
    r1={"BTCUSDT":{"return_pct":1.0,"quote_volume":1},"AAAUSDT":{"return_pct":2.2,"quote_volume":1}}
    r4={"BTCUSDT":{"return_pct":0.5,"quote_volume":1},"AAAUSDT":{"return_pct":2.6,"quote_volume":1}}
    x=score_row("AAAUSDT","AAA",r1,r4,r1["BTCUSDT"],r4["BTCUSDT"])
    assert x["stage"]=="EARLY"
    assert x["btc_relative_1h_pct"]>0
    assert x["independent_signal_count"]>=2

def test_btc_rally_does_not_create_false_alt_strength():
    r1={"BTCUSDT":{"return_pct":3.0,"quote_volume":1},"AAAUSDT":{"return_pct":3.2,"quote_volume":1}}
    r4={"BTCUSDT":{"return_pct":4.0,"quote_volume":1},"AAAUSDT":{"return_pct":4.2,"quote_volume":1}}
    x=score_row("AAAUSDT","AAA",r1,r4,r1["BTCUSDT"],r4["BTCUSDT"])
    assert x["stage"]=="WATCH"
