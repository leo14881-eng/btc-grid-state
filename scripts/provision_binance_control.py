"""Offline, explicit control-ledger provisioning. Never clears legacy ownership."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from research.hunter_binance_control import Control


def provision(state):
    if sys.platform!='linux':
        raise ValueError('CONTROL_PROVISION_REQUIRES_LINUX')
    if not state.is_absolute() or not state.is_file():
        raise ValueError('EXISTING_ABSOLUTE_STATE_REQUIRED')
    import fcntl
    # Existing lock only: never invent a new inode alongside a live collector.
    with state.with_name('collector.lock').open('r+b') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with sqlite3.connect(state.as_uri()+'?mode=ro',uri=True,timeout=.25) as db:
            meta=dict(db.execute('SELECT key,value FROM meta'))
        if meta.get('connected')=='1':
            raise ValueError('OFFLINE_STATE_CONFIRMATION_REQUIRED')
        control=Control.provision(state.with_name('control.sqlite3'),state)
        return dict(status='PROVISIONED_OFFLINE',control=str(control.path),
                    legacy_state=str(state),boot_bound=True,enabled=False,
                    shadow_only=True,real_trading_enabled=False,
                    capital_authority='NONE_SHADOW_ONLY')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(provision(args.state),sort_keys=True))
