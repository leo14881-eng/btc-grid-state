import datetime as dt,unittest
from research import hunter_tail_risk as risk
from research import hunter_tail_risk_replay as replay
from research.hunter_policy import C

NOW=dt.datetime(2026,10,5,12,0,0,tzinfo=dt.timezone.utc)

def evidence(oid="e1",btc1=0.0,btc15=0.0,btc5=0.0,neg=.2,loss5=.05,loss10=0.0,cat=.0,stable=.05,missing=None):
    return {"observation_id":oid,"observed_at_utc":NOW.isoformat(),"missing_or_stale":list(missing or []),
            "btc_short":{"return_5m_pct":btc5,"return_15m_pct":btc15,"return_1h_pct":btc1},
            "breadth":{"negative_fraction":neg,"loss_5pct_fraction":loss5,"loss_10pct_fraction":loss10},
            "liquidity":{"catastrophic_fraction":cat},"stablecoins":{"max_deviation_pct":stable}}

class SystemicRiskTests(unittest.TestCase):
    def test_same_cycle_small_future_clock_skew_is_fresh_but_large_future_is_not(self):
        self.assertTrue(risk.is_fresh((NOW+dt.timedelta(seconds=2)).isoformat(),NOW,900))
        self.assertFalse(risk.is_fresh((NOW+dt.timedelta(minutes=2)).isoformat(),NOW,900))
    def test_missing_evidence_fails_closed_high(self):
        level,reasons=risk.classify_systemic_risk(evidence(missing=["BTC_SHORT_TERM_MISSING"]),C)
        self.assertEqual(level,"HIGH");self.assertTrue(any("FAIL_CLOSED" in x for x in reasons))

    def test_multi_factor_crash_is_critical(self):
        level,_=risk.classify_systemic_risk(evidence(btc1=-11,btc15=-7,neg=.95,loss5=.8,loss10=.6,cat=.7,stable=2.5),C)
        self.assertEqual(level,"CRITICAL")

    def test_recovery_requires_independent_normal_observations(self):
        state={}
        high=evidence("shock",btc1=-7,neg=.9,loss5=.6)
        row,_=risk.update_systemic_risk(state,high,NOW,C);self.assertEqual(row["level"],"HIGH")
        for i in range(1,int(C["SYSTEMIC_RECOVERY_OBSERVATIONS"])):
            row,_=risk.update_systemic_risk(state,evidence("n"+str(i)),NOW+dt.timedelta(minutes=5*i),C)
            self.assertEqual(row["level"],"ELEVATED");self.assertTrue(row["recovery_mode"])
        row,_=risk.update_systemic_risk(state,evidence("n-final"),NOW+dt.timedelta(minutes=25),C)
        self.assertEqual(row["level"],"NORMAL");self.assertFalse(row["recovery_mode"])

    def test_distinct_observation_too_soon_cannot_advance_recovery(self):
        state={}
        risk.update_systemic_risk(state,evidence("shock",btc1=-7,neg=.9,loss5=.6),NOW,C)
        row,_=risk.update_systemic_risk(state,evidence("normal1"),NOW+dt.timedelta(minutes=5),C)
        self.assertEqual(row["recovery_observations"],1)
        row,_=risk.update_systemic_risk(state,evidence("normal2"),NOW+dt.timedelta(minutes=6),C)
        self.assertEqual(row["recovery_observations"],1);self.assertIn("RECOVERY_OBSERVATION_TOO_SOON",row["reasons"])

    def test_duplicate_observation_cannot_advance_recovery_after_restart(self):
        state={}
        risk.update_systemic_risk(state,evidence("shock",btc1=-7,neg=.9,loss5=.6),NOW,C)
        row,_=risk.update_systemic_risk(state,evidence("same"),NOW+dt.timedelta(minutes=5),C)
        n=row["recovery_observations"]
        persisted={"systemic_risk":dict(state["systemic_risk"])}
        row,changed=risk.update_systemic_risk(persisted,evidence("same"),NOW+dt.timedelta(minutes=10),C)
        self.assertFalse(changed);self.assertEqual(row["recovery_observations"],n)

class TailBudgetTests(unittest.TestCase):
    def test_calibration_candidates_are_reported_without_formal_selection(self):
        state={"systemic_risk":{"level":"NORMAL","risk_release_fraction":1.0,"recovery_mode":False}}
        x=risk.tail_budget_snapshot(state,C,20000.0)
        self.assertEqual(x["budget_status"],"CALIBRATION_CEILING_NOT_FINAL")
        self.assertEqual(set(x["candidate_budget_caps_usdt"]),{"1000","2000","3000","4000"})
        self.assertEqual(x["effective_tail_cap_usdt"],4000.0)

    def test_quarantine_reduces_effective_tail_capacity(self):
        state={"systemic_risk":{"level":"NORMAL","risk_release_fraction":1.0,"recovery_mode":False},
               "circuit_breaker":{"quarantined_cash_usdt":1000.0}}
        self.assertEqual(risk.tail_budget_snapshot(state,C,20000.0)["effective_tail_cap_usdt"],3000.0)

    def test_realized_profit_is_not_an_input_to_tail_budget(self):
        a=risk.tail_budget_snapshot({"systemic_risk":{"level":"NORMAL","risk_release_fraction":1.0,"recovery_mode":False}},C,20000.0)
        b=risk.tail_budget_snapshot({"systemic_risk":{"level":"NORMAL","risk_release_fraction":1.0,"recovery_mode":False},
                                     "closed_positions":[{"net_pnl_usdt":9999}]},C,20000.0)
        self.assertEqual(a["effective_tail_cap_usdt"],b["effective_tail_cap_usdt"])

class ReplayAssumptionTests(unittest.TestCase):
    def test_adverse_fill_stress_subtracts_slippage_without_fake_near_zero_fill(self):
        self.assertAlmostEqual(replay.stressed_fill_move(-47.2481,10.0),-57.2481,places=4)
        self.assertEqual(replay.stressed_fill_move(-95.0,10.0),-99.9)

class CircuitBreakerTests(unittest.TestCase):
    def test_loss_quarantines_released_notional_and_two_losses_trip(self):
        state={"systemic_risk":{"last_observation_id":"shock"}}
        risk.record_loss_exit(state,-100,"HARD_INVALIDATION",1000,NOW,C)
        self.assertEqual(state["circuit_breaker"]["status"],"WATCH")
        self.assertEqual(state["circuit_breaker"]["quarantined_cash_usdt"],1000)
        risk.record_loss_exit(state,-100,"HARD_INVALIDATION",1000,NOW+dt.timedelta(minutes=1),C)
        self.assertEqual(state["circuit_breaker"]["status"],"TRIPPED")
        self.assertEqual(state["circuit_breaker"]["quarantined_cash_usdt"],2000)

    def test_recovery_releases_quarantine_gradually(self):
        state={"systemic_risk":{}}
        risk.record_loss_exit(state,-600,"HARD_INVALIDATION",2000,NOW,C)
        before=state["circuit_breaker"]["quarantined_cash_usdt"]
        for i in range(1,int(C["CIRCUIT_RECOVERY_OBSERVATIONS_BEFORE_RELEASE"])+1):
            ev=evidence("r"+str(i));systemic,_=risk.update_systemic_risk(state,ev,NOW+dt.timedelta(minutes=5*i),C)
            risk.advance_circuit_breaker(state,systemic,ev,NOW+dt.timedelta(minutes=5*i),C,True)
        after=state["circuit_breaker"]["quarantined_cash_usdt"]
        self.assertGreater(after,0);self.assertLess(after,before)


class UnifiedRiskAdmissionRegressionTests(unittest.TestCase):
    def _normal(self):
        return {"systemic_risk":{"level":"NORMAL","raw_level":"NORMAL","last_observation_id":"ok","recovery_mode":False,"risk_release_fraction":1.0}}

    def test_tripped_circuit_blocks_new_risk_even_when_systemic_normal(self):
        state=self._normal();state["circuit_breaker"]={"status":"TRIPPED","quarantined_cash_usdt":1000.0}
        self.assertTrue(risk.risk_blocks_new(state))

    def test_recovering_circuit_still_blocks_new_risk(self):
        state=self._normal();state["circuit_breaker"]={"status":"RECOVERING","quarantined_cash_usdt":500.0}
        self.assertTrue(risk.risk_blocks_new(state))

    def test_normal_circuit_and_systemic_allow_new_risk(self):
        state=self._normal();state["circuit_breaker"]={"status":"NORMAL","quarantined_cash_usdt":0.0}
        self.assertFalse(risk.risk_blocks_new(state))

    def test_profitable_exit_resets_consecutive_losses_without_releasing_quarantine(self):
        state=self._normal()
        risk.record_loss_exit(state,-100,"HARD_INVALIDATION",1000,NOW,C)
        q=state["circuit_breaker"]["quarantined_cash_usdt"]
        self.assertEqual(state["circuit_breaker"]["consecutive_loss_exits"],1)
        risk.record_loss_exit(state,25,"PROFIT_PROTECTION",1000,NOW+dt.timedelta(minutes=1),C)
        self.assertEqual(state["circuit_breaker"]["consecutive_loss_exits"],0)
        self.assertEqual(state["circuit_breaker"]["quarantined_cash_usdt"],q)

    def test_circuit_recovery_requires_temporal_independence(self):
        state=self._normal()
        risk.record_loss_exit(state,-100,"HARD_INVALIDATION",1000,NOW,C)
        risk.record_loss_exit(state,-100,"HARD_INVALIDATION",1000,NOW+dt.timedelta(minutes=1),C)
        self.assertEqual(state["circuit_breaker"]["status"],"TRIPPED")
        ev1=evidence("recover-1")
        systemic={"raw_level":"NORMAL"}
        risk.advance_circuit_breaker(state,systemic,ev1,NOW+dt.timedelta(minutes=5),C,True)
        n=state["circuit_breaker"]["recovery_observations"]
        ev2=evidence("recover-2")
        risk.advance_circuit_breaker(state,systemic,ev2,NOW+dt.timedelta(minutes=5,seconds=1),C,True)
        self.assertEqual(state["circuit_breaker"]["recovery_observations"],n)
        self.assertEqual(state["circuit_breaker"].get("recovery_wait_reason"),"CIRCUIT_RECOVERY_OBSERVATION_TOO_SOON")

if __name__=="__main__":unittest.main()
