"""Actual OS processes, different working directories, one durable governor."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from research.hunter_bybit_stream_store import Store, Budget


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
                    time.sleep(.85)
                    released=time.time_ns()//1_000_000
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


if __name__=='__main__':unittest.main()
