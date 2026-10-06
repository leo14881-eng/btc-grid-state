import asyncio
import copy
import datetime as dt
import json
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from research.hunter_fast_watch import Config, Watch, DualWatch, utc
from scripts.hunter_market_stream import PublicREST, BybitREST, atomic_state, restore, watchdog


def position(venue='BINANCE_SPOT', key='p', asset='ENA'):
    return dict(shadow_id=key, asset=asset, execution_venue=venue, market_symbol=asset+'USDT',
        market_type='spot', execution_fee_bps=10, capital_authority='NONE_SHADOW_ONLY',
        tranches=[dict(price=1, notional_usdt=1000, buy_slippage_bps=0)])


def portfolio(*positions):
    return dict(mode='SIMULATION_ONLY_NO_REAL_ORDERS', open_positions=list(positions), closed_positions=[])


def event(seq=1, price=1.04, symbol='ENAUSDT', at=None):
    x=dict(s=symbol, u=seq, b=str(price), a=str(price+.0001))
    if at is not None: x['E']=int(at*1000)
    return x


def bybit_event(seq=1, price=1.04, at=100):
    return dict(topic='orderbook.1.ENAUSDT', type='snapshot', ts=int(at*1000),
        data=dict(s='ENAUSDT', u=seq, seq=seq, b=[[str(price),'10000']], a=[[str(price+.0001),'10000']]))


def book(price=1.04, at=100, venue='binance', qty=10000):
    return dict(exchange=venue, market='spot', symbol='ENAUSDT', price_unit='USDT',quantity_unit='BASE',
        fetched_at=utc(at), source_timestamp=at*1000 if venue=='bybit' else None,
        bids=[[str(price),str(qty)]], asks=[[str(price+.0001),str(qty)]])


class FastWatchTests(unittest.TestCase):
    def setUp(self):
        self.p=position(); self.ledger=portfolio(self.p); self.w=Watch()
        self.w.reconcile(self.ledger,'sha',100); self.w.connect(100)

    def review(self, price, at, seq=1, venue='binance'):
        self.w.event(event(seq,price,at=at),at)
        trigger=self.w.pending.pop('p')
        return self.w.review('p',trigger,book(price,at,venue),at)

    def test_connect(self): self.assertTrue(self.w.connected)
    def test_disconnect(self):
        self.w.disconnect(101); self.assertFalse(self.w.connected);self.assertIn('ENAUSDT',self.w.probe_due(101))
    def test_reconnect(self):
        self.w.disconnect(101);self.w.connect(102);self.assertEqual(self.w.connection_id,'2')
    def test_rotation_24h(self):
        self.assertFalse(self.w.rotation_due(101));self.assertTrue(self.w.rotation_due(100+24*3600))
    def test_tcp_connected_no_market(self):
        self.assertIn('ENAUSDT',self.w.probe_due(112));self.assertTrue(self.w.connected)
    def test_symbol_stale_is_independent(self):
        self.w.event(event(symbol='BTCUSDT'),111)
        self.assertEqual(self.w.probe_due(112),['ENAUSDT'])
    def test_all_symbols_stale(self): self.assertEqual(len(self.w.probe_due(112)),2)
    def test_duplicate(self):
        self.w.event(event(),100);self.assertEqual(self.w.event(event(),101),'DEDUPLICATE')
        self.assertEqual(self.w.symbols['ENAUSDT']['last_valid_event_at'],100)
    def test_old_event(self):
        self.w.event(event(2),100);self.assertEqual(self.w.event(event(1),101),'DROP_OLD_EVENT')
    def test_future_event(self): self.assertEqual(self.w.event(event(at=110),100),'EVENT_TIME_INVALID')
    def test_stale_exchange_time(self): self.assertEqual(self.w.event(event(at=90),100),'EVENT_TIME_INVALID')
    def test_wrong_symbol(self): self.assertEqual(self.w.event(event(symbol='FAKEUSDT'),100),'SYMBOL_IDENTITY_INVALID')
    def test_stream_identity(self):
        self.assertNotEqual(self.w.event(dict(stream='fakeusdt@bookTicker',data=event()),100),'ACCEPTED')
    def test_book_receipt_not_exchange_timestamp(self):
        self.w.event(event(),100);self.assertIsNone(self.w.symbols['ENAUSDT']['event_time'])
        self.assertEqual(self.w.symbols['ENAUSDT']['timestamp_quality'],'RECEIPT_BOUND')
    def test_aggtrade_does_not_mask_stale_book(self):
        self.w.event(dict(e='aggTrade',s='ENAUSDT',a=1,p='1.04',E=112000),112)
        self.assertIn('ENAUSDT',self.w.probe_due(112))
    def test_dynamic_buy_subscribe(self):
        change=self.w.reconcile(portfolio(self.p,position(key='b',asset='HUMA')),'new',101)
        self.assertIn('HUMAUSDT',change['subscribe'])
    def test_dynamic_sell_unsubscribe(self):
        self.w.actual={'BTCUSDT','ENAUSDT'}
        self.assertEqual(self.w.reconcile(portfolio(),'new',101)['unsubscribe'],['ENAUSDT'])
    def test_scope_limit_not_universe(self):
        w=Watch(Config(max_symbols=1))
        with self.assertRaises(ValueError): w.reconcile(self.ledger,'sha',100)
    def test_stale_rest_probe(self):
        self.w.probe_due(112)
        self.assertEqual(self.w.rest('ENAUSDT',dict(symbol='ENAUSDT',bidPrice='1.04',askPrice='1.041'),112,113),'REST_FALLBACK_ACTIVE')
        self.assertIn('p',self.w.pending)
    def test_rest_failure(self):
        self.w.probe_due(112);self.assertEqual(self.w.rest('ENAUSDT',{},112,113,'HTTP_451'),'FAST_MARKET_DATA_DEGRADED')
        self.assertEqual(self.w.pending,{})
    def test_both_sources_unavailable(self):
        self.w.probe_due(170);self.assertEqual(self.w.rest('ENAUSDT',{},170,171,'TIMEOUT'),'MARKET_DATA_UNAVAILABLE')
    def test_rest_recovery_requires_multiple_events(self):
        self.w.probe_due(112);self.w.rest('ENAUSDT',dict(symbol='ENAUSDT',bidPrice='1.04',askPrice='1.041'),112,113)
        for seq in (1,2):self.w.event(event(seq,at=113+seq),113+seq)
        self.assertTrue(self.w.symbols['ENAUSDT']['fallback'])
        self.w.event(event(3,at=116),116);self.assertFalse(self.w.symbols['ENAUSDT']['fallback'])
    def test_recovery_old_event(self):
        self.w.event(event(8),100);self.w.probe_due(112)
        self.w.rest('ENAUSDT',dict(symbol='ENAUSDT',bidPrice='1.04',askPrice='1.041'),112,113)
        self.w.event(event(7,at=114),114);self.assertEqual(self.w.symbols['ENAUSDT']['recovery_count'],0)
    def test_rest_conflict_does_not_trigger(self):
        self.w.probe_due(112);self.w.rest('ENAUSDT',dict(symbol='ENAUSDT',bidPrice='1',askPrice='1.001'),112,113)
        self.w.event(event(1,1.04,at=114),114)
        self.assertEqual(self.w.pending,{})
    def test_arm_not_sell(self):
        result=self.review(1.04,100);self.assertTrue(result['armed']);self.assertFalse(result['exit'])
    def test_new_peak_no_mechanical_exit(self):
        self.review(1.04,100);result=self.review(1.06,106,2);self.assertFalse(result['exit'])
    def test_positive_giveback(self):
        self.review(1.04,100);result=self.review(1.005,106,2);self.assertTrue(result['exit'])
        self.assertGreater(result['execution']['net_pnl_usdt'],0)
    def test_gap_no_fake_profit(self):
        self.review(1.04,100);result=self.review(.99,106,2)
        self.assertFalse(result['exit']);self.assertEqual(result['incident'],'GAPPED_THROUGH_PROTECTION_WINDOW')
    def test_unarmed_below_threshold(self):
        self.w.event(event(price=1.01),100);self.assertEqual(self.w.pending,{})
    def test_fresh_depth_required(self):
        self.w.event(event(),100);t=self.w.pending.pop('p')
        self.assertEqual(self.w.review('p',t,book(at=90),100)['status'],'FRESH_FULL_DEPTH_REQUIRED')
    def test_full_quantity_required(self):
        self.w.event(event(),100);t=self.w.pending.pop('p')
        self.assertEqual(self.w.review('p',t,book(qty=1),100)['status'],'FRESH_FULL_DEPTH_REQUIRED')
    def test_stale_trigger(self):
        self.w.event(event(),100);t=self.w.pending.pop('p')
        self.assertEqual(self.w.review('p',t,book(at=110),110)['status'],'STALE_TRIGGER')
    def test_no_double_exit(self):
        self.review(1.04,100);self.review(1.005,106,2)
        self.w.event(event(3,1.005,at=112),112);self.assertEqual(self.w.pending,{})
    def test_duplicate_transition(self):
        self.w.event(event(),100);t=self.w.pending.pop('p');self.w.review('p',t,book(),100)
        self.assertEqual(self.w.review('p',t,book(),100)['status'],'DEDUPLICATE')
    def test_monitor_concurrent_reconcile_no_write(self):
        before=copy.deepcopy(self.ledger);self.review(1.04,100)
        self.w.reconcile(self.ledger,'nextsha',105)
        self.assertEqual(self.ledger,before);self.assertEqual(self.w.counterfactual['p']['protection_lifecycle']['state'],'ARMED')
    def test_single_writer_no_formal_mutation(self):
        result=self.review(1.04,100);self.assertFalse(result['formal_mutation'])
        self.assertNotIn('protection_lifecycle',self.p)
    def test_add_during_depth_review_rejects_old_cashflow(self):
        self.w.event(event(),100);t=self.w.pending.pop('p')
        p=copy.deepcopy(self.p);p['tranches'].append(dict(price=.9,notional_usdt=1000))
        self.w.reconcile(portfolio(p),'newsha',101)
        self.assertEqual(self.w.review('p',t,book(at=101),101)['status'],'POSITION_REBOUND_REVIEW_REJECTED')
    def test_restart_preserves_arm_requires_fresh_quotes(self):
        self.review(1.04,100)
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'state.json';row=self.w.snapshot(101);row['counterfactual']=self.w.counterfactual;atomic_state(path,row)
            new=Watch();new.reconcile(self.ledger,'sha',102);restore(new,path,102)
            self.assertEqual(new.counterfactual['p']['protection_lifecycle']['state'],'ARMED')
            self.assertFalse(new.connected);self.assertIsNone(new.symbols['ENAUSDT']['last_valid_event_at'])
    def test_stale_runtime_discard(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'state.json';atomic_state(path,self.w.snapshot(100))
            self.assertEqual(restore(self.w,path,200),'STALE_LOCAL_RUNTIME_DISCARDED')
    def test_watchdog_missing_is_optional(self):
        self.assertFalse(watchdog('/nonexistent',100)['authoritative_monitor_affected'])
    def test_shadow_boundaries(self):
        x=self.w.snapshot(100);self.assertEqual(x['real_order_count'],0);self.assertFalse(x['real_trading_enabled'])
        self.assertEqual(x['capital_authority'],'NONE_SHADOW_ONLY')
    def test_api_orders_refused(self):
        rest=PublicREST(None,Config())
        with self.assertRaises(ValueError): asyncio.run(rest.get('/api/v3/order',{}))
    def test_health_fallback_not_hunter_failed(self):
        self.w.probe_due(112)
        for s in self.w.symbols:self.w.rest(s,dict(symbol=s,bidPrice='1.04',askPrice='1.041'),112,113)
        self.assertEqual(self.w.snapshot(114)['status'],'FAST_PATH_DEGRADED_REST_FALLBACK_HEALTHY')
    def test_missing_venue_never_guessed(self):
        p=position();p.pop('execution_venue')
        d=DualWatch();d.reconcile(portfolio(p),'sha',100)
        self.assertEqual(len(d.unroutable),1);self.assertNotIn('ENAUSDT',d.watches['BINANCE_SPOT'].symbols)


class DualVenueTests(unittest.TestCase):
    def setUp(self):
        self.p=position('BYBIT_SPOT');self.w=Watch(venue='BYBIT_SPOT')
        self.w.reconcile(portfolio(self.p),'sha',100);self.w.connect(100)
    def test_bybit_only_routing(self): self.assertEqual(set(self.w.symbols),{'ENAUSDT'})
    def test_binance_only_routing(self):
        w=Watch();w.reconcile(portfolio(position()),'sha',100);self.assertIn('ENAUSDT',w.symbols)
    def test_dual_listed_uses_explicit_venue(self):
        d=DualWatch();d.reconcile(portfolio(self.p),'sha',100)
        self.assertNotIn('ENAUSDT',d.watches['BINANCE_SPOT'].symbols)
    def test_bybit_snapshot(self): self.assertEqual(self.w.event(bybit_event(),100),'ACCEPTED')
    def test_bybit_idle_snapshot_same_update_new_timestamp(self):
        self.w.event(bybit_event(),100);self.assertEqual(self.w.event(bybit_event(at=103),103),'ACCEPTED')
    def test_bybit_ticker_not_fake_bid(self):
        self.assertEqual(self.w.event(dict(topic='tickers.ENAUSDT',type='snapshot',ts=100000,data=dict(symbol='ENAUSDT',lastPrice='1.04')),100),'SYMBOL_IDENTITY_INVALID')
    def test_bybit_stale(self):self.assertEqual(self.w.probe_due(112),['ENAUSDT'])
    def test_bybit_rest_takeover(self):
        self.w.probe_due(112);self.assertEqual(self.w.rest('ENAUSDT',dict(symbol='ENAUSDT',bidPrice='1.04',askPrice='1.041',source_timestamp=113000),112,113),'REST_FALLBACK_ACTIVE')
    def test_bybit_rest_timestamp_required(self):
        self.w.probe_due(112);self.assertEqual(self.w.rest('ENAUSDT',dict(symbol='ENAUSDT',bidPrice='1.04',askPrice='1.041'),112,113),'FAST_MARKET_DATA_DEGRADED')
    def test_bybit_both_failed(self):
        self.w.probe_due(112);self.assertEqual(self.w.rest('ENAUSDT',{},112,113,'HTTP_403'),'FAST_MARKET_DATA_DEGRADED')
    def test_bybit_primary_depth_match(self):
        self.w.event(bybit_event(),100);t=self.w.pending.pop('p')
        result=self.w.review('p',t,book(venue='bybit'),100);self.assertTrue(result['armed'])
    def test_mixed_venue_rejected(self):
        self.w.event(bybit_event(),100);t=self.w.pending.pop('p')
        self.assertEqual(self.w.review('p',t,book(),100)['status'],'VENUE_MATCH_REQUIRED')
    def test_binance_mixed_rejected(self):
        w=Watch();w.reconcile(portfolio(position()),'sha',100);w.event(event(),100);t=w.pending.pop('p')
        self.assertEqual(w.review('p',t,book(venue='bybit'),100)['status'],'VENUE_MATCH_REQUIRED')
    def test_unknown_bybit_fee_no_arm(self):
        self.w.counterfactual['p'].pop('execution_fee_bps');self.w.event(bybit_event(),100);t=self.w.pending.pop('p')
        self.assertEqual(self.w.review('p',t,book(venue='bybit'),100)['status'],'FRESH_FULL_DEPTH_REQUIRED')
    def test_bybit_subscribe_and_unsubscribe(self):
        self.w.actual={'ENAUSDT'};change=self.w.reconcile(portfolio(position('BYBIT_SPOT','h','HUMA')),'new',102)
        self.assertEqual(change,dict(subscribe=['HUMAUSDT'],unsubscribe=['ENAUSDT']))
    def test_armed_disconnect_immediate_probe(self):
        self.w.event(bybit_event(),100);t=self.w.pending.pop('p');self.w.review('p',t,book(venue='bybit'),100)
        self.w.disconnect(101);self.assertEqual(self.w.probe_due(101),['ENAUSDT'])
    def test_fallback_arm_and_exit_venue_matched(self):
        for at,price in [(113,1.04),(120,1.005)]:
            self.w.probe_due(at);self.w.rest('ENAUSDT',dict(symbol='ENAUSDT',bidPrice=str(price),askPrice=str(price+.0001),source_timestamp=at*1000),at,at)
            t=self.w.pending.pop('p');r=self.w.review('p',t,book(price,at,'bybit'),at)
        self.assertTrue(r['exit']);self.assertGreater(r['execution']['net_pnl_usdt'],0)
    def test_venues_isolated_no_secondary_sell(self):
        d=DualWatch();d.reconcile(portfolio(self.p,position('BINANCE_SPOT','b')),'sha',100)
        d.watches['BINANCE_SPOT'].event(event(),100)
        self.assertEqual(d.watches['BYBIT_SPOT'].pending,{})
    def test_cross_venue_divergence_not_average(self):
        d=DualWatch();d.reconcile(portfolio(self.p,position('BINANCE_SPOT','b')),'sha',100)
        d.watches['BINANCE_SPOT'].event(event(price=1),100);d.watches['BYBIT_SPOT'].event(bybit_event(price=1.1),100)
        row=d.snapshot(100)['cross_venue'][0];self.assertEqual(row['status'],'CROSS_VENUE_PRICE_DIVERGENCE')
        self.assertFalse(row['execution_substitution_allowed'])


class TransportTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(importlib.util.find_spec('aiohttp'), 'resident Fast Watch transport dependency not installed')
    async def test_real_local_websocket_ping_pong_and_disconnect(self):
        import aiohttp
        from aiohttp import web
        seen=asyncio.Event()
        async def handler(request):
            ws=web.WebSocketResponse(autoping=False);await ws.prepare(request)
            await ws.ping(b'hunter-proof')
            msg=await ws.receive(timeout=2)
            if msg.type==aiohttp.WSMsgType.PONG and msg.data==b'hunter-proof':seen.set()
            await ws.send_json(event());await ws.close();return ws
        app=web.Application();app.router.add_get('/ws',handler)
        runner=web.AppRunner(app);await runner.setup();site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
        port=site._server.sockets[0].getsockname()[1]
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(f'http://127.0.0.1:{port}/ws',autoping=True) as ws:
                    msg=await ws.receive(timeout=2);self.assertEqual(msg.type,aiohttp.WSMsgType.TEXT)
                    self.assertEqual(json.loads(msg.data)['s'],'ENAUSDT')
                    await asyncio.wait_for(seen.wait(),2)
                    msg=await ws.receive(timeout=2);self.assertEqual(msg.type,aiohttp.WSMsgType.CLOSE)
        finally:await runner.cleanup()
    async def test_public_rest_budget(self):
        rest=PublicREST(None,Config(weight_budget_per_minute=1))
        with self.assertRaisesRegex(RuntimeError,'BUDGET'):await rest.get('/api/v3/ticker/bookTicker',{'symbol':'ENAUSDT'})
    async def test_rest_circuit_is_independent_of_ws(self):
        rest=PublicREST(None,Config());rest.blocked_until=__import__('time').time()+30
        with self.assertRaisesRegex(RuntimeError,'CIRCUIT_OPEN'):await rest.get('/api/v3/depth',{'symbol':'ENAUSDT'})
        watch=Watch();watch.reconcile(portfolio(position()),'sha',100);watch.connect(100)
        self.assertEqual(watch.event(event(),100),'ACCEPTED')
    async def test_bybit_private_endpoint_refused(self):
        rest=BybitREST(None,Config())
        with self.assertRaises(ValueError):await rest.get('/v5/order/create',{})
    async def test_bounded_concurrency(self):
        rest=PublicREST(None,Config(concurrency=2));active=0;peak=0
        async def job():
            nonlocal active,peak
            async with rest.semaphore:
                active+=1;peak=max(peak,active);await asyncio.sleep(.01);active-=1
        await asyncio.gather(*(job() for _ in range(8)))
        self.assertEqual(peak,2)


if __name__=='__main__': unittest.main()
