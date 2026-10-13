"""Binance market/control boundary. No network, strategy, or trading ledger writes."""
import contextlib
from bisect import bisect_left
from collections import deque
import json
import math
import os
import sqlite3
import threading
import time
import weakref
from pathlib import Path

HEALTH_CONTROL_CONTRACT = 'binance_health_control_v2'
CONTROL_CONTRACT = 'binance_control_v1'
MARKET_ADMISSION_SECONDS = 2
_WRITERS = weakref.WeakValueDictionary()
_WRITERS_LOCK = threading.Lock()
_LOCAL_INIT_LOCK = threading.Lock()
SQLITE_TIMING_BOUNDS_US = tuple(1 << power for power in range(31))


def _phase_sample(metrics, key, elapsed_ns, error=None, context=None):
    """Pure aggregation; caller owns only its own diagnostics lock."""
    elapsed_ns = max(0,elapsed_ns)
    elapsed = elapsed_ns//1000
    item = metrics.setdefault(key,dict(count=0,max_us=0,total_us=0,errors={},
        histogram=[0]*(len(SQLITE_TIMING_BOUNDS_US)+1)))
    item['count'] += 1
    item['max_us'] = max(item['max_us'],elapsed)
    item['total_us'] += elapsed
    item['histogram'][bisect_left(SQLITE_TIMING_BOUNDS_US,(elapsed_ns+999)//1000)] += 1
    if error is not None:
        # Retain the existing market error-key contract. The class-name fallback
        # here is not an SQLite error name; sqlite_context never makes that claim.
        code = getattr(error,'sqlite_errorcode',None)
        name = getattr(error,'sqlite_errorname',type(error).__name__)
        error_key = str(code)+':'+str(name)
        item['errors'][error_key] = item['errors'].get(error_key,0)+1
    if context is not None:
        item['last_context'] = context
        if error is not None:item['last_error_context'] = context


def _timing_snapshot(metrics, observations):
    result = json.loads(json.dumps(metrics))
    for item in result.values():
        rank = (99*item['count']+99)//100
        cumulative = 0
        item['p99_upper_bound_us'] = 'unknown'
        for index,count in enumerate(item['histogram']):
            cumulative += count
            if cumulative >= rank:
                if index < len(SQLITE_TIMING_BOUNDS_US):
                    item['p99_upper_bound_us'] = SQLITE_TIMING_BOUNDS_US[index]
                break
    result['_timing'] = dict(scope='PER_INSTANCE_LIFETIME',unit='microseconds',
        sample_resolution_us=1,totals_rounding='FLOOR',histogram_rounding='CEILING',
        histogram_upper_bounds_us=list(SQLITE_TIMING_BOUNDS_US)+['overflow'],
        percentile='P99_CEILING_RANK_BUCKET_UPPER_BOUND_NOT_EXACT',
        overflow_percentile='unknown',reset=False)
    result['connection_observations'] = json.loads(json.dumps(observations))
    return result


def _connection_context(connection, context, observation):
    """Read-only probes on this connection; never open a second database."""
    value = dict(context,observation=observation,busy_timeout_ms='unknown',
                 journal_mode='unknown',probe_errors=[])
    if connection is None:
        value['observation'] = 'NO_CONNECTION'
        return value
    for pragma,field in (('busy_timeout','busy_timeout_ms'),('journal_mode','journal_mode')):
        try:
            row = connection.execute('PRAGMA '+pragma).fetchone()
            if row is not None:value[field] = row[0]
        except Exception as error:
            value['probe_errors'].append(dict(pragma=pragma,type=type(error).__name__,
                sqlite_errorcode=getattr(error,'sqlite_errorcode','unknown'),
                sqlite_errorname=getattr(error,'sqlite_errorname','unknown')))
    return value


def _failure_context(connection, error, context, phase):
    value = _connection_context(connection,dict(context,phase=phase),'AFTER_FAILURE_CLEANUP_SAME_CONNECTION')
    value.update(error_type=type(error).__name__,
        sqlite_errorcode=getattr(error,'sqlite_errorcode','unknown'),
        sqlite_errorname=getattr(error,'sqlite_errorname','unknown'))
    try:error.sqlite_context = value
    except Exception:pass  # Diagnostics must not replace the original exception.
    return value


def _safe_measure(measure, *args, **kwargs):
    try:measure(*args,**kwargs)
    except Exception:pass  # Cleanup and its original error outrank diagnostics.


def _record_failure(measure, connection, error, context, kind, phase, started, elapsed_ns):
    try:
        failure = _failure_context(connection,error,context,phase)
        measure(kind,phase,started,error,context=failure,elapsed_ns=elapsed_ns)
    except Exception:pass


class MarketAdmissionTimeout(sqlite3.OperationalError):
    """The process queue expired before any SQLite connection was opened."""


class _WriterAdmission:
    """FIFO for collaborating writers in this process, never an SQLite retry."""
    def __init__(self):
        self.condition = threading.Condition()
        self.waiting = deque()
        self.active = None
        self.thread = None

    def acquire(self, timeout):
        ticket = object()
        deadline = time.monotonic()+timeout
        with self.condition:
            if self.thread == threading.get_ident():
                raise sqlite3.OperationalError('BINANCE_MARKET_WRITER_REENTRY')
            self.waiting.append(ticket)
            self.condition.notify_all()
            waited = False
            try:
                while True:
                    remaining = deadline-time.monotonic()
                    if self.active is None and self.waiting[0] is ticket and (not waited or remaining > 0):
                        self.waiting.popleft()
                        self.active = ticket
                        self.thread = threading.get_ident()
                        return ticket
                    if remaining <= 0:
                        raise MarketAdmissionTimeout('BINANCE_MARKET_ADMISSION_TIMEOUT')
                    waited = True
                    self.condition.wait(remaining)
            except BaseException:
                self.waiting.remove(ticket)
                self.condition.notify_all()
                raise

    def release(self, ticket):
        with self.condition:
            if self.active is not ticket or self.thread != threading.get_ident():
                raise RuntimeError('BINANCE_MARKET_ADMISSION_OWNER_MISMATCH')
            self.active = self.thread = None
            self.condition.notify_all()


def _writer_for(path):
    identity = os.path.normcase(str(Path(path).resolve()))
    with _WRITERS_LOCK:
        writer = _WRITERS.get(identity)
        if writer is None:
            writer = _WriterAdmission()
            _WRITERS[identity] = writer
        return writer


class MarketControlMixin:
    """Keep HTTP ownership independent of the market database's writer lock."""

    def _local(self):
        if not hasattr(self, '_market_local'):
            with _LOCAL_INIT_LOCK:
                if not hasattr(self, '_market_local'):
                    self._market_metrics_lock = threading.Lock()
                    self._market_metrics = {}
                    self._market_connection_observations = {}
                    self._market_writer = _writer_for(self.path)
                    self._market_local = threading.local()
        return self._market_local

    def _measure(self, kind, phase, started, error=None, *, context=None, elapsed_ns=None):
        elapsed_ns = time.perf_counter_ns()-started if elapsed_ns is None else elapsed_ns
        with self._market_metrics_lock:
            _phase_sample(self._market_metrics,kind+':'+phase,elapsed_ns,error,context)

    def _observe_connection(self, connection, kind, context):
        with self._market_metrics_lock:
            if kind in self._market_connection_observations:return
            self._market_connection_observations[kind] = {'observation':'PENDING'}
        started = time.perf_counter_ns()
        value = _connection_context(connection,context,'FIRST_CONNECTION_FOR_KIND')
        with self._market_metrics_lock:
            self._market_connection_observations[kind] = value
        self._measure(kind,'connection_probe',started,context=value)

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
        coordinated = getattr(self,'control',None) is not None
        admission_timeout = None
        if coordinated:
            if not math.isfinite(timeout) or timeout < 0:
                raise ValueError('BINANCE_MARKET_TIMEOUT_INVALID')
            # Separate, explicit queue budget. No SQLite connection, control
            # transaction or HTTP owner is held while waiting for this turn.
            admission_timeout = min(timeout,MARKET_ADMISSION_SECONDS)
            timeout = min(timeout,.25)
        kind = 'read' if readonly else write_kind
        begin = 'BEGIN' if readonly else 'BEGIN IMMEDIATE'
        context = dict(database='market',kind=kind,begin_statement=begin,
                       connect_timeout_seconds=timeout)
        started = time.perf_counter_ns()
        connection = None
        ticket = None
        phase = 'admission_wait' if coordinated and not readonly else 'connect'
        try:
            if coordinated and not readonly:
                ticket = self._market_writer.acquire(admission_timeout)
                self._measure(kind,phase,started,context=context)
                phase = 'connect'; started = time.perf_counter_ns()
            connection = sqlite3.connect(address,uri=existing,timeout=timeout)
            connection.row_factory = sqlite3.Row
            self._measure(kind,phase,started,context=context)
            self._observe_connection(connection,kind,context)
            phase = 'begin'; started = time.perf_counter_ns()
            connection.execute(begin)
            self._measure(kind,phase,started,context=context)
            phase = 'body'; started = time.perf_counter_ns()
            yield connection
            self._measure(kind,phase,started,context=context)
            phase = 'commit'; started = time.perf_counter_ns()
            connection.commit()
            self._measure(kind,phase,started,context=context)
        except BaseException as error:
            elapsed = time.perf_counter_ns()-started
            error.market_phase = phase
            try:
                if connection is not None:
                    rollback = time.perf_counter_ns()
                    try:connection.rollback()
                    except BaseException as rollback_error:
                        rollback_elapsed = time.perf_counter_ns()-rollback
                        _record_failure(self._measure,connection,rollback_error,context,
                                        kind,'rollback',rollback,rollback_elapsed)
                        raise
                    else:_safe_measure(self._measure,kind,'rollback',rollback,context=context)
            finally:
                _record_failure(self._measure,connection,error,context,kind,phase,started,elapsed)
            raise
        finally:
            try:
                if connection is not None:
                    closed = time.perf_counter_ns()
                    try:connection.close()
                    except BaseException as error:
                        elapsed = time.perf_counter_ns()-closed
                        _record_failure(self._measure,connection,error,context,kind,'close',closed,elapsed)
                        raise
                    else:_safe_measure(self._measure,kind,'close',closed,context=context)
            finally:
                if ticket is not None:
                    self._market_writer.release(ticket)

    def transaction_diagnostics(self, *, include_file_state=True):
        self._local()
        with self._market_metrics_lock:
            result = _timing_snapshot(self._market_metrics,self._market_connection_observations)
        if include_file_state:
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
