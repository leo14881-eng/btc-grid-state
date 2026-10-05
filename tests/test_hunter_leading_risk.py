import datetime as dt,unittest
from research import hunter_leading_risk as r
from research.hunter_policy import C
NOW=dt.datetime(2026,10,5,12,tzinfo=dt.timezone.utc)
def ev(oid="1",neg=.2,acc=0,rel=.2,racc=0,spread=5,sacc=0,depth=0,imb=0,btc5=0,btc15=0,btc1=0,oi=0,fund=0,basis=0,stable=.01,missing=None):
 return {"observation_id":oid,"breadth":{"negative_fraction":neg,"negative_acceleration":acc,"loss_5pct_fraction":0},
 "relative":{"negative_1h_fraction":rel,"negative_1h_acceleration":racc},"liquidity":{"median_spread_bps":spread,"spread_acceleration_bps":sacc,"bid_depth_change_pct":depth,"median_book_imbalance":imb},
 "btc_structure":{"return_5m_pct":btc5,"return_15m_pct":btc15,"return_1h_pct":btc1},"leverage":{"open_interest_change_pct":oi,"funding_rate":fund,"basis_pct":basis},
 "stablecoins":{"max_deviation_pct":stable},"missing_or_stale":list(missing or [])}
class LeadingRiskTests(unittest.TestCase):
 def test_normal_is_normal(self):
  raw,score,groups,_=r.classify(ev(),C);self.assertEqual(raw,"NORMAL");self.assertEqual(score,0);self.assertFalse(any(groups.values()))
 def test_multiple_independent_groups_make_precrash(self):
  raw,score,groups,_=r.classify(ev(neg=.9,rel=.9,depth=-50,btc15=-2),C)
  self.assertIn(raw,("PRE_CRASH_2","PRE_CRASH_3"));self.assertGreaterEqual(sum(groups.values()),3);self.assertGreaterEqual(score,60)
 def test_single_group_cannot_jump_to_precrash(self):
  raw,_,groups,_=r.classify(ev(neg=.95),C);self.assertEqual(sum(groups.values()),1);self.assertEqual(raw,"WATCH")
 def test_upgrade_requires_independent_observations(self):
  state={};bad=ev("a",neg=.9,rel=.9,depth=-50,btc15=-2)
  row,_=r.update_state(state,bad,NOW,C);self.assertEqual(row["level"],"NORMAL");self.assertTrue(row["candidate_level"].startswith("PRE_CRASH"))
  row,_=r.update_state(row,ev("b",neg=.9,rel=.9,depth=-50,btc15=-2),NOW+dt.timedelta(minutes=5),C);self.assertTrue(row["level"].startswith("PRE_CRASH"))
 def test_duplicate_cannot_confirm_after_restart(self):
  row,_=r.update_state({},ev("a",neg=.9,rel=.9,depth=-50),NOW,C)
  same,changed=r.update_state(dict(row),ev("a",neg=.9,rel=.9,depth=-50),NOW+dt.timedelta(minutes=5),C)
  self.assertFalse(changed);self.assertEqual(same["level"],row["level"])
 def test_missing_derivatives_is_uncertainty_not_sell_signal(self):
  raw,_,_,_=r.classify(ev(missing=["DERIVATIVES_UNAVAILABLE"]),C);self.assertEqual(raw,"NORMAL")
  row,_=r.update_state({},ev("a",missing=["DERIVATIVES_UNAVAILABLE"]),NOW,C);self.assertTrue(row["data_uncertain"])
 def test_recovery_requires_multiple_observations(self):
  bad=ev("a",neg=.9,rel=.9,depth=-50,btc15=-2);row,_=r.update_state({},bad,NOW,C)
  row,_=r.update_state(row,ev("b",neg=.9,rel=.9,depth=-50,btc15=-2),NOW+dt.timedelta(minutes=5),C)
  old=row["level"]
  row,_=r.update_state(row,ev("n1"),NOW+dt.timedelta(minutes=10),C);self.assertEqual(row["level"],old)
  for i in range(2,int(C["LEADING_RECOVERY_OBSERVATIONS"])+1):
   row,_=r.update_state(row,ev("n"+str(i)),NOW+dt.timedelta(minutes=5*(i+1)),C)
  self.assertEqual(row["level"],"NORMAL")
 def test_counterfactual_never_mutates_and_is_bounded(self):
  x=r.shadow_derisk("PRE_CRASH_3",19000,4000);self.assertEqual(x["B_BALANCED"]["target_exposure_usdt"],4750)
  self.assertGreater(x["B_BALANCED"]["counterfactual_reduction_usdt"],0)
if __name__=="__main__":unittest.main()
