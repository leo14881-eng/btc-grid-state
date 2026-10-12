#!/usr/bin/env bash
set -eu
state=$(systemctl show hunter-binance-stream.service -p ActiveState -p SubState -p MainPID -p ControlGroup -p Result -p ExecMainStatus)
printf '%s\n' "$state"
if ! printf '%s\n' "$state" | grep -Eq '^ActiveState=(failed|inactive)$' || ! printf '%s\n' "$state" | grep -qx 'MainPID=0'; then
  printf '%s\n' 'STOPPED_SERVICE_REQUIRED; no service action taken' >&2
  exit 1
fi
journalctl --utc --no-pager -u hunter-binance-stream.service --since '2026-10-12 17:19:00' --until '2026-10-12 17:22:00' -o short-iso -n 200
runuser -u hunter-binance-stream -- /opt/hunter-binance-stream/venv/bin/python -B - <<'PY'
"""One consistent, read-only snapshot of the stopped Binance collector.

Run with its existing service account and Python -B; no collector imports,
service control, network, token output, SQLite checkpoint, or recovery action.
"""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def inspect(directory):
    raw=(directory/'manifest.json').read_bytes();scan=json.loads(raw)
    symbols=[]
    for base,coin in scan['coins'].items():
        for pair in coin.get('pairs',[]):
            if pair.get('venue')=='binance':
                if pair.get('base')!=base or pair.get('pair')!=base+'USDT':
                    raise ValueError('MANIFEST_PAIR_MISMATCH')
                symbols.append(pair['pair'])
    if (not symbols or len(symbols)!=len(set(symbols)) or 'BTCUSDT' not in symbols
            or any(not isinstance(s,str) or not s.endswith('USDT')
                or not 1<=len(s[:-4])<=30 or not s.isalnum()
                or s.upper()!=s or s.lower().upper()!=s for s in symbols)):
        raise ValueError('MANIFEST_SYMBOLS_INVALID')
    # Match deployed topic()'s Unicode symbol domain, including CJK assets.
    # Exact deployed roster(): lexicographic topics, 500 per socket and 100
    # per SUBSCRIBE. Historical manifest age is evidence, not readiness.
    names=sorted([s.lower()+'@kline_'+i for s in symbols for i in ('15m','1h','1d')]+['btcusdt@kline_5m'])
    # Even mode=ro can update the live WAL shm reader marks. Never open the
    # source with SQLite: capture bytes, then query an isolated temporary copy.
    def capture():
        result={}
        for name in ('state.sqlite3','state.sqlite3-wal','state.sqlite3-shm','manifest.json'):
            path=directory/name
            if not path.exists():
                if name in ('state.sqlite3','manifest.json'):raise ValueError('SOURCE_FILE_MISSING')
                continue
            if path.is_symlink() or not path.is_file() or path.stat().st_size>256*1024*1024:
                raise ValueError('SOURCE_FILE_UNSAFE_OR_TOO_LARGE')
            result[name]=path.read_bytes()
        return result
    source=capture()
    if source.get('manifest.json')!=raw or capture()!=source:
        raise ValueError('SOURCE_CHANGED_DURING_CAPTURE')
    with tempfile.TemporaryDirectory(prefix='binance-readonly-snapshot-') as scratch:
        snapshot=Path(scratch)
        for name in ('state.sqlite3','state.sqlite3-wal'):
            if name in source:(snapshot/name).write_bytes(source[name])
        db=sqlite3.connect((snapshot/'state.sqlite3').as_uri()+'?mode=ro',uri=True,timeout=1)
        db.row_factory=sqlite3.Row
        try:
            db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
            meta=dict(db.execute("SELECT key,value FROM meta WHERE key IN ('epoch','connected','scope_hash','roster_hash')"))
            rows=[dict(r) for r in db.execute('SELECT topic,epoch,ws_rx,ws_ts FROM topics')]
            owners={name:db.execute('SELECT COUNT(*) FROM '+name).fetchone()[0] for name in ('requests','leases')}
        finally:db.close()
    if capture()!=source:raise ValueError('SOURCE_CHANGED_DURING_DIAGNOSTIC')
    if (meta.get('scope_hash')!=digest(scan) or meta.get('roster_hash')!=digest(names)
            or sorted(r['topic'] for r in rows)!=names or any(r['epoch']!=meta.get('epoch') for r in rows)):
        raise ValueError('SNAPSHOT_MANIFEST_OR_EPOCH_MISMATCH')
    rows={r['topic']:r for r in rows};groups=defaultdict(list);shards=defaultdict(list)
    for ordinal,name in enumerate(names):
        row=rows[name]
        if any(type(row[k]) is not int or row[k]<0 for k in ('ws_rx','ws_ts')):
            raise ValueError('INVALID_PERSISTED_TIMESTAMP')
        symbol,interval=name.split('@kline_')
        row.update(ordinal=ordinal,shard=ordinal//500,subscribe_batch=(ordinal%500)//100,interval=interval)
        groups[symbol].append(row);shards[row['shard']].append(row)
    summary=[]
    for shard,items in sorted(shards.items()):
        seen=[r for r in items if r['ws_rx']>0]
        summary.append(dict(shard=shard,total=len(items),no_frame=sum(r['ws_rx']==0 for r in items),
            missing_intervals=dict(Counter(r['interval'] for r in items if r['ws_rx']==0)),
            first_topic=items[0]['topic'],last_topic=items[-1]['topic'],
            min_rx=min((r['ws_rx'] for r in seen),default=None),max_rx=max((r['ws_rx'] for r in seen),default=None)))
    fields=('topic','ordinal','shard','subscribe_batch','ws_rx','ws_ts')
    affected=[dict(symbol=symbol,intervals=[{k:r[k] for k in fields} for r in items])
        for symbol,items in sorted(groups.items()) if any(r['ws_rx']==0 for r in items)]
    buckets=Counter((r['ws_rx']//10000*10000,r['ws_ts']//10000*10000) for r in rows.values())
    return dict(source_read_only=True,query_target='PRIVATE_TEMP_COPY_REMOVED_ON_EXIT',
        source_sha256={name:hashlib.sha256(data).hexdigest() for name,data in source.items()},
        epoch=meta['epoch'],connected=meta.get('connected'),owners=owners,
        manifest_sha256=hashlib.sha256(raw).hexdigest(),scope_hash=meta['scope_hash'],roster_hash=meta['roster_hash'],
        manifest_as_of=scan.get('as_of_utc'),topics=len(names),no_frame=sum(r['ws_rx']==0 for r in rows.values()),
        index_base=0,shards=summary,affected_symbols=affected,
        ws_10s_buckets=[dict(rx_ms=rx,ts_ms=ts,n=n) for (rx,ts),n in sorted(buckets.items())],
        interpretation='Persisted receipt only; no first-stale cause or request outcome inferred.')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir',type=Path,default=Path('/var/lib/hunter-binance-stream'))
    args=parser.parse_args()
    print(json.dumps(inspect(args.state_dir),ensure_ascii=False,separators=(',',':')))

PY
