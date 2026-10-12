"""Actual OS processes, different working directories, one durable governor."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from urllib.parse import urlsplit

from research.hunter_bybit_stream_store import Store, Budget
from research.hunter_bybit_public import Client


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'shared.db'
        self.store=Store(self.path)
        self.budget=Budget(self.store)

    def test_spacing_and_maximum_four_inflight_are_global(self):
        tokens=[]
        for n in range(4):
            tokens.append(self.budget.acquire(1000+n*200))
            self.assertIsNotNone(tokens[-1])
            self.assertIsNone(Budget(Store(self.path)).acquire(1000+n*200+199))
        self.assertIsNone(Budget(Store(self.path)).acquire(1800))
        self.budget.release(tokens[0])
        self.assertIsNotNone(Budget(Store(self.path)).acquire(1800))

    def test_crash_or_restart_does_not_expire_held_leases(self):
        for n in range(4): self.budget.acquire(1000+n*200)
        self.assertIsNone(Budget(Store(self.path)).acquire(10_000_000))

    def test_cooldown_and_halt_survive_new_process_state(self):
        self.budget.failure(403,'access too frequent',1000)
        other=Budget(Store(self.path))
        self.assertIsNone(other.acquire(600_999))
        token=other.acquire(601_000)
        self.assertIsNotNone(token)
        other.release(token)
        other.failure(403,'country block',601_001)
        with self.assertRaisesRegex(RuntimeError,'COUNTRY_403'):
            Budget(Store(self.path)).acquire(10_000_000)

    def test_unknown_403_is_not_automatically_retried(self):
        self.budget.failure(403,'unclassified',1000)
        with self.assertRaisesRegex(RuntimeError,'UNKNOWN_403'):
            self.budget.acquire(9999999)

    def test_429_retry_after_and_clock_rollback(self):
        self.budget.failure(429,'',1000,8)
        self.assertIsNone(self.budget.acquire(8999))
        self.assertIsNotNone(self.budget.acquire(9000))
        with self.assertRaisesRegex(RuntimeError,'CLOCK_ROLLBACK'):self.budget.acquire(8999)

    def test_six_processes_different_checkout_dirs_share_rate_and_inflight(self):
        if not hasattr(os,'fork'):
            self.fail('This acceptance requires Linux fork, not a skip')
        children=[]
        saturated=Path(self.temp.name)/'four-held'
        for index in range(6):
            cwd=Path(self.temp.name)/f'checkout-{index}';cwd.mkdir()
            pid=os.fork()
            if pid==0:
                try:
                    os.chdir(cwd)
                    governor=Budget(Store(self.path))
                    deadline=time.monotonic()+10
                    token=None
                    while token is None and time.monotonic()<deadline:
                        token=governor.acquire()
                        if token is None:time.sleep(.005)
                    if token is None:os._exit(2)
                    with governor.store.db(readonly=True) as db:
                        acquired=db.execute('SELECT acquired_ms FROM leases WHERE token=?',(token,)).fetchone()[0]
                    # Force actual saturation before releases. A fixed .85s
                    # sleep could yield only three overlaps on a busy CI host,
                    # without testing the fourth slot or revealing a violation.
                    while not saturated.exists() and time.monotonic()<deadline:
                        with governor.store.db(readonly=True) as db:
                            held=db.execute('SELECT COUNT(*) FROM leases').fetchone()[0]
                        if held>=4:saturated.write_text('four requests are held')
                        else:time.sleep(.005)
                    if not saturated.exists():os._exit(4)
                    time.sleep(.1)
                    released=time.monotonic_ns()//1_000_000
                    governor.release(token)
                    (cwd/'receipt.json').write_text(json.dumps([acquired,released]))
                    os._exit(0)
                except BaseException:
                    os._exit(3)
            children.append(pid)
        for pid in children:
            _,status=os.waitpid(pid,0)
            self.assertEqual(os.waitstatus_to_exitcode(status),0)
        receipts=[json.loads(p.read_text()) for p in Path(self.temp.name).glob('checkout-*/receipt.json')]
        self.assertEqual(len(receipts),6)
        starts=sorted(row[0] for row in receipts)
        self.assertTrue(all(b-a>=200 for a,b in zip(starts,starts[1:])),starts)
        events=sorted([(s,1) for s,_ in receipts]+[(e,-1) for _,e in receipts])
        inflight=peak=0
        for _,delta in events:inflight+=delta;peak=max(peak,inflight)
        self.assertLessEqual(peak,4)
        self.assertEqual(peak,4)
        print('BYBIT_SHARED_BUDGET_ACCEPTANCE '+json.dumps(dict(processes=6,checkouts=6,
              min_spacing_ms=min(b-a for a,b in zip(starts,starts[1:])),peak_inflight=peak)))

    def test_mixed_metadata_and_kline_clients_share_one_process_budget(self):
        children=[];saturated=Path(self.temp.name)/'mixed-four'
        paths=['/v5/market/instruments-info','/v5/market/tickers','/v5/market/kline']*2
        for index,path in enumerate(paths):
            cwd=Path(self.temp.name)/f'mixed-{index}';cwd.mkdir()
            pid=os.fork()
            if pid==0:
                try:
                    os.chdir(cwd);record={};deadline=time.monotonic()+12
                    def transport(url,end,cancelled,on_denial,before_go):
                        before_go();record['path']=urlsplit(url).path
                        while not saturated.exists() and time.monotonic()<deadline:
                            with client.store.db(readonly=True) as db:
                                count=db.execute('SELECT COUNT(*) FROM leases').fetchone()[0]
                            if count==4:saturated.write_text('four')
                            else:time.sleep(.005)
                        if not saturated.exists():raise RuntimeError('not saturated')
                        time.sleep(.1)
                        record['end']=time.monotonic_ns()//1_000_000
                        return 200,b'{"retCode":0,"result":{"category":"spot"}}',1
                    client=Client(Store.reader(self.path),transport)
                    acquire=client.budget.acquire
                    def admission(*args,**kwargs):
                        token=acquire(*args,**kwargs)
                        if token:
                            with client.store.db(readonly=True) as db:
                                record['start']=db.execute('SELECT acquired_ms FROM leases WHERE token=?',(token,)).fetchone()[0]
                        return token
                    client.budget.acquire=admission
                    params=dict(category='spot')
                    if path.endswith('/kline'):params.update(symbol='BTCUSDT',interval='15',limit=25,end=1000)
                    client.request(path,params,timeout=12)
                    (cwd/'receipt.json').write_text(json.dumps(record));os._exit(0)
                except BaseException as error:
                    (cwd/'failure.txt').write_text(type(error).__name__+':'+str(error));os._exit(3)
            children.append(pid)
        statuses=[os.waitstatus_to_exitcode(os.waitpid(pid,0)[1]) for pid in children]
        self.assertEqual(statuses,[0]*6,[p.read_text() for p in Path(self.temp.name).glob('mixed-*/failure.txt')])
        rows=[json.loads(p.read_text()) for p in Path(self.temp.name).glob('mixed-*/receipt.json')]
        self.assertEqual(sorted(r['path'] for r in rows),sorted(paths))
        starts=sorted(r['start'] for r in rows)
        self.assertTrue(all(b-a>=200 for a,b in zip(starts,starts[1:])))
        active=peak=0
        for _,delta in sorted([(r['start'],1) for r in rows]+[(r['end'],-1) for r in rows]):
            active+=delta;peak=max(peak,active)
        self.assertEqual(peak,4)
        print('PUBLIC_MIXED_BUDGET_ACCEPTANCE '+json.dumps(dict(processes=6,paths=sorted(set(paths)),
              min_spacing_ms=min(b-a for a,b in zip(starts,starts[1:])),peak_inflight=peak)))


if __name__=='__main__':unittest.main()
