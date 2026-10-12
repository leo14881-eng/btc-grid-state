"""Local-only provisioning, roster capture and read-only acceptance; never a job runner."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re

from research.hunter_bybit_stream import plan, roster
from research.hunter_bybit_stream_store import STATE_PATH, Store, content_hash, roster_hash


def prepare(snapshot, source_head, now):
    if not re.fullmatch('[0-9a-f]{40}', source_head):
        raise ValueError('PINNED_READBACK_HEAD_REQUIRED')
    topics = roster(snapshot, now)
    receipt = dict(schema='hunter_bybit_roster_receipt_v1', source_head_sha=source_head,
                   captured_at_utc=now.isoformat(), snapshot_sha256=snapshot['snapshot_sha256'],
                   snapshot_source=snapshot['source'], roster_hash=roster_hash(topics), topics=topics,
                   shadow_only=True, real_trading_enabled=False, capital_authority='NONE_SHADOW_ONLY')
    receipt['receipt_sha256'] = content_hash(receipt)
    return receipt


def acceptance(snapshot, reader, now, clock=lambda:dt.datetime.now(dt.timezone.utc)):
    topics = roster(snapshot, now)
    end = int(now.timestamp()*1000)
    generation = reader.freeze(topics, end, end)
    finished = int(clock().timestamp()*1000)
    if not 0 <= finished-end <= 90_000:
        generation = dict(generation, status='UNKNOWN', windows={},
                          reason='CACHE_READ_FINISHED_STALE_OR_CLOCK_ROLLBACK')
    return dict(schema='hunter_bybit_acceptance_v1', checked_at_utc=now.isoformat(),
                expected_topics=len(topics), roster_hash=roster_hash(topics),
                status=generation['status'], reason=generation.get('reason'),
                failed_topic=generation.get('failed_topic'),
                manifest_sha256=generation.get('manifest_sha256'), epoch=generation.get('epoch'),
                generation_sha256=generation.get('generation_sha256'),
                windows=len(generation.get('windows', {})),
                read_finished_ms=finished, generation_end_ms=end,
                # Not a claim that a Discovery publication has switched sources.
                publication_verified=False, shadow_only=True, real_trading_enabled=False,
                capital_authority='NONE_SHADOW_ONLY')


def atomic_json(path, value):
    temp = path.with_suffix('.tmp')
    with temp.open('x', encoding='utf-8') as handle:
        json.dump(value, handle, sort_keys=True, separators=(',', ':'), allow_nan=False)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    temp.replace(path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('initialize', 'prepare', 'check'))
    parser.add_argument('--snapshot', type=Path)
    parser.add_argument('--source-head')
    args = parser.parse_args()
    if args.action == 'check':
        if args.snapshot is None:
            parser.error('--snapshot is required (fresh authoritative readback)')
        snapshot = json.loads(args.snapshot.read_text(encoding='utf-8'))
        report = acceptance(snapshot, Store.reader(), dt.datetime.now(dt.timezone.utc))
        print(json.dumps(report, sort_keys=True))
        return 0 if report['status'] == 'READY' else 1
    import fcntl
    # Provisioning and manifest changes cannot race the live collector.
    with (STATE_PATH.parent/'collector.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.action == 'initialize':
            if STATE_PATH.exists():
                raise ValueError('EXISTING_STATE_PRESERVED_MANUAL_REVIEW_REQUIRED')
            Store()
            print(json.dumps(dict(status='INITIALIZED_NOT_STARTED', path=str(STATE_PATH))))
            return 0
        if args.snapshot is None or args.source_head is None:
            parser.error('prepare requires --snapshot and --source-head')
        snapshot = json.loads(args.snapshot.read_text(encoding='utf-8'))
        receipt = prepare(snapshot, args.source_head, dt.datetime.now(dt.timezone.utc))
        archive = STATE_PATH.parent/'manifests'
        archive.mkdir(exist_ok=True)
        archived = archive/(receipt['receipt_sha256']+'.json')
        if not archived.exists():
            atomic_json(archived, dict(receipt=receipt, snapshot=snapshot))
        # Store both source and complete snapshot for later manifest/source audit.
        atomic_json(STATE_PATH.parent/'manifest.json', snapshot)
        print(json.dumps(dict(status='PREPARED_NOT_STARTED', receipt=receipt, plan=plan(receipt['topics'])), sort_keys=True))
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
