import copy
import datetime as dt
import json
import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch
from research import hunter_shadow_trader as v1
from research import hunter_shadow_trader_v2 as engine

class V1ExclusionAuditTests(unittest.TestCase):
 def fixtures(self,root):
  rows=[{'base':'GAIB','source_venue':'bybit','execution_supported':False,'source_observed_at_utc':'2026-10-06T00:00:00Z'},
        {'base':'HELD','source_venue':'bybit','execution_supported':False},
        {'base':'NEW','independent_signal_count':2,'score':0.1}]
  data={'hunter-early-signals.json':{'policy_version':engine.VERSION,'scan_generation_id':'G','as_of_utc':'2026-10-06T00:00:00Z','early':rows,'all_signals':rows},
        'hunter-cex-universe-run.json':{'generation_id':'G','venue_status':{'binance':{'excluded_bstocks':['STOCK']}}},
        'hunter-tactical-capital-review.json':{'scan_generation_id':'G','candidates':[]},
        'hunter-shadow-portfolio.json':{'schema':'hunter_shadow_v2_portfolio_v2',
            'mode':'SIMULATION_ONLY_NO_REAL_ORDERS','open_positions':[{'asset':'HELD'}],
            'closed_positions':[],'events':[],'decisions':[]}}
  for name,d in data.items():(root/name).write_text(json.dumps(d))
 def test_exclusion_does_not_admit_candidate_or_drop_existing_management(self):
  with tempfile.TemporaryDirectory() as d:
   root=pathlib.Path(d)/'research/results';root.mkdir(parents=True);self.fixtures(root)
   old=os.getcwd()
   try:
    os.chdir(d);_,review=v1.load_early_into_review()
   finally:os.chdir(old)
  self.assertEqual({c['asset'] for c in review['candidates']},{'HELD','NEW'})
  audit=review['v1_early_exclusions'];self.assertEqual(audit['scan_generation_id'],'G')
  self.assertEqual([x['asset'] for x in audit['rows']],['GAIB'])
  self.assertEqual(audit['rows'][0]['source_venue'],'bybit')
  state={'decisions':[],'events':[],'open_positions':[]}
  scan={'generation_id':'G','coins':{'GAIB':{'reference_price':1}}}
  with patch.object(engine,'ENTRY_MODE','DISCOVERY'):
   engine.record_early_sample_exclusions(state,review,scan,dt.datetime.now(dt.timezone.utc))
   engine.record_early_sample_exclusions(state,review,scan,dt.datetime.now(dt.timezone.utc))
  self.assertEqual(len(state['decisions']),1)
  self.assertEqual(state['decisions'][0]['action'],'SAMPLE_EXCLUDED')
  self.assertEqual(state['decisions'][0]['notional_usdt'],0)
  self.assertEqual(state['events'],[]);self.assertEqual(state['open_positions'],[])
 def test_stale_audit_fails_before_recording_and_v2_is_unchanged(self):
  state={'decisions':[]};review={'v1_early_exclusions':{'scan_generation_id':'OLD','rows':[]}}
  original=copy.deepcopy(state)
  with patch.object(engine,'ENTRY_MODE','DISCOVERY'):
   with self.assertRaisesRegex(SystemExit,'V1_EXCLUSION_AUDIT_GENERATION_MISMATCH'):
    engine.record_early_sample_exclusions(state,review,{'generation_id':'NEW'},dt.datetime.now(dt.timezone.utc))
  self.assertEqual(state,original)
  with patch.object(engine,'ENTRY_MODE','EXECUTABLE'):
   engine.record_early_sample_exclusions(state,review,{'generation_id':'NEW'},dt.datetime.now(dt.timezone.utc))
  self.assertEqual(state,original)

if __name__=='__main__':unittest.main()
