"""Synthetic state-machine fixtures; never historical execution evidence."""
import copy
import datetime as dt
import json
import unittest
from unittest.mock import patch

from research import hunter_lifecycle_v2 as v
from research import hunter_shadow_trader_v2 as e
from research import hunter_lifecycle_state as old
from research.hunter_policy import stamp

NOW = dt.datetime(2026, 10, 10, 12, tzinfo=dt.timezone.utc)


def receipt(at=NOW):
    rows = []
    end = int(at.timestamp()*1000)//900000*900000
    for i in range(24):
        start = end-(24-i)*900000
        hi, lo, close, vol = (102, 88, 95, 100)
        if 16 <= i < 20: hi, lo, close = 98, 80, 89
        if i >= 20: hi, lo, close = 94, 75, [85, 86, 93.5, 93][i-20]
        if i >= 22: vol = 50
        rows.append([start, close, hi, lo, close, 10, start+899999, vol, 3, 2, vol*.2, 0])
    return v.candle_receipt('ENAUSDT', rows, at)


def position():
    return dict(asset='ENA', shadow_id='synthetic-only', opened_at_utc=(NOW-dt.timedelta(days=1)).isoformat(),
                tranches=[dict(price=100, notional_usdt=1000, buy_slippage_bps=0, at=(NOW-dt.timedelta(days=1)).isoformat())],
                mfe_pct=0, mae_pct=0, health_state='DEGRADED', degraded_cycles=2,
                entry_thesis_evidence=dict(score=12,independent=3,btc_rel_1h=2,btc_rel_4h=3,rel_accel=1,
                    signal_evidence=stamp('ENA','entry',(NOW-dt.timedelta(days=1)).isoformat())),
                last_health_evidence_id='prior', last_health_generation_id='prior',
                last_v2_thesis_source_closed_at_ms=int(NOW.timestamp()*1000)-900001,
                last_health_observed_at_utc=(NOW-dt.timedelta(minutes=5)).isoformat())


def relative_packets(at=NOW):
    packets={};end=int(at.timestamp()*1000)//3600000*3600000
    for window,count in (('1h',2),('4h',5)):
        for role,symbol in (('asset','ENAUSDT'),('btc','BTCUSDT')):
            close=(98 if window=='1h' else 97) if role=='asset' else 100
            rows=[[end-(count-1-i)*3600000,100,101,96,close,10,
                   end-(count-1-i)*3600000+3599999,100,1,1,50,0] for i in range(count)]
            packets[role+'_'+window]=dict(schema='hunter_v2_rolling_source_v1',exchange='binance',market='spot',
                symbol=symbol,interval='1h',lookback=window,fetched_at=at.isoformat(),rows=rows)
    return packets


def strong_source(at,generation,asset='ENA'):
    micro=receipt(at);micro['symbol']=asset+'USDT'
    for r in micro['rows']:r[1]=r[4]=90;r[2]=92;r[3]=89;r[7]=100;r[10]=50
    packets=relative_packets(at)
    for key,pkt in packets.items():
        if key.startswith('asset'):
            pkt['symbol']=asset+'USDT'
            for r in pkt['rows']:r[2]=104;r[4]=102 if key.endswith('1h') else 103
    return dict(generation_id=generation,micro_receipt=micro,relative_receipts=packets)


def inputs(at=NOW, generation='g0', price=93):
    signal=dict(score=0, independent_signal_count=0, btc_relative_1h_pct=-2,
                btc_relative_4h_pct=-3, relative_acceleration_pct=-1.25, return_1h_pct=-2)
    c=dict(asset='ENA',signal=signal,signal_evidence=stamp('ENA',generation,at.isoformat()),
           execution_scenario=dict(buy_slippage_bps=10,estimated_rr=1),blockers=[],
           v2_lifecycle_evidence=dict(generation_id=generation,micro_receipt=receipt(at),relative_receipts=relative_packets(at)))
    book=dict(exchange='binance',market='spot',symbol='ENAUSDT',price_unit='USDT',quantity_unit='BASE',
              fetched_at=at.isoformat(),bids=[[price,10000]],asks=[[price+.01,10000]])
    liq=dict(snapshots=dict(ENA=dict(as_of_utc=at.isoformat(),spread_bps=10,
        bid_depth_2pct_usdt=50000,ask_depth_2pct_usdt=50000,raw_book_evidence=book)))
    scan=dict(generation_id=generation,as_of_utc=at.isoformat(),coins=dict(ENA=dict(reference_price=price)))
    return scan,dict(as_of_utc=at.isoformat(),candidates=[c]),liq,{}


class V2LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.mode=patch.object(e,'ENTRY_MODE','EXECUTABLE');self.mode.start();self.addCleanup(self.mode.stop)

    def run_state(self, state, args, at=NOW):
        with patch.object(e,'backfill_opportunity_history',side_effect=lambda p,n:p):
            e.manage_existing_positions(state,*args,at,capital_proposals=[])

    def state(self):
        return dict(open_positions=[position()],closed_positions=[],events=[dict(type='HISTORICAL_SENTINEL')],decisions=[])

    def test_unapproved_rebound_model_is_review_only_and_restart_safe(self):
        s=self.state();prior=copy.deepcopy(s['events']);args=inputs()
        self.run_state(s,args)
        self.assertEqual(s['closed_positions'],[])
        p=s['open_positions'][0]
        self.assertEqual(p['last_monitor_decision']['action'],'HOLD')
        review=p['loss_recovery_lifecycle']['rebound_review']
        self.assertEqual(review['state'],'REBOUND_EXIT_REVIEW')
        self.assertFalse(review['exit']);self.assertEqual(review['risk_comparison']['status'],'UNKNOWN')
        self.assertEqual(s['events'],prior)
        saved=json.loads(json.dumps(s));self.run_state(saved,args)
        self.assertEqual(saved['events'],s['events']);self.assertEqual(saved['closed_positions'],s['closed_positions'])
        self.assertEqual(saved['open_positions'],s['open_positions'])

    def test_each_required_condition_blocks_in_isolation(self):
        for field in ('score','independent_signal_count','btc_relative_1h_pct','btc_relative_4h_pct','relative_acceleration_pct'):
            with self.subTest(missing=field):
                s=self.state();args=inputs();args[1]['candidates'][0]['signal'].pop(field)
                self.run_state(s,args)
                self.assertEqual(len(s['events']),1)
                self.assertEqual(s['open_positions'][0]['last_monitor_decision']['thesis_status'],'EVIDENCE_PENDING')
        for variant in ('missing_original','missing_micro','wrong_generation','stale_book','strong_relative','strong_structure','strong_demand','no_rebound','no_risk_advantage','missing_price'):
            with self.subTest(variant=variant):
                s=self.state();args=inputs();c=args[1]['candidates'][0]
                if variant=='missing_original':s['open_positions'][0].pop('entry_thesis_evidence')
                if variant=='missing_micro':c.pop('v2_lifecycle_evidence')
                if variant=='wrong_generation':c['signal_evidence']['generation_id']='wrong'
                if variant=='stale_book':args[2]['snapshots']['ENA']['as_of_utc']=(NOW-dt.timedelta(hours=2)).isoformat()
                if variant=='strong_relative':c['signal']['btc_relative_1h_pct']=1
                rows=c.get('v2_lifecycle_evidence',{}).get('micro_receipt',{}).get('rows',[])
                if variant=='strong_structure':
                    for r in rows[-4:]:r[2]=103;r[3]=81;r[1]=r[4]=94
                if variant=='strong_demand':
                    for r in rows[-2:]:r[7]=200;r[10]=160
                if variant=='no_rebound':rows[-1][1]=rows[-1][4]=93.8
                if variant=='no_risk_advantage':args=inputs(price=80)
                if variant=='missing_price':args[0]['coins'].pop('ENA')
                self.run_state(s,args)
                self.assertEqual(len(s['events']),1);self.assertEqual(len(s['open_positions']),1)
                self.assertIsNotNone(s['open_positions'][0]['last_monitor_decision']['reasons'])

    def test_missing_is_not_zero_volume_weakness_or_strong_health(self):
        s=self.state();args=inputs();c=args[1]['candidates'][0]
        c['signal'].update(score=12,independent_signal_count=4)
        c['signal'].pop('btc_relative_1h_pct');c['signal'].pop('btc_relative_4h_pct')
        c.pop('v2_lifecycle_evidence');self.run_state(s,args)
        p=s['open_positions'][0];d=p['last_monitor_decision']
        self.assertEqual(d['thesis_status'],'EVIDENCE_PENDING')
        self.assertEqual(p['health_state'],'DEGRADED')
        self.assertEqual(d['thesis_review']['checks']['volume_demand'],'UNKNOWN')

    def test_partial_bar_cannot_become_completed_after_fetch(self):
        at=NOW-dt.timedelta(seconds=2);r=receipt(at)
        start=int(NOW.timestamp()*1000)-900000
        r['rows'].append([start,93,94,92,93,1,start+899999,.01,1,0,0,0])
        t=v.technical(r,'ENA',NOW+dt.timedelta(seconds=2))
        self.assertEqual(t['status'],'FRESH')
        self.assertLess(t['source_closed_at_ms'],start)

    def test_fresh_fetch_of_old_bars_is_unknown(self):
        r=receipt(NOW-dt.timedelta(minutes=15));r['fetched_at']=NOW.isoformat()
        self.assertEqual(v.technical(r,'ENA',NOW)['status'],'UNKNOWN')

    def test_duplicate_old_and_stale_generation_preserve_state(self):
        s=self.state();args=inputs();args[1]['candidates'][0].pop('v2_lifecycle_evidence')
        self.run_state(s,args);before=copy.deepcopy(s)
        self.run_state(s,args);self.assertEqual(s,before)
        self.run_state(s,inputs(NOW-dt.timedelta(minutes=5),'older'),NOW-dt.timedelta(minutes=5))
        self.assertEqual(s['open_positions'],before['open_positions'])
        p=s['open_positions'][0];mark=p['last_price'];count=p['degraded_cycles']
        self.run_state(s,inputs(NOW-dt.timedelta(hours=1),'stale'),NOW+dt.timedelta(minutes=5))
        self.assertEqual(p['last_price'],mark);self.assertEqual(p['degraded_cycles'],count)
        self.assertIn('MARKET_EVIDENCE_STALE_NO_LIFECYCLE_MUTATION',p['last_monitor_decision']['reasons'])

    def test_recovery_count_and_canonical_state_survive_review_cancel(self):
        p=position();p['recovery_state']='LOSS_RECOVERY'
        for i in range(3):
            now=NOW+dt.timedelta(minutes=5*i);gen='strong'+str(i)
            ev=dict(score=12,independent=3,btc_rel_1h=2,btc_rel_4h=3,rel_accel=1,
                signal_evidence=stamp('ENA',gen,now.isoformat()),
                v2_thesis_review=dict(generation_id=gen,checks=dict(signal='FRESH'),technical={}))
            old.recovery(p,ev,now,-20,gen,'STRONG')
            result=v.rebound_review(p,ev,now,dict(net_pnl_usdt=-20),'STRONG',gen)
            self.assertFalse(result['exit']);self.assertEqual(result['state'],'CANCELLED_RECOVERY')
        self.assertEqual(p['recovery_state'],'RECOVERED')

    def test_old_source_with_new_processing_time_cannot_change_mark_or_protection(self):
        s=self.state();args=inputs();args[1]['candidates'][0].pop('v2_lifecycle_evidence')
        self.run_state(s,args);p=s['open_positions'][0];before=copy.deepcopy(p)
        older=inputs(NOW-dt.timedelta(minutes=1),'old-source',price=120)
        self.run_state(s,older,NOW+dt.timedelta(minutes=1))
        for k in ('last_price','mfe_pct','mae_pct','protection_lifecycle','health_state','degraded_cycles'):
            self.assertEqual(p[k],before[k])
        self.assertEqual(p['last_monitor_decision']['reasons'],['MARKET_SOURCE_NOT_NEWER_NO_LIFECYCLE_MUTATION'])

    def test_missing_or_invalid_market_admission_never_exits(self):
        for field,value in [('as_of_utc',None),('as_of_utc',''),('as_of_utc','invalid'),('generation_id',None)]:
            with self.subTest(field=field,value=value):
                s=self.state();args=inputs();args[0][field]=value;before=copy.deepcopy(s['events'])
                self.run_state(s,args)
                self.assertEqual(s['events'],before)
                p=s['open_positions'][0];self.assertNotIn('last_price',p)
                self.assertEqual(p['last_monitor_decision']['thesis_status'],'EVIDENCE_PENDING')

    def test_legacy_thesis_is_exact_metadata_not_new_signal(self):
        rows=v.legacy_entries();row=next(r for r in rows if r['asset']=='NEXO')
        p=dict(asset=row['asset'],shadow_id=row['shadow_id'],opened_at_utc=row['opened_at_utc'],
               tranches=[copy.deepcopy(row['first_tranche_identity'])])
        v.hydrate_legacy_entry(p)
        self.assertTrue(v.entry_thesis_known(p))
        self.assertFalse((p['entry_thesis_evidence'].get('signal_evidence') or {}).get('evidence_id'))
        self.assertEqual(p['entry_thesis_evidence']['historical_provenance']['commit_sha'],row['evidence']['historical_provenance']['commit_sha'])
        p.pop('entry_thesis_evidence');p['tranches'][0]['price']+=1;v.hydrate_legacy_entry(p)
        self.assertNotIn('entry_thesis_evidence',p)

    def test_archive_retains_final_decision_receipt(self):
        s=self.state();args=inputs();args[2]['snapshots']['ENA']['spread_bps']=1000
        self.run_state(s,args);p=s['closed_positions'][0];p['observation_complete']=True
        with patch.object(e,'MAX_CLOSED_HOT',0):e.compact_closed_history(s)
        self.assertEqual(s['closed_trade_archive'][0]['last_monitor_decision'],p['last_monitor_decision'])

    def test_v1_has_no_new_receipt_or_loss_rule(self):
        s=self.state()
        with patch.object(e,'ENTRY_MODE','DISCOVERY'):self.run_state(s,inputs())
        self.assertNotIn('last_monitor_decision',s['open_positions'][0])
        self.assertEqual(len(s['events']),1)


if __name__=='__main__':unittest.main()
