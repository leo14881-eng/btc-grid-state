import datetime as dt
import importlib.util
import pathlib
import json
import tempfile
import urllib.error
import unittest

spec=importlib.util.spec_from_file_location(
    "hunter_identity_audit",
    pathlib.Path(__file__).resolve().parents[1]/"research/hunter_identity_audit.py")
audit=importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)
NOW=dt.datetime(2026,9,26,10,tzinfo=dt.timezone.utc)
AT=NOW.isoformat()

def scan():
    return {"binance_complete":True,"as_of_utc":AT,
        "coins":{"PUMP":{"pairs":[{"pair":"PUMPUSDT"}],"venues":["binance"]},
                 "ABC":{"pairs":[{"pair":"ABCUSDT"}],"venues":["binance"]},
                 "MSFTB":{"pairs":[{"pair":"MSFTBUSDT"}],"venues":["binance"]}}}

def fact(address="0x123",pair="ABCUSDT",verified_at=AT):
    return {"contract_verified":True,"identity":{
        "exchange_pair":pair,"platform":"ethereum",
        "contract_address":address,
        "official_contract_source":"https://project.example/contract",
        "verified_at_utc":verified_at}}

class IdentityAuditTests(unittest.TestCase):
    def test_contract_cache_written_as_valid_json_and_read_back(self):
        cache={"compound-governance-token":{"coin_id":"compound-governance-token",
                                              "platforms":{"ethereum":"0x123"}}}
        with tempfile.TemporaryDirectory() as folder:
            path=pathlib.Path(folder)/"cache.json"
            audit.save_contract_cache(cache,path)
            self.assertEqual(json.loads(path.read_text()),cache)
            self.assertTrue(path.read_text().endswith("\\n")==False)
            self.assertTrue(path.read_text().endswith("\n"))

    def test_pump_not_false_leveraged_and_tokenized_equity_review(self):
        self.assertEqual(audit.classify("PUMP"),"SPOT_TOKEN_UNVERIFIED")
        self.assertEqual(audit.classify("BTCUP"),"LEVERAGED_TOKEN_REVIEW")
        self.assertEqual(audit.classify("MSFTB"),"TOKENIZED_EQUITY_REVIEW")
        self.assertEqual(audit.classify("ARB"),"SPOT_TOKEN_UNVERIFIED")

    def test_symbol_only_never_becomes_verified(self):
        r=audit.build(scan(),{"coingecko":[{"symbol":"abc","id":"wrong"}]},
                      {"assets":{}},NOW)
        self.assertEqual(r["assets"]["ABC"]["identity_status"],"UNVERIFIED")
        self.assertFalse(r["assets"]["ABC"]["capital_identity_pass"])
        self.assertEqual(r["universe_count"],3)

    def test_exact_independent_contract_match(self):
        market={"coingecko":[{"symbol":"abc","id":"abc","platforms":{"ethereum":"0x123"},"contract_as_of_utc":AT}]}
        r=audit.build(scan(),market,{"assets":{"ABC":fact()}},NOW)
        self.assertEqual(r["assets"]["ABC"]["identity_status"],"THIRD_PARTY_CORROBORATED")
        self.assertTrue(r["assets"]["ABC"]["capital_identity_pass"])

    def test_mismatched_contract_blocks(self):
        market={"coingecko":[{"symbol":"abc","id":"abc","platforms":{"ethereum":"0x999"},"contract_as_of_utc":AT}]}
        r=audit.build(scan(),market,{"assets":{"ABC":fact()}},NOW)
        self.assertEqual(r["assets"]["ABC"]["identity_status"],"BLOCKED")
        self.assertIn("THIRD_PARTY_CONTRACT_MISMATCH",r["assets"]["ABC"]["blockers"])

    def test_stale_attestation_blocks(self):
        r=audit.build(scan(),{},{"assets":{"ABC":fact(verified_at="2026-09-01T00:00:00+00:00")}},NOW)
        self.assertEqual(r["assets"]["ABC"]["identity_status"],"STALE")

    def test_ticker_collision_never_cross_matches(self):
        market={"coingecko":[{"symbol":"abc","id":"abc","platforms":{"ethereum":"0x123"}},
                              {"symbol":"abc","id":"impostor","platforms":{"ethereum":"0x123"}}]}
        r=audit.build(scan(),market,{"assets":{"ABC":fact()}},NOW)
        self.assertFalse(r["assets"]["ABC"]["capital_identity_pass"])
        self.assertIn("ABC",r["ticker_collisions"])

    def test_contract_fetch_only_for_attested_assets_and_timestamped(self):
        market={"coingecko":[{"symbol":"abc","id":"abc-project"}]}
        calls=[]
        def fake(coin_id):
            calls.append(coin_id)
            return {"coin_id":coin_id,"symbol":"ABC","platforms":{"ethereum":"0x123"},
                    "source_url":"https://www.coingecko.com/en/coins/"+coin_id}
        enriched,cache,meta=audit.enrich_contracts(
            market,{"assets":{"ABC":fact()}},{},NOW,fetch=fake)
        self.assertEqual(calls,["abc-project"])
        r=audit.build(scan(),enriched,{"assets":{"ABC":fact()}},NOW)
        self.assertTrue(r["assets"]["ABC"]["capital_identity_pass"])
        enriched2,cache,meta=audit.enrich_contracts(
            market,{"assets":{"ABC":fact()}},cache,NOW,fetch=fake)
        self.assertEqual(calls,["abc-project"])
        self.assertEqual(meta["fetched"],0)

    def test_contract_fetch_failure_never_reuses_stale_cache(self):
        market={"coingecko":[{"symbol":"abc","id":"abc-project"}]}
        cache={"abc-project":{"as_of_utc":"2026-09-01T00:00:00+00:00",
                             "platforms":{"ethereum":"0x123"}}}
        def broken(coin_id):
            raise RuntimeError("rate limited")
        enriched,cache,meta=audit.enrich_contracts(
            market,{"assets":{"ABC":fact()}},cache,NOW,fetch=broken)
        self.assertIn("ABC",meta["failures"])
        r=audit.build(scan(),enriched,{"assets":{"ABC":fact()}},NOW)
        self.assertFalse(r["assets"]["ABC"]["capital_identity_pass"])

    def test_small_cap_not_in_market_top500_can_be_corrobated(self):
        declared=fact()
        declared["identity"]["coingecko_id"]="abc-project"
        def fake(coin_id):
            return {"coin_id":coin_id,"symbol":"ABC",
                    "asset_platform_id":"ethereum",
                    "platforms":{"ethereum":"0x123"},
                    "source_url":"https://www.coingecko.com/en/coins/"+coin_id}
        market,cache,meta=audit.enrich_contracts(
            {"coingecko":[]},{"assets":{"ABC":declared}},{},NOW,fetch=fake)
        r=audit.build(scan(),market,{"assets":{"ABC":declared}},NOW)
        self.assertTrue(r["assets"]["ABC"]["capital_identity_pass"])

    def test_native_coin_needs_independent_current_coin_id(self):
        declared={"contract_verified":True,"identity":{
            "exchange_pair":"ABCUSDT","native_asset":True,
            "native_chain":"abc-chain","coingecko_id":"abc-project",
            "official_contract_source":"https://project.example/native",
            "verified_at_utc":AT}}
        def fake(coin_id):
            return {"coin_id":coin_id,"symbol":"ABC",
                    "asset_platform_id":None,"platforms":{},
                    "source_url":"https://www.coingecko.com/en/coins/"+coin_id}
        market,_,_=audit.enrich_contracts(
            {"coingecko":[]},{"assets":{"ABC":declared}},{},NOW,fetch=fake)
        r=audit.build(scan(),market,{"assets":{"ABC":declared}},NOW)
        self.assertEqual(r["assets"]["ABC"]["identity_status"],
                         "THIRD_PARTY_NATIVE_CORROBORATED")
        self.assertTrue(r["assets"]["ABC"]["capital_identity_pass"])

    def test_identity_429_stops_other_requests(self):
        facts={"assets":{"ABC":fact(),"PUMP":fact(pair="PUMPUSDT")}}
        market={"coingecko":[{"symbol":"abc","id":"abc-project"},
                              {"symbol":"pump","id":"pump-project"}]}
        calls=[]
        def blocked(cid):
            calls.append(cid)
            raise urllib.error.HTTPError("https://api.coingecko.com",429,
                                         "Too Many Requests",{"Retry-After":"1800"},None)
        _,_,meta=audit.enrich_contracts(market,facts,{},NOW,fetch=blocked)
        self.assertEqual(len(calls),1)
        self.assertIn("_source_rate_limit",meta["failures"])
        self.assertEqual(meta["retry_after"],"1800")

    def test_wrong_exchange_pair_blocks(self):
        status,blockers=audit.identity_status(
            "ABC",scan()["coins"]["ABC"],fact(pair="OTHERUSDT"),{},NOW)
        self.assertEqual(status,"BLOCKED")
        self.assertIn("OFFICIAL_EXCHANGE_PAIR_MISMATCH",blockers)

    def test_old_scan_refused(self):
        s=scan();s["as_of_utc"]="2026-09-01T00:00:00+00:00"
        with self.assertRaisesRegex(ValueError,"STALE_SCAN"):
            audit.build(s,{},{"assets":{}},NOW)

if __name__=="__main__":unittest.main()
