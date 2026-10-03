import datetime as dt,unittest
from research.hunter_shadow_trader import net_return,choose,profit_exit,weighted_entry,position_net_return
class ShadowTests(unittest.TestCase):
 def test_costs_reduce_return(self):
  self.assertLess(net_return(100,110,20,10),.10)
 def test_broad_learning_lane_buys_observed_candidates(self):
  scan={"coins":{"A":{"reference_price":1,"change_24h_pct":5},"B":{"reference_price":1,"change_24h_pct":25}}}
  def c(a,s):return {"asset":a,"signal":{"score":s,"btc_relative_1h_pct":-5,"btc_relative_4h_pct":-5,"independent_signal_count":0},"execution_scenario":{"buy_slippage_bps":10},"blockers":["RESEARCH_INCOMPLETE"]}
  out=choose({"candidates":[c("A",1),c("B",20)]},scan,set())
  self.assertEqual({x[1] for x in out},{"A","B"})
 def test_weighted_entry_multiple_tranches(self):
  p={"entry_reference_price":1,"notional_usdt":1000,"tranches":[{"price":1,"notional_usdt":1000},{"price":.8,"notional_usdt":1000}]}
  self.assertAlmostEqual(weighted_entry(p),.9)
 def test_profit_giveback_exits_only_while_still_net_profitable(self):
  p={"entry_reference_price":1,"notional_usdt":1000,"buy_slippage_bps":0,"mfe_net_pct":8,
     "tranches":[{"price":1,"notional_usdt":1000,"buy_slippage_bps":0}]}
  ex,nr,mfe,gb=profit_exit(p,1.05)
  self.assertTrue(ex);self.assertGreater(nr,0);self.assertGreaterEqual(gb,2)
if __name__=="__main__":unittest.main()
