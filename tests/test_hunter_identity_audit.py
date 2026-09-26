import datetime as dt
import importlib.util
import pathlib
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
        market={"coingecko":[{"symbol":"abc","id":"abc","platforms":{"ethereum":"0x123"}}]}
        r=audit.build(scan(),market,{"assets":{"ABC":fact()}},NOW)
        self.assertEqual(r["assets"]["ABC"]["identity_status"],"THIRD_PARTY_CORROBORATED")
        self.assertTrue(r["assets"]["ABC"]["capital_identity_pass"])

    def test_mismatched_contract_blocks(self):
        market={"coingecko":[{"symbol":"abc","id":"abc","platforms":{"ethereum":"0x999"}}]}
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
