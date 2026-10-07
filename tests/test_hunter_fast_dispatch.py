import asyncio
import time
import unittest
from types import SimpleNamespace

from research.hunter_fast_watch import Config
from scripts.hunter_market_stream import active_market_reviews


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_probe_cannot_hold_a_fresh_review(self):
        stop=asyncio.Event();release=asyncio.Event();entered=asyncio.Event()
        reviewed=asyncio.Event();watch=SimpleNamespace(config=Config(),pending={'p':{'received_at':time.time()}})
        watch.probe_due=lambda now:['SLOWUSDT']
        watch.record=lambda *args,**kwargs:None
        async def probe(symbol):
            entered.set();await release.wait()
        async def review(key,trigger):reviewed.set()
        task=asyncio.create_task(active_market_reviews(watch,probe,review,stop))
        try:
            await asyncio.wait_for(entered.wait(),.5)
            await asyncio.wait_for(reviewed.wait(),.1)
            self.assertFalse(release.is_set())
        finally:
            stop.set();task.cancel();await asyncio.gather(task,return_exceptions=True)

    async def test_armed_exit_review_precedes_arm(self):
        stop=asyncio.Event();started=[];entered=asyncio.Event();release=asyncio.Event()
        watch=SimpleNamespace(config=Config(),pending={
            'arm':{'received_at':time.time(),'kind':'FAST_ARM_REVIEW'},
            'exit':{'received_at':time.time(),'kind':'FAST_EXIT_OR_PEAK_REVIEW'}})
        watch.probe_due=lambda now:[];watch.record=lambda *args,**kwargs:None
        async def review(key,trigger):
            started.append(key)
            if len(started)==2:entered.set()
            await release.wait()
        task=asyncio.create_task(active_market_reviews(watch,None,review,stop))
        try:
            await asyncio.wait_for(entered.wait(),.5)
            self.assertEqual(started,['exit','arm'])
        finally:
            stop.set();task.cancel();await asyncio.gather(task,return_exceptions=True)

    async def test_stale_pending_does_not_spend_depth_budget(self):
        stop=asyncio.Event();rejected=asyncio.Event();starts=[]
        watch=SimpleNamespace(config=Config(),pending={'p':{'received_at':time.time()-10}})
        watch.probe_due=lambda now:[]
        def record(kind,now,**data):
            self.assertEqual(data['result']['status'],'STALE_TRIGGER');rejected.set()
        watch.record=record
        async def review(key,trigger):starts.append(key)
        task=asyncio.create_task(active_market_reviews(watch,None,review,stop))
        try:
            await asyncio.wait_for(rejected.wait(),.5)
            self.assertEqual(starts,[])
        finally:
            stop.set();task.cancel();await asyncio.gather(task,return_exceptions=True)

    async def test_one_inflight_review_and_shutdown_cancels_children(self):
        stop=asyncio.Event();entered=asyncio.Event();cancelled=asyncio.Event();starts=[]
        watch=SimpleNamespace(config=Config(),pending={'p':{'received_at':time.time()}})
        watch.probe_due=lambda now:[];watch.record=lambda *args,**kwargs:None
        async def review(key,trigger):
            starts.append(key);entered.set()
            try:await asyncio.Event().wait()
            finally:cancelled.set()
        task=asyncio.create_task(active_market_reviews(watch,None,review,stop))
        try:
            await asyncio.wait_for(entered.wait(),.5)
            watch.pending['p']={'received_at':time.time()}
            await asyncio.sleep(1.05)
            self.assertEqual(starts,['p'])
            self.assertIn('p',watch.pending)
        finally:
            stop.set();task.cancel();await asyncio.gather(task,return_exceptions=True)
        self.assertTrue(cancelled.is_set())

    async def test_probe_exception_does_not_stop_depth_reviews(self):
        stop=asyncio.Event();failed=asyncio.Event();reviewed=asyncio.Event()
        watch=SimpleNamespace(config=Config(),pending={})
        watch.probe_due=lambda now:['ENAUSDT']
        def record(kind,now,**data):
            if kind=='PROBE_TASK_FAILED':failed.set()
        watch.record=record
        async def probe(symbol):raise RuntimeError('SIMULATED_PROBE_ERROR')
        async def review(key,trigger):reviewed.set()
        task=asyncio.create_task(active_market_reviews(watch,probe,review,stop))
        try:
            await asyncio.wait_for(failed.wait(),1.5)
            watch.pending['p']={'received_at':time.time()}
            await asyncio.wait_for(reviewed.wait(),1.5)
        finally:
            stop.set();task.cancel();await asyncio.gather(task,return_exceptions=True)

    async def test_probe_fanout_reserves_capacity_and_deduplicates_symbols(self):
        stop=asyncio.Event();release=asyncio.Event();entered=asyncio.Event()
        starts=[];watch=SimpleNamespace(config=Config(),pending={})
        watch.probe_due=lambda now:['BTCUSDT','ENAUSDT','HUMAUSDT','IOUSDT']
        watch.record=lambda *args,**kwargs:None
        async def probe(symbol):
            starts.append(symbol)
            if len(starts)>=2:entered.set()
            await release.wait()
        task=asyncio.create_task(active_market_reviews(watch,probe,None,stop))
        try:
            await asyncio.wait_for(entered.wait(),.5)
            await asyncio.sleep(.05)
            self.assertEqual(starts,['BTCUSDT','ENAUSDT'])
        finally:
            stop.set();task.cancel();await asyncio.gather(task,return_exceptions=True)
