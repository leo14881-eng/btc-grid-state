"""Binance market/control boundary. No network, strategy, or trading ledger writes."""
import contextlib
import json
import sqlite3
import threading
import time
from pathlib import Path

HEALTH_CONTROL_CONTRACT = 'binance_health_control_v2'
CONTROL_CONTRACT = 'binance_control_v1'


class MarketControlMixin:
    """Keep HTTP ownership independent of the market database's writer lock."""

    def _local(self):
        # Instances are prepared before worker threads are admitted.
        if not hasattr(self, '_market_local'):
            self._market_local = threading.local()
            self._market_metrics_lock = threading.Lock()
            self._market_metrics = {}
        return self._market_local

    def _measure(self, kind, phase, started, error=None):
        elapsed = (time.perf_counter_ns()-started)//1000
        code = getattr(error, 'sqlite_errorcode', None)
        name = getattr(error, 'sqlite_errorname', type(error).__name__ if error else None)
        with self._market_metrics_lock:
            item = self._market_metrics.setdefault(kind+':'+phase,
                dict(count=0,max_us=0,total_us=0,errors={}))
            item['count'] += 1
            item['max_us'] = max(item['max_us'],elapsed)
            item['total_us'] += elapsed
            if error:
                key = str(code)+':'+str(name)
                item['errors'][key] = item['errors'].get(key,0)+1

    @contextlib.contextmanager
    def db(self, readonly=False, timeout=10, *, write_kind='market'):
        local = self._local()
        current = getattr(local,'connection',None)
        if current is not None:
            # Only apply_intent owns this transaction. Nested helpers do not
            # commit independently of the publication receipt.
            yield current
            return
        existing = readonly or self._existing
        address = self.path.as_uri()+('?mode=ro' if readonly else '?mode=rw') if existing else str(self.path)
        if getattr(self,'control',None) is not None:
            timeout = min(timeout,.25)
        kind = 'read' if readonly else write_kind
        started = time.perf_counter_ns()
        connection = None
        phase = 'connect'
        try:
            connection = sqlite3.connect(address,uri=existing,timeout=timeout)
            connection.row_factory = sqlite3.Row
            self._measure(kind,phase,started)
            phase = 'begin'; started = time.perf_counter_ns()
            connection.execute('BEGIN' if readonly else 'BEGIN IMMEDIATE')
            self._measure(kind,phase,started)
            phase = 'body'; started = time.perf_counter_ns()
            yield connection
            self._measure(kind,phase,started)
            phase = 'commit'; started = time.perf_counter_ns()
            connection.commit()
            self._measure(kind,phase,started)
        except BaseException as error:
            self._measure(kind,phase,started,error)
            if connection is not None:
                rollback = time.perf_counter_ns()
                try:
                    connection.rollback()
                except BaseException as rollback_error:
                    self._measure(kind,'rollback',rollback,rollback_error)
                    raise
                else:
                    self._measure(kind,'rollback',rollback)
            raise
        finally:
            if connection is not None:
                connection.close()

    def transaction_diagnostics(self):
        self._local()
        with self._market_metrics_lock:
            result = json.loads(json.dumps(self._market_metrics))
        wal = Path(str(self.path)+'-wal')
        result['wal_bytes'] = wal.stat().st_size if wal.exists() else 0
        return result

    def enable_control(self, path=None, *, readonly=False):
        from research.hunter_binance_control import Control
        target = Path(path) if path else self.path.with_name('control.sqlite3')
        if target.resolve() == self.path.resolve():
            raise ValueError('BINANCE_CONTROL_MUST_BE_SEPARATE')
        self._local()
        self.control = Control.reader(target) if readonly else Control(target)
        return self.control

    def set_control_source(self, source_head):
        import re
        if not re.fullmatch('[a-f0-9]{40}',source_head):
            raise ValueError('BINANCE_CONTROL_SOURCE_REQUIRED')
        self._control_source_head = source_head

    def _control_for_meta(self, meta):
        required = meta.get('health_contract') == HEALTH_CONTROL_CONTRACT
        control = getattr(self,'control',None)
        if required and control is None:
            control = self.enable_control(readonly=True)
        return control if required else None

    def capture_fence(self, names):
        from research.hunter_binance_stream_store import roster_hash
        roster_hash(names)
        with self.db(readonly=True) as db:
            meta = dict(db.execute('SELECT key,value FROM meta'))
        control = self._control_for_meta(meta)
        token = control.fence(names,epoch=meta.get('epoch')) if control else None
        return dict(epoch=meta.get('epoch'),manifest_sha256=meta.get('scope_hash'),
                    roster_hash=meta.get('roster_hash'),health_contract=meta.get('health_contract'),
                    control=token)

    def finish_read(self, generation, names, checked_at, fence):
        from research.hunter_binance_stream_store import FRESH_MS, budget_state, request_gate
        if not fence or not 0 <= checked_at-generation['generation_end_ms'] <= FRESH_MS:
            raise ValueError('BINANCE_FINAL_READ_STALE')
        # This is a NEW market read transaction, never the freeze transaction.
        with self.db(readonly=True) as db:
            meta = dict(db.execute('SELECT key,value FROM meta'))
            requested=set(names)
            current={r['topic']:dict(r) for r in db.execute('SELECT * FROM topics') if r['topic'] in requested}
            if (set(current)!=requested or any(not row['ack'] or row['gap']
                    or row['epoch']!=generation.get('epoch') for row in current.values())):
                raise ValueError('BINANCE_FINAL_TOPIC_REVOKED')
            if meta.get('health_contract'):
                unresolved={r[0] for r in db.execute('SELECT DISTINCT topic FROM bn_unprocessed WHERE resolved_at IS NULL')}
                reverify={r[0].split(':',1)[1] for r in db.execute("SELECT kind FROM metadata WHERE kind LIKE 'quiet_reverify:%'")}
                if requested & (unresolved|reverify):
                    raise ValueError('BINANCE_FINAL_TOPIC_REVOKED')
            if meta.get('health_contract')!=HEALTH_CONTROL_CONTRACT:
                budget=budget_state(db)
                if budget['halt'] or request_gate(self,db) or checked_at<budget['cooldown_ms']:
                    raise ValueError('BINANCE_FINAL_BUDGET_REVOKED')
        expected = dict(epoch=meta.get('epoch'),manifest_sha256=meta.get('scope_hash'),
                        roster_hash=meta.get('roster_hash'),health_contract=meta.get('health_contract'))
        if (any(fence.get(k)!=v or generation.get(k)!=v for k,v in expected.items())
                or meta.get('connected')!='1'
                or (meta.get('health_contract') and meta.get('health_phase')!='ACTIVE')):
            raise ValueError('BINANCE_FINAL_READ_REVOKED')
        if fence.get('generation_sha256')!=generation.get('generation_sha256'):
            raise ValueError('BINANCE_FINAL_GENERATION_HASH')
        control = self._control_for_meta(meta)
        if control:
            if not fence.get('control'):
                raise ValueError('BINANCE_CONTROL_FENCE_REQUIRED')
            control.validate_fence(fence['control'],names,epoch=meta['epoch'])
        elif fence.get('control'):
            raise ValueError('BINANCE_CONTROL_CONTRACT_CHANGED')
        return True

    def scheduler_snapshot(self):
        with self.db(readonly=True) as db:
            states = {r['topic']:dict(r) for r in db.execute('SELECT * FROM topics')}
            latest = dict(db.execute('SELECT topic,MAX(start) FROM bars GROUP BY topic'))
            unresolved = {r[0] for r in db.execute('SELECT DISTINCT topic FROM bn_unprocessed WHERE resolved_at IS NULL')}
            reverify = {r[0].split(':',1)[1] for r in db.execute(
                "SELECT kind FROM metadata WHERE kind LIKE 'quiet_reverify:%'")}
        for name,value in states.items():
            value.update(latest=latest.get(name),unresolved=name in unresolved,reverify=name in reverify)
        return states

    def apply_work(self, kind, epoch, names, body, requested, received, *, job_key, recovery_id=None):
        from research.hunter_binance_stream_store import content_hash
        control = getattr(self,'control',None)
        if control is None:
            raise ValueError('BINANCE_CONTROL_REQUIRED_FOR_PUBLICATION')
        envelope = dict(kind=kind,names=list(names),body=body,requested=requested,
                        received=received,job_key=job_key)
        identity = control.publication_intent(epoch,job_key,list(names),content_hash(envelope),
                                              payload=envelope,recovery_id=recovery_id)
        return self.apply_intent(control.get_intent(identity))

    def revoke_work(self, kind, epoch, names, reason, now, *, job_key):
        """Revoke the whole dependency batch before storing failure evidence.

        Transport failure has no market body to publish. Its durable failed
        intent still fences previously frozen readers until the same job and
        scope produce a newly validated publication.
        """
        from research.hunter_binance_stream_store import content_hash
        value=dict(kind='failure',failed_kind=kind,names=list(names),reason=str(reason),
                   observed_at=now,job_key=job_key)
        identity=self.control.publication_intent(epoch,job_key,list(names),content_hash(value),payload=value)
        self.control.reject_publication(identity,str(reason))
        return identity

    def apply_intent(self, intent):
        control = getattr(self,'control',None)
        if control is None:
            raise ValueError('BINANCE_CONTROL_REQUIRED_FOR_PUBLICATION')
        try:
            receipt = self._apply_intent_market(intent)
        except BaseException as error:
            control.reject_publication(intent['id'],type(error).__name__)
            raise
        control.reconcile(intent['id'],receipt,supersede_failed=True)
        return receipt

    def _apply_intent_market(self, intent):
        from research.hunter_binance_stream_store import content_hash
        value = intent['payload']
        if (not isinstance(value,dict) or content_hash(value)!=intent['payload_hash']
                or content_hash(sorted(value['names']))!=intent['scope_hash']):
            raise ValueError('BINANCE_PUBLICATION_INTENT_MISMATCH')
        names = value['names']; kind = value['kind']; epoch = intent['epoch']
        if kind not in ('directory','seed','repair','quiet'):
            raise ValueError('BINANCE_PUBLICATION_KIND')
        if kind!='directory' and len(names)!=1:
            raise ValueError('BINANCE_PUBLICATION_TOPIC_SCOPE')
        receipt = dict(intent_id=intent['id'],epoch=epoch,payload_hash=intent['payload_hash'],
                       scope_hash=intent['scope_hash'])
        outcome = 'VALID'
        with self.db(write_kind='publication:'+kind) as db:
            self.check_epoch(db,epoch)
            prior = db.execute('SELECT * FROM bn_publications WHERE intent_id=?',(intent['id'],)).fetchone()
            if prior:
                if {key:prior[key] for key in receipt}!=receipt:
                    raise ValueError('BINANCE_PUBLICATION_RECEIPT_MISMATCH')
                if prior['outcome']!='VALID':
                    raise ValueError('BINANCE_PUBLICATION_REJECTED_RECEIPT')
                return receipt
            local = self._local(); local.connection = db
            try:
                args = (epoch,names,value['body'],value['requested'],value['received'])
                if kind=='directory':
                    self.instrument_batch(*args)
                    if any(self.get_proof('instrument',name).get('result')!='VALID' for name in names):
                        outcome = 'UNKNOWN'
                elif kind=='quiet':
                    self.verify_quiet(epoch,names[0],value['body'],value['requested'],value['received'])
                    proof = self.get_proof('quiet',names[0])
                    if proof.get('result')!='VALID':
                        outcome = 'UNKNOWN'
                else:
                    self.ingest_rest(epoch,names[0],value['body'],value['requested'],value['received'])
                db.execute('INSERT INTO bn_publications VALUES(?,?,?,?,?)',(*receipt.values(),outcome))
            finally:
                local.connection = None
        # Corrective bars, the reverification latch and INVALID proofs must
        # survive. Their receipt can never release the control publication gate,
        # including after a crash between these two databases.
        if outcome!='VALID':
            raise ValueError('BINANCE_PUBLICATION_NOT_VALID')
        return receipt
