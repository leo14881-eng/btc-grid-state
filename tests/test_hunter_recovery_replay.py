import copy
import datetime as dt
import json
import unittest
from research import hunter_tail_risk as risk
from research.hunter_policy import C
from tests.test_hunter_tail_risk import NOW, evidence

class RecoveryReplayTests(unittest.TestCase):
    def ev(self, oid, at):
        ev=evidence(oid);ev['observed_at_utc']=at.isoformat();return ev

    def tripped(self):
        s={}
        risk.record_loss_exit(s,-100,'HARD_INVALIDATION',1000,NOW,C)
        risk.record_loss_exit(s,-100,'HARD_INVALIDATION',1000,NOW,C)
        return s

    def update(self,s,oid,seconds,source_seconds=None):
        now=NOW+dt.timedelta(seconds=seconds)
        observed=NOW+dt.timedelta(seconds=seconds if source_seconds is None else source_seconds)
        risk.update_risk_controls(s,self.ev(oid,observed),now,C)

    def test_alternating_ids_survive_restart_without_releasing_again(self):
        s=self.tripped()
        balances=[]
        for i,oid in enumerate(['A','B','A','B','A'],1):
            self.update(s,oid,i*300)
            s=json.loads(json.dumps(s))
            balances.append(s['circuit_breaker']['quarantined_cash_usdt'])
        self.assertEqual(balances,[2000,1500,1500,1500,1500])
        self.assertEqual(s['circuit_breaker']['status'],'RECOVERING')

    def test_systemic_alternating_ids_do_not_complete_recovery(self):
        s={};shock=self.ev('shock',NOW);shock['missing_or_stale']=['missing']
        risk.update_risk_controls(s,shock,NOW,C)
        for i,oid in enumerate(['A','B','A','B','A'],1):
            self.update(s,oid,i*300);s=json.loads(json.dumps(s))
        self.assertEqual(s['systemic_risk']['recovery_observations'],2)
        self.assertTrue(s['systemic_risk']['recovery_mode'])

    def test_loss_timestamp_bounds_first_and_second_observation(self):
        s=self.tripped()
        self.update(s,'old-A',-600)
        self.update(s,'old-B',-300)
        self.assertEqual(s['circuit_breaker']['recovery_observations'],0)
        self.assertEqual(s['circuit_breaker']['quarantined_cash_usdt'],2000)

    def test_old_source_time_cannot_be_replayed_at_new_processing_time(self):
        s=self.tripped()
        self.update(s,'A',300)
        self.update(s,'B',600)
        before=copy.deepcopy(s['circuit_breaker'])
        self.update(s,'historical-new-id',900,450)
        self.assertEqual(s['circuit_breaker'],before)

    def test_processing_time_rollback_does_not_advance_or_release(self):
        s=self.tripped();self.update(s,'A',600)
        before=copy.deepcopy(s['circuit_breaker'])
        self.update(s,'backwards',300)
        self.assertEqual(s['circuit_breaker'],before)

    def test_source_gap_must_also_reach_existing_300_seconds(self):
        s=self.tripped();self.update(s,'A',300)
        self.update(s,'B',600,301)
        self.assertEqual(s['circuit_breaker']['recovery_observations'],1)
        self.assertEqual(s['circuit_breaker']['quarantined_cash_usdt'],2000)

    def test_unique_in_order_observations_still_release_at_existing_rate(self):
        s=self.tripped();balances=[]
        for i in range(1,6):
            self.update(s,str(i),i*300)
            balances.append(s['circuit_breaker']['quarantined_cash_usdt'])
        self.assertEqual(balances,[2000,1500,1000,500,0])
        self.assertEqual(s['circuit_breaker']['status'],'NORMAL')

    def test_completed_episode_ids_cannot_release_in_later_episode(self):
        s=self.tripped()
        for i in range(1,6):self.update(s,str(i),i*300)
        loss_at=NOW+dt.timedelta(seconds=1800)
        risk.record_loss_exit(s,-100,'HARD_INVALIDATION',1000,loss_at,C)
        risk.record_loss_exit(s,-100,'HARD_INVALIDATION',1000,loss_at,C)
        for i in range(1,6):self.update(s,str(i),1800+i*300)
        self.assertEqual(s['circuit_breaker']['quarantined_cash_usdt'],2000)
        self.assertEqual(s['circuit_breaker']['recovery_observations'],0)

    def test_legacy_loss_events_bound_first_observation_after_restart(self):
        s=self.tripped();s['circuit_breaker'].pop('last_loss_exit_at_utc',None)
        s=json.loads(json.dumps(s))
        self.update(s,'old-A',300,-300)
        self.update(s,'old-B',600,-1)
        self.assertEqual(s['circuit_breaker']['recovery_observations'],0)
        self.assertEqual(s['circuit_breaker']['quarantined_cash_usdt'],2000)

    def test_direct_circuit_api_rejects_alternating_ids(self):
        s=self.tripped()
        for i,oid in enumerate(['A','B','A'],1):
            now=NOW+dt.timedelta(seconds=i*300)
            risk.advance_circuit_breaker(s,{'raw_level':'NORMAL'},self.ev(oid,now),now,C,True)
        self.assertEqual(s['circuit_breaker']['quarantined_cash_usdt'],1500)
        self.assertEqual(s['circuit_breaker']['recovery_observations'],2)

    def test_legacy_persisted_timestamp_rejects_clock_rollback(self):
        s=self.tripped()
        s['circuit_breaker']['updated_at_utc']=(NOW+dt.timedelta(seconds=1200)).isoformat()
        s['circuit_breaker']['recovery_observations']=1
        before=copy.deepcopy(s['circuit_breaker'])
        self.update(s,'older-than-persisted',600)
        self.assertEqual(s['circuit_breaker'],before)

    def test_systemic_legacy_counter_without_identity_history_restarts(self):
        s={'systemic_risk':dict(level='ELEVATED',raw_level='NORMAL',recovery_mode=True,
            recovery_observations=2,last_observation_id='legacy',risk_release_fraction=.5,
            last_recovery_counted_at_utc=NOW.isoformat(),last_observed_at_utc=NOW.isoformat())}
        self.update(s,'new',300)
        self.assertEqual(s['systemic_risk']['recovery_observations'],1)
        self.assertTrue(s['systemic_risk']['recovery_mode'])
