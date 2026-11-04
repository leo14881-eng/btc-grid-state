"""Local-only provisioning, roster capture and read-only acceptance; never a job runner."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import sqlite3

from research.hunter_binance_stream import plan, roster
from research.hunter_binance_stream_store import STATE_PATH, Store, content_hash, roster_hash, topic, split_topic
from research import hunter_binance_stream_consumer as consumer


def prepare(snapshot, source_head, now):
    if not re.fullmatch('[0-9a-f]{40}', source_head):
        raise ValueError('PINNED_READBACK_HEAD_REQUIRED')
    topics = roster(snapshot, now)
    receipt = dict(schema='hunter_binance_roster_receipt_v1', source_head_sha=source_head,
                   captured_at_utc=now.isoformat(), snapshot_sha256=content_hash(snapshot),
                   snapshot_source='CAPTURED_BINANCE_CEX_SCAN', roster_hash=roster_hash(topics), topics=topics,
                   shadow_only=True, real_trading_enabled=False, capital_authority='NONE_SHADOW_ONLY')
    receipt['receipt_sha256'] = content_hash(receipt)
    return receipt


def required_scope(scope,now,*,snapshot=None,selection=None):
    """Derive the original consumer set without executing any job or publisher.

    Monitor selection is {'states': [V1, V2], 'cursor': original_cursor}.
    Daily selection is {'previous': prior_research_report, 'early': early_report}.
    Both call the production selection function; explicit lists cannot quietly
    replace an unavailable member of the required set.
    """
    result=dict(scope=scope,input_sha256={})
    if scope in ('early','collector_inventory'):
        all_topics=roster(snapshot,now)
        names=all_topics if scope=='collector_inventory' else [n for n in all_topics if split_topic(n)[1] in ('15m','1h')]
        result.update(expected_roster=roster_hash(all_topics),input_sha256={'snapshot':content_hash(snapshot)})
    elif scope=='monitor':
        from research.hunter_position_monitor import management_selection,C
        if (not isinstance(selection,dict) or not isinstance(selection.get('states'),list)
                or len(selection['states'])!=2 or type(selection.get('cursor')) is not int
                or any(not isinstance(s,dict) or not isinstance(s.get('open_positions'),list)
                       for s in selection['states'])):
            raise ValueError('BINANCE_MONITOR_ORIGINAL_SELECTION_REQUIRED')
        selected,next_cursor=management_selection(selection['states'],selection['cursor'],C['MONITOR_EVIDENCE_BATCH'])
        names=sorted(topic(a+'USDT',i) for a in set(selected)|{'BTC'} for i in ('15m','1h'))
        result.update(selected_assets=selected,next_cursor=next_cursor,
                      selection_budget=C['MONITOR_EVIDENCE_BATCH'],input_sha256={'selection':content_hash(selection)})
    elif scope=='daily':
        from research.hunter_forward_research import select_rotation,finite,ROTATION,TRIGGER_LIMIT
        if (not isinstance(snapshot,dict) or snapshot.get('binance_complete') is not True
                or not snapshot.get('generation_id') or not isinstance(snapshot.get('coins'),dict)
                or not snapshot['coins'] or not isinstance(selection,dict)
                or not isinstance(selection.get('previous'),dict) or not isinstance(selection.get('early'),dict)
                or selection['early'].get('scan_generation_id')!=snapshot['generation_id']):
            raise ValueError('BINANCE_DAILY_ORIGINAL_SELECTION_REQUIRED')
        early=[x.get('base') for x in selection['early'].get('early',[]) if x.get('base')]
        chosen,cursor,_=select_rotation(snapshot['coins'],selection['previous'],early)
        pairs=[];without_binance=[]
        for asset in chosen:
            available=[p for p in snapshot['coins'][asset].get('pairs',[]) if p.get('venue')=='binance']
            if not available:
                without_binance.append(asset);continue
            pair=max(available,key=lambda row:finite(row.get('volume_24h_usdt')) or 0)
            if pair.get('base')!=asset or pair.get('pair')!=asset+'USDT':
                raise ValueError('BINANCE_DAILY_PAIR_IDENTITY_MISMATCH')
            pairs.append(pair['pair'])
        names=sorted({topic(pair,'1d') for pair in pairs})
        result.update(selected_assets=chosen,without_binance_pair=without_binance,next_cursor=cursor,
                      rotation=ROTATION,trigger_limit=TRIGGER_LIMIT,
                      input_sha256={'snapshot':content_hash(snapshot),'selection':content_hash(selection)})
    elif scope=='btc':names=[topic('BTCUSDT','5m')]
    else:raise ValueError('BINANCE_CONSUMER_SCOPE_INVALID')
    if names:roster_hash(names)
    result.update(topics=names,scope_sha256=content_hash(names),expected_topics=len(names))
    return result


def _acceptance(required,reader,now,clock):
    names=required['topics'];end=int(now.timestamp()*1000);frozen=None;generation={}
    report=dict(schema='hunter_binance_consumer_acceptance_v1',checked_at_utc=now.isoformat(),
                **required,status='UNKNOWN',reason=None,windows=0,generation_end_ms=end,
                publication_verified=False,shadow_only=True,real_trading_enabled=False,
                capital_authority='NONE_SHADOW_ONLY')
    if not names:
        return dict(report,status='NOT_REQUIRED',reason='NO_BINANCE_PAIR_IN_ORIGINAL_DAILY_SELECTION',
                    read_finished_ms=int(clock().timestamp()*1000))
    try:
        frozen=consumer.read(names,now,reader=reader,clock=lambda:int(clock().timestamp()*1000),
                             expected_roster=required.get('expected_roster'))
        generation=frozen.generation
        report.update(status='READY',windows=len(generation['windows']),
                      control_fence_sha256=frozen.fence_sha256)
        # Preparing the report must not open a second, weaker approval path.
        # The same bound consumer checks control state and proof expiry again.
        report.update({key:generation.get(key) for key in
            ('roster_hash','manifest_sha256','epoch','generation_sha256','control_contract')})
        report['source_head_sha']=frozen.source_head_sha
        report['read_finished_ms']=frozen.check_delivery()
    except (ValueError,RuntimeError,OSError,sqlite3.Error) as error:
        generation=getattr(error,'generation',generation)
        report.update(status='UNKNOWN',reason=str(error),windows=0,
                      failed_topic=generation.get('failed_topic'),read_finished_ms=int(clock().timestamp()*1000))
        report.update({key:generation.get(key) for key in ('roster_hash','manifest_sha256','epoch','generation_sha256')})
    return report


def consumer_acceptance(scope,reader,now,*,snapshot=None,selection=None,
                        clock=lambda:dt.datetime.now(dt.timezone.utc)):
    return _acceptance(required_scope(scope,now,snapshot=snapshot,selection=selection),reader,now,clock)


def acceptance(snapshot,reader,now,clock=lambda:dt.datetime.now(dt.timezone.utc)):
    """Backward-compatible whole-collector diagnostic, never a consumer gate."""
    return consumer_acceptance('collector_inventory',reader,now,snapshot=snapshot,clock=clock)


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
    parser.add_argument('--scope',choices=('early','monitor','daily','btc','collector_inventory'),default='early',
                        help='check the actual consumer set; collector_inventory is diagnostic only')
    parser.add_argument('--selection',type=Path,help='captured original Monitor states/cursor or Daily previous/early reports')
    args = parser.parse_args()
    if args.action == 'check':
        if args.scope in ('early','daily','collector_inventory') and args.snapshot is None:
            parser.error('--snapshot is required (fresh authoritative readback)')
        if args.scope in ('monitor','daily') and args.selection is None:
            parser.error('--selection is required for the original consumer selection')
        snapshot=json.loads(args.snapshot.read_text(encoding='utf-8')) if args.snapshot else None
        selection=json.loads(args.selection.read_text(encoding='utf-8')) if args.selection else None
        report=consumer_acceptance(args.scope,Store.reader(),dt.datetime.now(dt.timezone.utc),
                                   snapshot=snapshot,selection=selection)
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
