import unittest
from research.hunter_shadow_optimizer import metrics
class OptimizerTests(unittest.TestCase):
 def test_metrics(self):
  xs=[{"net_pnl_usdt":100,"net_return_pct":10,"btc_relative_return_pct":5,"mae_pct":-2},{"net_pnl_usdt":-50,"net_return_pct":-5,"btc_relative_return_pct":-1,"mae_pct":-6}]
  m=metrics(xs);self.assertEqual(m["n"],2);self.assertEqual(m["net_pnl_usdt"],50);self.assertEqual(m["profit_factor"],2.0);self.assertEqual(m["worst_mae_pct"],-6)
if __name__=="__main__":unittest.main()
