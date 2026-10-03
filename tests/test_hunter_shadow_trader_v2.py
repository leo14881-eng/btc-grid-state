import datetime as dt,unittest
from research.hunter_shadow_trader_v2 import thesis_alive,weighted_entry,should_add,add_tranche,net_pnl,REVIEW_HOURS
class ShadowV2Tests(unittest.TestCase):
 def c(self,r1=1,r4=2,ind=2):return {"signal":{"btc_relative_1h_pct":r1,"btc_relative_4h_pct":r4,"independent_signal_count":ind},"execution_scenario":{"buy_slippage_bps":10}}
 def pos(self):return {"tranches":[{"tranche":1,"at":"x","price":100.0,"notional_usdt":1000.0,"buy_slippage_bps":10}],"mfe_pct":0,"mae_pct":0}
 def test_drawdown_alone_not_invalidation(self):self.assertTrue(thesis_alive(self.c()))
 def test_signal_collapse_invalidates(self):self.assertFalse(thesis_alive(self.c(-3,-4,1)))
 def test_add_at_lower_price(self):self.assertTrue(should_add(self.pos(),95.9,self.c()))
 def test_no_add_when_thesis_dead(self):self.assertFalse(should_add(self.pos(),90,self.c(-3,-4,1)))
 def test_average_falls_after_add(self):
  p=self.pos();add_tranche(p,92,self.c(),dt.datetime.now(dt.timezone.utc));self.assertLess(weighted_entry(p),100)
 def test_costs_counted(self):self.assertLess(net_pnl(self.pos(),108),80)
 def test_time_is_review_not_exit(self):
  self.assertEqual(REVIEW_HOURS,(24.0,48.0,72.0)); self.assertTrue(thesis_alive(self.c()))
if __name__=="__main__":unittest.main()
