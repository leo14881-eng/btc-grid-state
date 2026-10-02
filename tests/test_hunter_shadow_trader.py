import datetime as dt,unittest
from research.hunter_shadow_trader import net_return,choose
class ShadowTests(unittest.TestCase):
 def test_costs_reduce_return(self):
  self.assertLess(net_return(100,110,20,10),.10)
 def test_anti_chase_and_rank(self):
  scan={"coins":{"A":{"reference_price":1,"change_24h_pct":5},"B":{"reference_price":1,"change_24h_pct":25}}}
  def c(a,s):return {"asset":a,"signal":{"score":s,"btc_relative_1h_pct":2,"btc_relative_4h_pct":2,"independent_signal_count":2},"execution_scenario":{"buy_slippage_bps":10}}
  out=choose({"candidates":[c("A",10),c("B",20)]},scan,set())
  self.assertEqual([x[1] for x in out],["A"])
if __name__=="__main__":unittest.main()
