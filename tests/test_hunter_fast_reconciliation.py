import copy
import datetime as dt
import unittest
from unittest.mock import patch
from research.hunter_fast_reconciliation import reconcile_view, observe

class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.now=dt.datetime.now(dt.timezone.utc)
        self.p={'mode':'SIMULATION_ONLY_NO_REAL_ORDERS','active_observation_generation_id':'MONITOR_CURRENT','open_positions':[]}
        self.health={'source_sha':'a'*40,'venues':{'BINANCE_SPOT':{'actual_subscriptions':['BTCUSDT']},'BYBIT_SPOT':{'actual_subscriptions':[]}}}
    def test_matched_read_only(self):
        original=copy.deepcopy(self.p)
        self.assertEqual(reconcile_view(self.p,{},'b'*40,self.now,self.health)['status'],'MATCHED')
        self.assertEqual(self.p,original)
    def test_venue_specific_subscription_drift(self):
        self.p['open_positions']=[{'shadow_id':'spot','asset':'ENA','market_symbol':'ENAUSDT','market_type':'spot','execution_venue':'BYBIT_SPOT','capital_authority':'NONE_SHADOW_ONLY','tranches':[{'price':1,'notional_usdt':1000}]}]
        self.health['venues']['BINANCE_SPOT']['actual_subscriptions'].append('ENAUSDT')
        row=reconcile_view(self.p,{},'b'*40,self.now,self.health)
        self.assertEqual(row['status'],'RECONCILE_REQUIRED')
        self.assertEqual(row['venues']['BYBIT_SPOT']['missing_symbols'],['ENAUSDT'])
        self.assertEqual(row['venues']['BINANCE_SPOT']['extra_symbols'],['ENAUSDT'])
    def test_unknown_identity_never_claims_match(self):
        self.p['open_positions']=[{'shadow_id':'unknown','asset':'ENA'}]
        self.assertEqual(reconcile_view(self.p,{},'b'*40,self.now,self.health)['status'],'PRIMARY_VENUE_IDENTITY_MISSING')
    def test_stale_health_does_not_disable_monitor(self):
        with patch('research.hunter_fast_reconciliation.read_health',side_effect=ValueError('STALE')):
            row=observe(self.p,{},'b'*40,self.now)
        self.assertFalse(row['authoritative_monitor_affected'])
        self.assertEqual(row['status'],'FAST_WATCH_HEALTH_UNAVAILABLE')
