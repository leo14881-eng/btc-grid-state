"""Read-only health acceptance. Never starts/stops services, edits guards or publishes."""
import argparse
import datetime as dt
import json
import re
from pathlib import Path
import time

from research.hunter_binance_stream_store import Budget, FRESH_MS, STATE_PATH, roster, roster_hash, content_hash
from research.hunter_binance_health_store import Store, CONTRACT, valid_proof
from research.hunter_binance_stream import SHARD_SIZE
from research.hunter_binance_stream_consumer import Frozen
from research.hunter_bybit_stream_store import budget_state,request_gate


def read_sample(store,names,manifest,head,now,clock=None):
    result=dict(contract=CONTRACT,collector_accepted=False,service_status='UNKNOWN',
                data_status='UNKNOWN',publication_verified=False,shadow_only=True,
                real_trading_enabled=False,capital_authority='NONE_SHADOW_ONLY')
    try:
        meta=store.metadata();value=store.read_health();budget=Budget(store).status()
        if not re.fullmatch('[0-9a-f]{40}',head):raise ValueError('HEALTH_SOURCE_HEAD_REQUIRED')
        if not isinstance(value,dict):raise ValueError('HEALTH_SAMPLE_MISSING')
        expected=dict(contract=CONTRACT,epoch=meta['epoch'],roster_hash=roster_hash(names),
                      manifest_sha256=manifest,source_head=head,topics=len(names),
                      shadow_only=True,real_trading_enabled=False,capital_authority='NONE_SHADOW_ONLY',
                      publication_verified=False)
        if any(value.get(k)!=v for k,v in expected.items()):raise ValueError('HEALTH_BINDING_MISMATCH')
        if (meta.get('scope_hash')!=manifest or meta.get('roster_hash')!=expected['roster_hash']
                or meta.get('health_epoch')!=meta['epoch'] or meta.get('connected')!='1'
                or meta.get('health_phase')!='ACTIVE'):
            raise ValueError('HEALTH_CONNECTION_MISMATCH')
        if budget['halt'] or now<budget['cooldown_ms']:raise ValueError('HEALTH_GLOBAL_STOP')
        if (value.get('service_status') not in ('HEALTHY','DEGRADED') or value.get('all_ack') is not True
                or value.get('halt') or value.get('cooldown_active') is not False
                or type(value.get('observed_at')) is not int or not 0<=now-value['observed_at']<=30000
                or type(value.get('queued')) is not int or not 0<=value['queued']<=4096
                or type(value.get('active_repairs')) is not int or not 0<=value['active_repairs']<=4
                or type(value.get('oldest_active_ms')) is not int or not 0<=value['oldest_active_ms']<=45000
                or type(value.get('queue_progress_at')) is not int
                or not 0<=now-value['queue_progress_at']<=30000):
            raise ValueError('HEALTH_SAMPLE_UNQUALIFIED')
        states=store.states()
        if set(s['topic'] for s in states)!=set(names) or any(not s['ack'] or s['epoch']!=meta['epoch'] for s in states):
            raise ValueError('HEALTH_ACK_SCOPE_MISMATCH')
        groups=[names[i:i+SHARD_SIZE] for i in range(0,len(names),SHARD_SIZE)]
        if value.get('shard_topics')!=[content_hash(sorted(group)) for group in groups]:
            raise ValueError('HEALTH_SHARD_SCOPE_MISMATCH')
        shards=value.get('shards')
        if not isinstance(shards,list) or len(shards)!=len(groups):raise ValueError('HEALTH_SHARDS_MISSING')
        for index,s in enumerate(shards):
            if (s.get('shard')!=index or s.get('evidence') not in ('MATCHING_PONG','VALIDATED_MARKET_FRAME')
                    or type(s.get('observed_at')) is not int
                    or not int(meta['health_epoch_started'])<=s['observed_at']<=value['observed_at']
                    or not 0<=now-s['observed_at']<=FRESH_MS):raise ValueError('HEALTH_SHARD_EXPIRED')
        frozen=store.freeze(names,now,now,expected['roster_hash'])
        if any(frozen.get(k)!=expected[k] for k in ('epoch','roster_hash','manifest_sha256')):
            raise ValueError('HEALTH_FREEZE_BINDING_CHANGED')
        # Finish fence catches reconnect, shutdown and stop changes during the read.
        with store.db(readonly=True) as db:
            final_meta=dict(db.execute('SELECT key,value FROM meta'))
            current=json.loads(db.execute('SELECT payload FROM metadata WHERE kind=?',(CONTRACT,)).fetchone()[0])
            if (any(final_meta.get(k)!=meta.get(k) for k in ('epoch','health_epoch','scope_hash','roster_hash','health_epoch_started'))
                    or final_meta.get('connected')!='1' or final_meta.get('health_phase')!='ACTIVE'
                    or current.get('epoch')!=meta['epoch'] or current.get('service_status') not in ('HEALTHY','DEGRADED')):
                raise ValueError('HEALTH_READ_CHANGED_OR_EXPIRED')
            connections={r[0]:json.loads(r[1]) for r in db.execute("SELECT topic,payload FROM bn_validity WHERE kind='connection'")}
            final_budget=budget_state(db)
            final_budget['halt']=final_budget['halt'] or request_gate(store,db)
        finished=clock() if clock is not None else now
        if (not now<=finished<=value['observed_at']+30000
                or finished-value['queue_progress_at']>30000
                or value['oldest_active_ms']+finished-value['observed_at']>45000):
            raise ValueError('HEALTH_READ_CHANGED_OR_EXPIRED')
        if (set(connections)!=set(names)
                or any(not valid_proof(connections[n],'connection',n,final_meta,finished) for n in names)
                or any(not 0<=finished-s['observed_at']<=FRESH_MS for s in shards)):
            raise ValueError('HEALTH_CONNECTION_PROOF_INVALID')
        if final_budget['halt'] or finished<final_budget['cooldown_ms']:raise ValueError('HEALTH_GLOBAL_STOP')
        if frozen['status']=='READY':
            try:Frozen(frozen,checked_at=finished)
            except ValueError:frozen=dict(frozen,status='UNKNOWN',reason='BINANCE_DATA_EXPIRED_DURING_HEALTH_READ')
        result.update(value,collector_accepted=False,data_status=frozen['status'],
                      data_reason=frozen.get('reason'),data_failed_topic=frozen.get('failed_topic'),
                      generation_end_ms=now,publication_verified=False,health_verified=True,health_verified_at=finished)
        if frozen['status']!='READY':result['service_status']='DEGRADED'
        return result
    except Exception as error:
        return dict(result,reason=str(error) if isinstance(error,ValueError) else 'HEALTH_READ_FAILED')


class Window:
    """300 seconds of advancing, bound health samples; replay never accumulates time."""
    def __init__(self):self.start=None;self.start_steady=None;self.last=None;self.last_steady=None;self.binding=None

    def observe(self,value,steady):
        stamp=value.get('observed_at')
        binding=tuple(value.get(k) for k in ('epoch','roster_hash','manifest_sha256','source_head'))
        good=(value.get('service_status') in ('HEALTHY','DEGRADED') and value.get('contract')==CONTRACT
              and value.get('health_verified') is True)
        if (not good or type(stamp) is not int or (self.last_steady is not None
                and not 0<=steady-self.last_steady<=30) or (self.last is not None and
                (stamp<self.last or stamp-self.last>30000)) or binding!=self.binding):
            self.start=self.start_steady=self.last=None
        self.binding=binding;self.last_steady=steady
        if not good or type(stamp) is not int:return dict(value,collector_accepted=False,healthy_seconds=0)
        if self.start is None:self.start=stamp;self.start_steady=steady
        if self.last is not None and stamp==self.last:
            return dict(value,collector_accepted=False,healthy_seconds=(stamp-self.start)/1000)
        self.last=stamp;seconds=min((stamp-self.start)/1000,steady-self.start_steady)
        return dict(value,collector_accepted=seconds>=300,healthy_seconds=seconds,
                    publication_verified=False)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--source-head',required=True)
    parser.add_argument('--state',type=Path,default=STATE_PATH)
    parser.add_argument('--watch',action='store_true')
    args=parser.parse_args()
    snapshot=json.loads(args.manifest.read_text(encoding='utf-8'))
    captured=dt.datetime.fromisoformat(snapshot['as_of_utc'].replace('Z','+00:00'))
    names=roster(snapshot,captured);manifest=content_hash(snapshot);store=Store.reader(args.state)
    window=Window();deadline=time.monotonic()+900
    while True:
        now=time.time_ns()//1_000_000
        result=window.observe(read_sample(store,names,manifest,args.source_head,now,clock=lambda:time.time_ns()//1_000_000),time.monotonic())
        print(json.dumps(result,sort_keys=True),flush=True)
        if result['collector_accepted']:return 0
        if not args.watch or time.monotonic()>=deadline:return 1
        time.sleep(10)


if __name__=='__main__':raise SystemExit(main())
