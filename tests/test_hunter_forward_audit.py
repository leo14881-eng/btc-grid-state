import datetime as dt
import importlib.util
import pathlib
import unittest

FILE=pathlib.Path(__file__).resolve().parents[1]/"research/hunter_forward_audit.py"
SPEC=importlib.util.spec_from_file_location("hunter_forward_audit",FILE)
audit=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)
NOW=dt.datetime(2026,9,26,10,tzinfo=dt.timezone.utc)

def coin(sym,price):
    return {"reference_venue":"binance","reference_price":price,
            "pairs":[{"venue":"binance","pair":sym+"USDT","price":price}]}

def inputs(at="2026-09-26T10:00:00+00:00"):
    scan={"binance_complete":True,"as_of_utc":at,
          "coins":{"BTC":coin("BTC",100),"A":coin("A",1),"RALLY":coin("RALLY",2)}}
    research={"universe_scan_as_of_utc":at,"as_of_utc":at,
              "rotation_batch":["A","RALLY"],
              "research_results":{"A":{"researched_at_utc":at,
                  "stage_price_only":"BASELINE","research_attention_signals":[],
                  "observations":{},"missing_facts":["supply unknown"]},
                  "RALLY":{"researched_at_utc":at,
                  "stage_price_only":"POST_MOVE","research_attention_signals":["VOLUME"],
                  "observations":{},"missing_facts":["supply unknown"]}}}
    return scan,research

class AuditTests(unittest.TestCase):
    def test_first_run_freezes_all_market_and_both_research_stages(self):
        s,r=inputs()
        data,summary=audit.build(s,r,{},NOW)
        self.assertEqual(summary["baseline_first_seen_count"],3)
        self.assertEqual(summary["frozen_research_event_count"],2)
        self.assertEqual(summary["outcomes"]["24h"]["research_event_n"],0)
        self.assertEqual(data["research_events"][1]["stage_price_only"],"POST_MOVE")
        self.assertEqual(data["research_events"][1]["reference_price"],2)

    def test_no_backdated_outcome_and_24h_forward_return_relative_btc(self):
        s,r=inputs()
        data,_=audit.build(s,r,{},NOW)
        nexttime=NOW+dt.timedelta(hours=24)
        s["as_of_utc"]=nexttime.isoformat()
        s["coins"]["A"]=coin("A",1.5)
        s["coins"]["BTC"]=coin("BTC",110)
        r["universe_scan_as_of_utc"]=nexttime.isoformat()
        r["as_of_utc"]=nexttime.isoformat()
        r["rotation_batch"]=[]
        updated,summary=audit.build(s,r,data,nexttime)
        a=next(x for x in updated["research_events"] if x["asset"]=="A")
        self.assertEqual(a["outcomes"]["24h"]["asset_return_pct"],50)
        self.assertEqual(a["outcomes"]["24h"]["btc_return_pct"],10)
        self.assertEqual(a["outcomes"]["24h"]["btc_relative_return_pct"],40)
        self.assertEqual(summary["outcomes"]["24h"]["research_event_n"],2)
        self.assertEqual(summary["outcomes"]["7d"]["research_event_n"],0)

    def test_missing_asset_cannot_fabricate_outcome(self):
        s,r=inputs()
        data,_=audit.build(s,r,{},NOW)
        nexttime=NOW+dt.timedelta(days=7)
        s["as_of_utc"]=nexttime.isoformat()
        s["coins"].pop("A")
        r["universe_scan_as_of_utc"]=nexttime.isoformat()
        r["as_of_utc"]=nexttime.isoformat()
        r["rotation_batch"]=[]
        updated,_=audit.build(s,r,data,nexttime)
        a=next(x for x in updated["research_events"] if x["asset"]=="A")
        self.assertNotIn("7d",a["outcomes"])

    def test_stale_research_market_join_fails(self):
        s,r=inputs()
        r["universe_scan_as_of_utc"]="2026-09-20T00:00:00+00:00"
        with self.assertRaisesRegex(ValueError,"timestamp mismatch"):
            audit.build(s,r,{},NOW)

    def test_repeated_hourly_review_does_not_overwrite_original_signal(self):
        s,r=inputs()
        data,_=audit.build(s,r,{},NOW)
        nexttime=NOW+dt.timedelta(hours=1)
        s["as_of_utc"]=nexttime.isoformat()
        r["as_of_utc"]=nexttime.isoformat()
        r["universe_scan_as_of_utc"]=nexttime.isoformat()
        r["research_results"]["RALLY"]["researched_at_utc"]=nexttime.isoformat()
        r["research_results"]["A"]["researched_at_utc"]=nexttime.isoformat()
        r["research_results"]["RALLY"]["research_attention_signals"]=["NEW_SIGNAL"]
        updated,summary=audit.build(s,r,data,nexttime)
        self.assertEqual(summary["frozen_research_event_count"],2)
        self.assertEqual(updated["research_events"][1]["research_attention_signals"],["VOLUME"])

    def test_no_btc_baseline_fails(self):
        s,r=inputs()
        s["coins"].pop("BTC")
        with self.assertRaisesRegex(ValueError,"Incomplete Binance/BTC"):
            audit.build(s,r,{},NOW)

if __name__=="__main__":unittest.main()
