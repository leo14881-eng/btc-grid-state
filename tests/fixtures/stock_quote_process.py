"""Subprocess-only offline monitor fixture. Never run a provider or Git writer."""
import argparse
from datetime import datetime, timezone, time
import json
import os
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / 'tests'))
from test_stock_position_collection import Provider
from research.stock_shadow import stock_position_monitor as monitor


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--rows', required=True)
    parser.add_argument('--now', required=True)
    parser.add_argument('--state-root')
    args = parser.parse_args()
    checked = datetime.fromisoformat(args.now)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return checked.astimezone(tz or timezone.utc)
    monitor.datetime = Clock
    monitor.now = lambda: checked.isoformat()
    rows = json.loads(Path(args.rows).read_text())
    provider = Provider(rows)
    monitor.urllib.request.urlopen = provider
    # Explicitly override inherited credentials even though all HTTP is mocked.
    os.environ['APCA_API_KEY_ID'] = 'offline-process-fixture'
    os.environ['APCA_API_SECRET_KEY'] = 'offline-process-fixture'
    if args.state_root:
        root = Path(args.state_root)
        root.mkdir(parents=True, exist_ok=True)
        monitor.ROOT = root
        monitor.STATE = root / 'portfolio-v1.json'
        monitor.EVENTS = root / 'trades-v1.json'
        monitor.HEALTH = root / 'position-monitor-v1.json'
        if not monitor.STATE.exists():
            monitor.save(monitor.STATE, {'positions': {s: {'symbol': s,
                'opened_at': '2026-10-08T15:00:00Z', 'tranches': [{'at': '2026-10-08T15:00:00Z',
                'price': 100, 'notional': 1000, 'reason': 'OFFLINE_FIXTURE'}],
                'mfe_net_pct': 0, 'mae_net_pct': -1} for s in rows}, 'closed': []})
        monitor._alpaca_exchange_session = lambda ts=None: {
            'date': checked.date().isoformat(), 'open': time(9, 30), 'close': time(16)}
        monitor.main()
    else:
        usage = {}
        result = monitor.alpaca_snapshot_quotes(sorted(rows), usage=usage)
        print(json.dumps({'result': result, 'usage': usage}, allow_nan=False))


if __name__ == '__main__':
    main()
