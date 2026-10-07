"""Private review receipts preserve venue-native evidence; never formal fills."""
import base64
import copy
import datetime as dt
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from research.hunter_fast_watch import Watch, venue_liquidation
from scripts.hunter_market_stream import archive_state, atomic_state
from test_hunter_fast_watch import book, bybit_event, event, portfolio, position


class DepthReceiptTests(unittest.TestCase):
    def watch(self, venue='BINANCE_SPOT'):
        p = position(venue)
        ledger = portfolio(p)
        w = Watch(venue=venue)
        w.reconcile(ledger, 'source', 100)
        w.connect(100)
        w.event(bybit_event() if venue == 'BYBIT_SPOT' else event(at=100), 100)
        return w, ledger, w.pending.pop('p')

    def receipt(self, venue='BINANCE_SPOT'):
        w, ledger, trigger = self.watch(venue)
        original = copy.deepcopy(ledger)
        native = book(venue='bybit' if venue == 'BYBIT_SPOT' else 'binance')
        before = copy.deepcopy(native)
        self.assertEqual(w.review('p', trigger, native, 100)['status'], 'OBSERVATION_ONLY')
        self.assertEqual(native, before)
        self.assertEqual(ledger, original)
        receipt = [h for h in w.history if h['kind'] == 'OBSERVATION_REVIEW'][-1]['depth_receipt']
        raw = gzip.decompress(base64.b64decode(receipt['payload']))
        self.assertEqual(hashlib.sha256(raw).hexdigest(), receipt['payload_sha256'])
        self.assertEqual(json.loads(raw), native)
        estimate = venue_liquidation(position(venue), json.loads(raw), dt.datetime.fromtimestamp(100, dt.timezone.utc))
        self.assertEqual(estimate, receipt['execution_estimate'])
        self.assertFalse(receipt['formal_execution'])
        self.assertFalse(receipt['historical_execution_verified'])
        return w, receipt

    def test_binance_native_depth_recomputable(self):
        _, r = self.receipt()
        self.assertEqual(r['timestamp_quality'], 'RECEIPT_BOUND')
        self.assertIsNone(r['source_timestamp'])

    def test_bybit_native_depth_recomputable(self):
        _, r = self.receipt('BYBIT_SPOT')
        self.assertEqual(r['timestamp_quality'], 'EXCHANGE_TIMESTAMP')
        self.assertEqual(r['execution_venue'], 'BYBIT_SPOT')

    def test_mixed_venue_rejected_without_receipt(self):
        w, _, trigger = self.watch('BYBIT_SPOT')
        self.assertEqual(w.review('p', trigger, book(), 100)['status'], 'VENUE_MATCH_REQUIRED')
        self.assertFalse(any(h['kind'] == 'OBSERVATION_REVIEW' for h in w.history))

    def test_stale_depth_rejected_without_receipt(self):
        w, _, trigger = self.watch()
        self.assertEqual(w.review('p', trigger, book(at=90), 100)['status'], 'FRESH_FULL_DEPTH_REQUIRED')
        self.assertFalse(any(h['kind'] == 'OBSERVATION_REVIEW' for h in w.history))

    def test_duplicate_review_does_not_duplicate_receipt(self):
        w, _, trigger = self.watch()
        w.review('p', trigger, book(), 100)
        w.review('p', trigger, book(), 100)
        self.assertEqual(sum(h['kind'] == 'OBSERVATION_REVIEW' for h in w.history), 1)

    def test_private_state_and_archive_retain_receipt(self):
        w, receipt = self.receipt()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'private.json'
            state = w.snapshot(100)
            atomic_state(path, state)
            archive_state(path, state)
            restored = json.loads(path.read_text())
            archived = json.loads(gzip.decompress(next(Path(directory).glob('*.gz')).read_bytes()))
            for saved in (restored, archived):
                r = [h for h in saved['history'] if h['kind'] == 'OBSERVATION_REVIEW'][-1]['depth_receipt']
                self.assertEqual(r, receipt)
                self.assertFalse(saved['formal_writer'])
                self.assertEqual(saved['real_order_count'], 0)


if __name__ == '__main__':
    unittest.main()
