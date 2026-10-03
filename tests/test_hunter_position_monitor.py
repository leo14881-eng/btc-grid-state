import unittest
from research import hunter_position_monitor as m
class PositionMonitorTests(unittest.TestCase):
 def test_assets_deduplicates_v1_v2(self):
  s=[{"open_positions":[{"asset":"A"},{"asset":"B"}]},{"open_positions":[{"asset":"A"}]}]
  self.assertEqual(m.assets(s),["A","B"])
 def test_relative_uses_btc_benchmark(self):
  x={"A":{"r5":3,"r15":4,"r60":5},"BTC":{"r5":1,"r15":1.5,"r60":2}}
  self.assertEqual(m.relative("A",x),{"btc_rel_5m":2,"btc_rel_15m":2.5,"btc_rel_60m":3})
if __name__=="__main__":unittest.main()
