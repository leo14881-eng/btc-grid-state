"""Synthetic, offline hourly producer-to-allocator integration; no historical fills."""
import contextlib
import copy
import datetime as dt
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_hunter_lifecycle_v2 import NOW, inputs, position
from research import hunter_early_signals as signals
from research import hunter_tactical_capital_review as capital
from research import hunter_shadow_trader_v2 as engine


class HourlyLifecycleSourcesTests(unittest.TestCase):
    def test_actual_main_stdout_fits_journal_without_dropping_persisted_receipts(self):
        scan=dict(binance_complete=True,generation_id='offline-log-size',coins={})
        source=inputs()[1]['candidates'][0]['v2_lifecycle_evidence']
        r1={'BTCUSDT':dict(return_pct=0,v2_source_receipt=source['relative_receipts']['btc_1h'])}
        r4={'BTCUSDT':dict(return_pct=0,v2_source_receipt=source['relative_receipts']['btc_4h'])}
        micro={}
        for index in range(10):
            asset='ASSET'+str(index);symbol=asset+'USDT'
            scan['coins'][asset]=dict(reference_price=100,pairs=[dict(pair=symbol,venue='binance')])
            for window,out in [('1h',r1),('4h',r4)]:
                packet=copy.deepcopy(source['relative_receipts']['asset_'+window]);packet['symbol']=symbol
                out[symbol]=dict(return_pct=8,v2_source_receipt=packet)
            packet=copy.deepcopy(source['micro_receipt']);packet['symbol']=symbol
            micro[symbol]=dict(volume_acceleration=3,compression_ratio=2,return_15m_pct=1,range_position=.7,v2_candle_receipt=packet)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);scanfile=root/'scan.json';out=root/'early.json';history=root/'history.json'
            scanfile.write_text(json.dumps(scan))
            capture=io.StringIO()
            with patch.object(signals,'SCAN',scanfile),patch.object(signals,'OUT',out),patch.object(signals,'HISTORY',history), \
                 patch.object(signals,'rolling',side_effect=[r1,r4]),patch.object(signals,'micro',return_value=micro), \
                 patch('urllib.request.urlopen',side_effect=AssertionError('OFFLINE')),contextlib.redirect_stdout(capture):
                signals.main()
            persisted=json.loads(out.read_text());durable=json.loads(history.read_text())
            log=capture.getvalue();summary=json.loads(log)
            self.assertEqual(summary['early_count'],10);self.assertEqual(len(summary['top']),10)
            self.assertEqual(len(log.splitlines()),1);self.assertLessEqual(len(log.encode()),8192)
            self.assertNotIn('v2_lifecycle_evidence',log)
            for row in persisted['early']:
                receipt=row['v2_lifecycle_evidence'];symbol=row['pair'];asset=row['base']
                self.assertEqual(receipt['micro_receipt'],micro[symbol]['v2_candle_receipt'])
                self.assertEqual(receipt['relative_receipts']['asset_1h'],r1[symbol]['v2_source_receipt'])
                self.assertEqual(durable['assets'][asset]['first_signal']['v2_lifecycle_evidence'],receipt)
            # The exact former stdout expression exceeds the fixed reader byte window.
            old=json.dumps(dict(early_count=10,top=persisted['early'][:10]))+'\n'
            self.assertGreater(len(old.encode()),30000)

    def test_producer_capital_review_management_allocator_preserve_sources_and_add_once(self):
        scan, _, liq, supply = inputs(price=93.6)
        scan['binance_complete'] = True
        scan['coins']['ENA'].update(pairs=[dict(pair='ENAUSDT', venue='binance')],
                                    venues=['binance'], change_24h_pct=0)
        scan['coins']['BTC'] = dict(reference_price=100, change_24h_pct=0)
        source = inputs()[1]['candidates'][0]['v2_lifecycle_evidence']
        r1, r4 = {}, {}
        for window, ret, out in [('1h', 4, r1), ('4h', 7, r4)]:
            for role, symbol in [('asset', 'ENAUSDT'), ('btc', 'BTCUSDT')]:
                packet = copy.deepcopy(source['relative_receipts'][role+'_'+window])
                close = 93.6 if role == 'asset' else 100
                opening = close/(1+ret/100) if role == 'asset' else close
                for row in packet['rows']:
                    row[1], row[2], row[3], row[4] = opening, max(opening, close)+1, min(opening, close)-1, close
                out[symbol] = dict(return_pct=ret if role == 'asset' else 0,
                                   quote_volume=1000, v2_source_receipt=packet)
        micro = copy.deepcopy(source['micro_receipt'])
        for index, row in enumerate(micro['rows']):
            close = 90+index*.15
            row[1] = row[4] = close
            row[2], row[3], row[7], row[10] = close+.2, close-.2, 100, 80
        microdata = {'ENAUSDT': dict(volume_acceleration=1.1, compression_ratio=1,
                                     return_15m_pct=.2, range_position=.9, v2_candle_receipt=micro)}
        early = signals.build(scan, r1, r4, microdata, NOW)
        expected_receipt = early['all_signals'][0]['v2_lifecycle_evidence']
        self.assertEqual(expected_receipt['generation_id'], scan['generation_id'])
        self.assertEqual(expected_receipt['relative_receipts']['asset_1h'], r1['ENAUSDT']['v2_source_receipt'])
        liq['scan_as_of_utc'] = NOW.isoformat()
        liq['snapshots']['ENA']['execution_scenarios'] = {'3000': dict(buy_slippage_bps=10, estimated_rr=4)}
        facts = {'assets': {'ENA': dict(tactical_supply_risk_verified=True, verified_at_utc=NOW.isoformat())}}
        identity = dict(scan_as_of_utc=NOW.isoformat(), assets={'ENA': dict(capital_identity_pass=True)})
        capital_state = dict(capital_pool_usdt=20000, open_cost_usdt=1000, pending_reservations_usdt=0)
        history = {'assets': {'ENA': dict(first_early_price=90)}}
        pos = position()
        pos.update(degraded_cycles=0, health_state='STRONG')
        state = dict(open_positions=[pos], closed_positions=[], events=[], decisions=[],
                     systemic_risk={'level': 'NORMAL', 'raw_level': 'NORMAL', 'last_observation_id': 'fresh',
                                    'risk_release_fraction': 1., 'recovery_mode': False})
        lookup = {capital.SCAN: scan, capital.EARLY: early, capital.LIQ: liq, capital.FACTS: facts,
                  capital.SUPPLY: supply, capital.IDENTITY: identity, capital.CAPITAL: capital_state,
                  capital.HISTORY: history, capital.ROOT/'hunter-shadow-v2-portfolio.json': state}

        class Clock(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW

        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp)/'review.json'
            with patch.object(capital, 'read', side_effect=lambda path, d={}: copy.deepcopy(lookup.get(path, d))), \
                 patch.object(capital, 'OUT', out), patch.object(capital.dt, 'datetime', Clock), \
                 contextlib.redirect_stdout(io.StringIO()):
                capital.main()
            review = json.loads(out.read_text())
        self.assertEqual(review['candidates'][0]['trade_action'], 'BUY')
        self.assertEqual(review['candidates'][0]['signal']['v2_lifecycle_evidence'], expected_receipt)
        proposals = []
        # Decision, health, source validation and marginal capital gate remain real.
        with patch.object(engine, 'ENTRY_MODE', 'EXECUTABLE'), patch.object(engine, 'CAPITAL_POOL_USDT', 20000), \
             patch.object(engine, 'EVENT_PREFIX', 'SHADOW_V2'), \
             patch.object(engine, 'backfill_opportunity_history', side_effect=lambda p, n: p), \
             patch.object(engine, '_execution_source_sha', return_value='a'*40), \
             patch('urllib.request.urlopen', side_effect=AssertionError('OFFLINE')):
            engine.manage_existing_positions(state, scan, review, liq, supply, NOW, capital_proposals=proposals)
            self.assertEqual(pos['health_state'], 'STRONG')
            self.assertEqual(len(proposals), 1)
            engine.execute_capital_proposals(state, proposals, scan, NOW)
            self.assertEqual(pos['last_monitor_decision']['action'], 'ADD')
            self.assertEqual(len(state['events']), 1)
            self.assertEqual(state['events'][0]['type'], 'SHADOW_V2_ADD')
            self.assertEqual(len(pos['tranches']), 2)
            saved = copy.deepcopy(state)
            engine.execute_capital_proposals(state, proposals, scan, NOW)
            self.assertEqual(state, saved)
            self.assertEqual(state['closed_positions'], [])


if __name__ == '__main__':
    unittest.main()
