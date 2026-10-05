"""Read-only differential replay and bounded public endpoint measurements.

Run from the adapter checkout; baseline is a separate immutable checkout.
All portfolios written by replay are temporary. Never runs a trading workflow.
"""
import argparse
import contextlib
import copy
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

# Only source annotations newly introduced by the adapter are excluded.
# All actions, reasons, prices, quantities, fees, P&L and risk state are compared.
ANNOTATIONS = {"execution_venue", "execution_pair", "market_type", "fill_model",
               "book_venue", "book_pair", "signal_venue"}

def normalized(value):
    if isinstance(value, dict):
        return {k: normalized(v) for k, v in value.items() if k not in ANNOTATIONS}
    if isinstance(value, list):
        return [normalized(v) for v in value]
    return value

def snapshot(frozen):
    sys.path.insert(0, str(Path.cwd()))
    from research import hunter_position_monitor as monitor
    from tests.test_hunter_position_monitor import PositionMonitorTests
    eng = monitor.eng
    now = dt.datetime(2026, 10, 4, tzinfo=dt.timezone.utc)
    results = {}
    with tempfile.TemporaryDirectory() as tmp:
        for v1 in (True, False):
            for scenario in ("add", "flat", "runner", "profit_exit", "hard_failure",
                             "risk_freeze", "missing_price", "missing_evidence"):
                price = {"add":90, "flat":100, "runner":120, "profit_exit":120,
                         "hard_failure":50}.get(scenario, 90)
                state, market, review, liq, supply = PositionMonitorTests().fixture(price)
                if scenario == "profit_exit":
                    review["candidates"][0]["signal"].update(btc_relative_1h_pct=-1,
                        btc_relative_4h_pct=-1, relative_acceleration_pct=-1)
                if scenario == "hard_failure":
                    liq["snapshots"]["X"].update(spread_bps=300,
                        bid_depth_2pct_usdt=100, ask_depth_2pct_usdt=100)
                if scenario == "risk_freeze":
                    state["systemic_risk"].update(level="HIGH", raw_level="HIGH")
                if scenario == "missing_price": market.pop("X")
                if scenario == "missing_evidence": review["candidates"] = []
                path = Path(tmp)/"portfolio.json"
                eng.atomic_json_write(path, state)
                with patch.object(eng, "backfill_opportunity_history", lambda p,n:p):
                    out = monitor.run_lane(path, "V1" if v1 else "SHADOW_V2", market,
                        review, liq, supply, now, v1)
                results[("V1" if v1 else "V2")+"_"+scenario] = {
                    "out":out, "state":monitor.load(path),
                    "summary":monitor.load(path.with_name("portfolio-summary.json"))}

            for scenario in ("new_buy", "new_buy_risk_freeze", "new_buy_full_pool"):
                monitor.configure_lane(v1)
                state, market, review, liq, supply = PositionMonitorTests().fixture(100)
                state["open_positions"] = []
                if scenario == "new_buy_risk_freeze":
                    state["systemic_risk"].update(level="HIGH",raw_level="HIGH")
                if scenario == "new_buy_full_pool":
                    state["open_positions"] = [{"asset":"OTHER","tranches":[{
                        "price":1,"notional_usdt":19000}]}]
                candidate = review["candidates"][0]
                scan = {"coins":market}
                action, reasons, evidence = eng.decision(candidate,scan,liq,supply,"ENTRY")
                pos = {"asset":"X","shadow_id":"FIXED-BUY-ID","tranches":[],
                       "opened_at_utc":now.isoformat(),"btc_entry_price":100,
                       "last_price":100,"mfe_pct":0,"mae_pct":0}
                buys = eng.execute_capital_proposals(state,[{"kind":"BUY","asset":"X",
                    "pos":pos,"amount":1000,"price":100,"evidence":evidence,
                    "reasons":reasons,"candidate":candidate}],scan,now)
                results[("V1" if v1 else "V2")+"_"+scenario] = {
                    "entry_action":action,"buys":buys,"state":state,
                    "capital":eng.capital_snapshot(state,scan)}

        # Replay the same frozen main portfolios, candidates and marks in both versions.
        root = Path(frozen)/"research/results"
        read = lambda name: json.loads((root/name).read_text())
        scan = read("hunter-cex-universe-run.json")
        review = read("hunter-tactical-capital-review.json")
        liq = read("hunter-liquidity-probe.json")
        supply = read("hunter-tactical-supply-risk.json")
        early = read("hunter-early-signals.json")
        exclusions = set(scan["venue_status"]["binance"]["excluded_bstocks"])
        now = dt.datetime.fromisoformat(early["as_of_utc"].replace("Z", "+00:00"))
        by = {c["asset"]:c for c in review["candidates"]}
        for v1, filename in ((True,"hunter-shadow-portfolio.json"),
                             (False,"hunter-shadow-v2-portfolio.json")):
            state = read(filename)
            current_review = copy.deepcopy(review)
            if v1:
                held = {p["asset"] for p in state["open_positions"]}
                signals = {s["base"]:s for s in early["early"]
                           if s.get("execution_supported") is not False}
                signals.update({s["base"]:s for s in early["all_signals"] if s["base"] in held})
                candidates = []
                for asset, signal in signals.items():
                    if asset in exclusions:continue
                    candidate = copy.deepcopy(by.get(asset, {"asset":asset,"blockers":[]}))
                    candidate["signal"] = signal
                    candidate["signal_evidence"] = eng.stamp(asset,early["scan_generation_id"],early["as_of_utc"])
                    candidates.append(candidate)
                current_review["candidates"] = candidates
            path = Path(tmp)/"portfolio.json"
            eng.atomic_json_write(path, state)
            with patch.object(eng, "backfill_opportunity_history", lambda p,n:p):
                out = monitor.run_lane(path, "V1" if v1 else "SHADOW_V2", scan["coins"],
                    current_review, liq, supply, now, v1, exclusions, regime_scan=scan)
            results[("V1" if v1 else "V2")+"_frozen_main_portfolio"] = {
                "out":out, "state":monitor.load(path),
                "summary":monitor.load(path.with_name("portfolio-summary.json"))}
    print(json.dumps(normalized(results), sort_keys=True, allow_nan=False))

def differential(baseline):
    script = str(Path(__file__).resolve())
    versions = []
    for cwd in (baseline, str(Path.cwd())):
        env = {**os.environ, "HUNTER_VENUE_EXECUTION_ADAPTERS_ENABLED":"0"}
        run = subprocess.run([sys.executable,script,"--snapshot",baseline],cwd=cwd,
                             env=env,text=True,capture_output=True,timeout=120)
        if run.returncode:raise RuntimeError(run.stderr[-4000:])
        versions.append(json.loads(run.stdout))
    differences = [name for name in versions[0] if versions[0][name]!=versions[1][name]]
    report = {"baseline":os.getenv("FROZEN_MAIN_SHA"), "cases":len(versions[0]),
              "differing_cases":differences, "historical_backfill":"DISABLED_IDENTICALLY",
              "ignored_source_annotation_keys":sorted(ANNOTATIONS)}
    root = Path(baseline)/"research/results"
    report["frozen_input_sha256"] = {name:hashlib.sha256((root/name).read_bytes()).hexdigest()
        for name in ("hunter-shadow-portfolio.json","hunter-shadow-v2-portfolio.json",
                     "hunter-cex-universe-run.json","hunter-early-signals.json",
                     "hunter-tactical-capital-review.json","hunter-liquidity-probe.json",
                     "hunter-tactical-supply-risk.json")}
    for name in versions[0]:
        if name.endswith("frozen_main_portfolio"):
            report[name] = {"baseline":versions[0][name]["out"],
                           "adapter":versions[1][name]["out"],
                           "net_pnl_usdt":versions[1][name]["summary"]["net_pnl_usdt"]}
    print("DIFFERENTIAL_REPORT "+json.dumps(report,sort_keys=True))
    if differences:
        for name in differences:
            print("DIFF "+name+" "+json.dumps({"baseline":versions[0][name],"adapter":versions[1][name]})[:12000])
        raise SystemExit("ECONOMIC_OR_DECISION_DIFFERENCE")

def live():
    sys.path.insert(0,str(Path.cwd()))
    from research import hunter_market as m
    records = []
    def measure(name, action):
        start = time.monotonic()
        try:
            result = action()
            if result is False:raise ValueError("CAPABILITY_NOT_READY")
            record = {"name":name,"ok":True,"items":len(result) if hasattr(result,"__len__") else None}
        except Exception as exc:
            record = {"name":name,"ok":False,"error":str(exc)[:180]}
        record["elapsed_ms"] = round((time.monotonic()-start)*1000,1)
        records.append(record)
    bybit, binance = m.Bybit(), m.Binance()
    for _ in range(3):
        measure("binance_quote_batch",binance.quotes)
        measure("bybit_quote_batch",bybit.quotes)
    measure("worker_health",lambda:m.request(bybit.host+"/health"))
    measure("bybit_v3_ready",bybit.ready)
    def books():
        found, errors = bybit.books(["BTCUSDT","ETHUSDT","FLUIDUSDT"])
        if errors:raise ValueError(json.dumps(errors))
        return found
    measure("bybit_v3_books",books)
    measure("bybit_v3_history",lambda:bybit.candles("BTCUSDT",5,2))
    # Public GET/market data only. An absent v3 deployment is an admission failure,
    # not a reason to modify the live Worker or enable the adapter.
    print("LIVE_PUBLIC_API_REPORT "+json.dumps({"records":records,
        "v3_admission":all(r["ok"] for r in records if r["name"].startswith("bybit_v3")),
        "benchmark_limit":"SMALL_SAMPLE_NOT_PRODUCTION_LOAD_TEST"},sort_keys=True))

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot")
    parser.add_argument("--baseline")
    parser.add_argument("--live",action="store_true")
    args = parser.parse_args()
    if args.snapshot:snapshot(args.snapshot)
    if args.baseline:differential(args.baseline)
    if args.live:live()
