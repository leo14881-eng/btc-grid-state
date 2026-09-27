import importlib.util
import pathlib
import unittest

spec=importlib.util.spec_from_file_location("hunter_execution_cost",pathlib.Path("research/hunter_execution_cost.py"))
h=importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)

class ExecutionCostTests(unittest.TestCase):
    def test_costs_and_target(self):
        b={"bids":[["99.9","200"]],"asks":[["100.1","200"]]}
        x=h.estimate(b,4000,fee_bps=10,target_net_usdt=150)
        self.assertGreater(x["roundtrip_cost_usdt"],0)
        self.assertGreater(x["required_mid_gain_pct"],0)
        self.assertGreater(x["estimated_stop_loss_usdt"],0)
        self.assertEqual(x["target_net_usdt"],150)
    def test_insufficient_depth_blocks(self):
        b={"bids":[["99","1"]],"asks":[["101","1"]]}
        with self.assertRaisesRegex(ValueError,"INSUFFICIENT_ASK_DEPTH"):
            h.estimate(b,4000)
    def test_invalid_book_blocks(self):
        with self.assertRaises(ValueError):
            h.estimate({"bids":[["102","100"]],"asks":[["101","100"]]},2000)

if __name__=="__main__":unittest.main()
