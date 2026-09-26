import datetime as dt
import importlib.util
import pathlib
import unittest

spec=importlib.util.spec_from_file_location(
    "hunter_primary_source_discovery",
    pathlib.Path(__file__).resolve().parents[1]/
    "research/hunter_primary_source_discovery.py")
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
NOW=dt.datetime(2026,9,26,10,tzinfo=dt.timezone.utc)
AT=NOW.isoformat()

def dossiers():
    def row(sym):
        return {"asset":sym,"nonprice_observations":{"coingecko_id":sym.lower()}}
    return {"as_of_utc":AT,"early_entry_watchlist":["EARLY"],
            "continuation_watchlist":["CONT"],"dossiers":[row("EARLY"),row("CONT")]}

def data(coin_id):
    return {"id":coin_id,"symbol":coin_id,"links":{
        "homepage":["https://project.example","http://unsafe.example"],
        "whitepaper":"https://project.example/docs",
        "blockchain_site":["https://explorer.example"],
        "repos_url":{"github":["https://github.com/example/project"]}},
        "platforms":{"ethereum":"0x123"}}

class PrimaryDiscoveryTests(unittest.TestCase):
    def test_both_lanes_and_bounded_cache(self):
        calls=[]
        def fake(cid):
            calls.append(cid)
            return data(cid)
        result,cache=m.build(dossiers(),{},NOW,fetcher=fake,budget=2)
        self.assertEqual(calls,["early","cont"])
        self.assertEqual(result["target_count"],2)
        self.assertEqual(result["fresh_fetched"],2)
        self.assertEqual(len(result["leads"]),2)
        self.assertEqual(result["leads"]["EARLY"]["homepage_candidates"],
                         ["https://project.example"])
        self.assertIn("UNVERIFIED",result["leads"]["EARLY"]["evidence_status"])
        result2,_=m.build(dossiers(),cache,NOW,fetcher=fake,budget=2)
        self.assertEqual(len(calls),2)
        self.assertEqual(result2["cache_reused"],2)

    def test_third_party_identity_mismatch_fails_closed(self):
        def wrong(cid):
            d=data(cid);d["id"]="wrong";return d
        result,_=m.build(dossiers(),{},NOW,fetcher=wrong,budget=2)
        self.assertEqual(result["leads"],{})
        self.assertIn("EARLY",result["failures"])

    def test_rate_limit_is_reported_without_fake_leads(self):
        def rate_limited(cid):raise RuntimeError("429")
        result,_=m.build(dossiers(),{},NOW,fetcher=rate_limited,budget=1)
        self.assertEqual(result["fresh_fetched"],1)
        self.assertEqual(len(result["failures"]),1)
        self.assertEqual(result["leads"],{})

    def test_no_coin_id_yields_explicit_empty_report(self):
        d=dossiers()
        for row in d["dossiers"]:row["nonprice_observations"]={}
        result,_=m.build(d,{},NOW,fetcher=lambda x:None)
        self.assertEqual(result["target_count"],0)
        self.assertEqual(result["leads"],{})

    def test_no_non_https_local_links(self):
        self.assertIsNone(m.safe_url("http://project.example"))
        self.assertIsNone(m.safe_url("https://localhost/path"))
        self.assertEqual(m.safe_url("https://project.example"),"https://project.example")

if __name__=="__main__":unittest.main()
