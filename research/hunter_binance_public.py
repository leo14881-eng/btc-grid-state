"""Default-off, fixed public spot transport under the existing shared budget.

One disposable HTTP process per admitted request; never a second collector.
Supervisor reaps it before releasing the lease. No fallback or credentials.
"""
import base64
import contextlib
import datetime as dt
import json
import math
import os
from pathlib import Path
import selectors
import sqlite3
import subprocess
import sys
import time
import urllib.parse

try:
    from research.hunter_binance_stream_store import Store, Budget
    from research.hunter_binance_http_child import allowed, MAX_BODY
except ModuleNotFoundError as error:
    if error.name != 'research': raise
    from hunter_binance_stream_store import Store, Budget
    from hunter_binance_http_child import allowed, MAX_BODY

# Deliberately shares the proven coordinator and error types, never a DB path.
from research.hunter_bybit_public import (coordinate, DurableStopError,
    UnreapedRequestError, UncertainRequestError, NotSentError)
from research.hunter_binance_control import ControlError, AdmissionClosed

RETIRE_GRACE_SECONDS = 1
CLEANUP_RETRY_SECONDS = 2


class QuarantinedRequestError(RuntimeError):
    """HTTP result discarded; original owner retired; one recovery is queued."""
    def __init__(self, requeue_id):
        super().__init__('BINANCE_RESULT_QUARANTINED_REVERIFY_REQUIRED')
        self.requeue_id = requeue_id


def safe_observer(observer):
    def observe(*args,**kwargs):
        try:
            if observer is not None:observer(*args,**kwargs)
        except Exception:pass  # Evidence failures must never alter admission or mask durable stops.
    return observe


def worker_command(url):
    return [sys.executable, '-B', str(Path(__file__).with_name('hunter_binance_http_child.py')), url, str(os.getpid())]


def exchange(url, deadline, cancelled, on_denial, before_go=lambda:None, *, drain_on_cancel=False, observe=None):
    """Monotonic deadline includes start/DNS/TLS/headers/body; cleanup is awaited.

    kill+wait is a required closure acknowledgement, not a Future timeout.
    An OS process that cannot be reaped keeps its lease and orphan marker.
    """
    allowed(url)
    observe=safe_observer(observe)
    if cancelled() or time.monotonic() >= deadline:
        raise NotSentError('PUBLIC_REQUEST_NOT_SENT')
    env = {key:os.environ[key] for key in ('PATH','LANG','SYSTEMROOT') if key in os.environ}
    process = subprocess.Popen(worker_command(url), stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, close_fds=True)
    data, header, result = bytearray(), None, None
    ready = sent = False
    def consume(chunk, allow_go):
        nonlocal data,header,result,ready,sent
        data.extend(chunk)
        if len(data) > MAX_BODY*2:
            raise ValueError('PUBLIC_IPC_TOO_LARGE')
        while b'\n' in data:
            line, _, rest = data.partition(b'\n'); data = bytearray(rest)
            item = json.loads(line)
            if (item.get('kind') == 'ready' and not ready and not sent
                    and item.get('pid') == process.pid and item.get('parent') == os.getpid()):
                ready = True
                if allow_go and not cancelled() and time.monotonic()<deadline:
                    before_go()  # Recheck the shared denial/owner state.
                    if cancelled() or time.monotonic()>=deadline: continue
                    sent = True  # A failed write is conservatively uncertain.
                    observe('GO')
                    process.stdin.write(b'GO\n'); process.stdin.flush(); process.stdin.close()
            elif item.get('kind') == 'headers' and sent and header is None:
                header = item.get('status')
                if type(header) is not int or not 100 <= header <= 599:
                    raise ValueError('PUBLIC_HTTP_STATUS_INVALID')
                observe('HEADERS',status=header)
                if header in (403,418): on_denial()
                used, window = item.get('weight_used'),item.get('weight_window_ms')
                if used is not None or window is not None:
                    if type(used) is not int or used<0 or type(window) is not int or window%60000:
                        raise ValueError('PUBLIC_HTTP_WEIGHT_HEADER_INVALID')
                    observe('WEIGHT',used=used,window_ms=window)
            elif item.get('kind') == 'response' and sent and result is None and item.get('status') == header:
                payload = base64.b64decode(item['payload'], validate=True)
                retry = item.get('retry')
                if len(payload)>MAX_BODY or type(retry) is not int or not 0 <= retry <= 600:
                    raise ValueError('PUBLIC_HTTP_RESPONSE_INVALID')
                result = (header,payload,retry)
                observe('RESPONSE',status=header)
            elif item.get('kind') == 'error':
                raise UncertainRequestError('PUBLIC_HTTP_OUTCOME_UNKNOWN_MANUAL_REVIEW')
            else:
                raise ValueError('PUBLIC_HTTP_PROTOCOL_INVALID')
    problem = None
    try:
        os.set_blocking(process.stdout.fileno(), False)
        with selectors.DefaultSelector() as poll:
            poll.register(process.stdout, selectors.EVENT_READ)
            eof = False
            while not eof:
                # Drain bytes already delivered, including a late403 header,
                # before observing cancellation or the data deadline.
                chunk = os.read(process.stdout.fileno(), 65536) if poll.select(0) else None
                if chunk == b'': eof = True
                if chunk:
                    consume(chunk, True)
                if (cancelled() and not (sent and drain_on_cancel)) or time.monotonic() >= deadline:
                    break
                if not chunk and not eof:
                    poll.select(min(.02,max(0,deadline-time.monotonic())))
    except BaseException as error:
        problem = error
    finally:
        if process.poll() is None:
            try: process.kill()
            except ProcessLookupError: pass  # Exit can race poll/kill.
        try:
            process.wait(timeout=1)
            observe('REAPED')
        except subprocess.TimeoutExpired as error:
            process.stdout.close()
            raise UnreapedRequestError('PUBLIC_HTTP_PROCESS_NOT_REAPED') from error
        finally:
            if not process.stdin.closed:
                with contextlib.suppress(BrokenPipeError): process.stdin.close()
    try:
        # Socket has actually ended. Drain buffered status before deciding what
        # happened, including a denial delivered at the cancellation boundary.
        while True:
            chunk = os.read(process.stdout.fileno(),65536)
            if not chunk: break
            consume(chunk, False)
    except BaseException as error:
        if not isinstance(problem, DurableStopError): problem = error
    finally:
        process.stdout.close()
    if isinstance(problem, DurableStopError): raise problem
    if not sent: raise NotSentError('PUBLIC_REQUEST_NOT_SENT') from problem
    if problem or data or result is None:
        raise UncertainRequestError('PUBLIC_HTTP_OUTCOME_UNKNOWN_MANUAL_REVIEW') from problem
    return result


@contextlib.contextmanager
def tracked(store, token, deadline, cancelled=lambda:False, *, cleanup_timeout=None, observe=None):
    observe=safe_observer(observe)
    import fcntl
    path = store.path.parent/('request-'+token+'.lock')
    handle = path.open('xb')
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    remove = False
    try:
        def begin():
            with store.db(timeout=max(0,min(.25,deadline-time.monotonic()))) as db:
                db.execute('INSERT INTO requests(token) VALUES(?)',(token,))
        coordinate(begin,deadline,cancelled)
        yield handle.close  # Immediate shared stop if denial persistence fails.
        # Normal exit means the caller proved completion (or no GO). Cleanup
        # may need its own bounded deadline after the data deadline expired.
        # Exceptions from the body still retain the marker and lease unchanged.
        finish_deadline = deadline if cleanup_timeout is None else time.monotonic()+cleanup_timeout
        def finish():
            with store.db(timeout=max(0,min(.25,finish_deadline-time.monotonic()))) as db:
                db.execute('DELETE FROM requests WHERE token=?',(token,))
                db.execute('DELETE FROM leases WHERE token=?',(token,))
        try:
            coordinate(finish,finish_deadline)
            if observe is not None:observe('RETIRED')
        except TimeoutError as error:
            if cleanup_timeout is None: raise
            raise DurableStopError('PUBLIC_REQUEST_CLEANUP_TIMEOUT_MANUAL_REVIEW') from error
        remove = True
        if time.monotonic()>=deadline:
            raise TimeoutError('PUBLIC_RESPONSE_FINISHED_AFTER_DEADLINE')
    finally:
        handle.close()
        if remove: path.unlink()


class Client:
    def __init__(self, store=None, transport=exchange, clock=None):
        # Existing-only: no auto directory/schema/budget provisioning in jobs.
        self.store = store or Store.reader()
        self.budget = Budget(self.store)
        self.transport = transport
        self.now = clock or (lambda:dt.datetime.now(dt.timezone.utc))

    def request(self, path, params=None, *, timeout=30, cancelled=lambda:False,
                drain_on_cancel=False, observe=None, context=None):
        control=getattr(self.store,'control',None)
        if control is not None:
            return self._coordinated_request(control,path,params,timeout=timeout,cancelled=cancelled,
                drain_on_cancel=drain_on_cancel,observe=observe,context=context)
        return self._legacy_request(path,params,timeout=timeout,cancelled=cancelled,
            drain_on_cancel=drain_on_cancel,observe=observe)

    def _retire_controlled(self,control,owner,observe):
        deadline=time.monotonic()+RETIRE_GRACE_SECONDS
        def retire():
            owner.cleanup_attempts+=1
            try:return control.retire(owner)
            except sqlite3.Error as error:
                owner.cleanup_errors.append(dict(phase=getattr(error,'control_phase','UNKNOWN'),
                    sqlite_code=getattr(error,'sqlite_errorcode',None),
                    sqlite_name=getattr(error,'sqlite_errorname',None),message=str(error)))
                owner.cleanup_errors[:]=owner.cleanup_errors[-16:]
                raise
        try:
            coordinate(lambda:control.prepare_retire(owner,deadline),deadline)
        except BaseException as error:
            # Without a durable prearmed deadline readers could not detect a
            # stalled cleanup. This case is a global stop, never local recovery.
            try:control.latch_stop('CONTROL_RETIRE_ARM_UNCONFIRMED')
            except ControlError:pass
            raise DurableStopError('CONTROL_RETIRE_ARM_UNCONFIRMED') from error
        try:
            coordinate(retire,deadline)
        except TimeoutError:
            recovery_deadline=time.monotonic()+CLEANUP_RETRY_SECONDS
            try:
                # The prearmed expired deadline already blocks this scope.
                # Keep the same lock and slot until both deletes commit.
                identity=coordinate(lambda:control.quarantine(owner,'OWNER_RETIRE_CONTENTION'),recovery_deadline)
                coordinate(retire,recovery_deadline)
            except BaseException as error:
                raise DurableStopError('CONTROL_QUARANTINE_RETIRE_UNCONFIRMED') from error
            observe('RETIRED')
            if owner.context.get('requeue_id'):
                try:control.latch_stop('CONTROL_REVERIFY_RETIRE_FAILED')
                except ControlError:pass
                raise DurableStopError('CONTROL_REVERIFY_RETIRE_FAILED')
            raise QuarantinedRequestError(identity)
        observe('RETIRED')

    def _coordinated_request(self,control,path,params,*,timeout,cancelled,
                             drain_on_cancel,observe,context):
        observe=safe_observer(observe)
        request_id=None;terminal='NOT_ADMITTED';failure=None
        try:
            params=params or {}
            url='https://data-api.binance.vision'+path+'?'+urllib.parse.urlencode(params)
            allowed(url)
            if not math.isfinite(timeout) or not 0<timeout<=600:
                raise ValueError('PUBLIC_DEADLINE_INVALID')
            if not isinstance(context,dict) or not {'epoch','job_key','scope','kind'}<=context.keys():
                raise ValueError('CONTROL_REQUEST_CONTEXT_REQUIRED')
            deadline=time.monotonic()+timeout
            # Cancellation is itself a terminal disposition: allocate the
            # logical call before testing it, without ever admitting HTTP.
            request_id=coordinate(lambda:control.begin_call(context),deadline)
            directory=path=='/api/v3/exchangeInfo'
            if (control.request_scope(path,params)!=sorted(set(context['scope']))
                    or directory!=(context['kind']=='directory')):
                raise ControlError('CONTROL_REQUEST_SCOPE_IDENTITY_MISMATCH')
            # Official exchangeInfo weight is 20 (including symbols batches).
            # Preserve the verified endpoint cost; never refund it from a
            # later, potentially reordered, response header.
            weight=20 if directory else 2
            def check():
                if cancelled() or time.monotonic()>=deadline:
                    raise NotSentError('PUBLIC_REQUEST_NOT_SENT')
            for attempt in range(1,4):
                owner=None;status=0;retry=1;not_sent=None;headers=[]
                while owner is None:
                    check()
                    try:
                        owner=coordinate(lambda:control.admit(context,request_id,attempt,path,weight,deadline),deadline,cancelled)
                    except AdmissionClosed as error:
                        raise NotSentError('CONTROL_ADMISSION_CLOSED_NOT_SENT') from error
                    if owner is None:time.sleep(min(control.admission_delay(),max(0,deadline-time.monotonic())))
                observe('ATTEMPT',attempt=attempt)
                def event(kind,**fields):
                    # Transport observers cannot fail admission. Only in-memory
                    # facts are captured here; control writes happen afterward.
                    if kind=='WEIGHT':headers.append((fields['used'],fields['window_ms']))
                    else:control.observe(owner,kind,**fields)
                    observe(kind,**fields)
                def before_go():
                    control.before_go(owner);check()
                def denial():
                    try:
                        control.failure(403,'Binance denial requires manual review',timeout=0)
                    except BaseException as error:
                        try:control.latch_stop('CONTROL_DENIAL_NOT_DURABLE')
                        except ControlError:pass
                        raise DurableStopError('CONTROL_DENIAL_NOT_DURABLE') from error
                try:
                    try:
                        check()
                        options={'observe':event}
                        if drain_on_cancel:options['drain_on_cancel']=True
                        status,payload,retry=self.transport(url,deadline,cancelled,denial,before_go,**options)
                        # A custom transport must supply the same independent
                        # completion/reap observations as the real child.
                        if not owner.complete or not owner.reaped:
                            raise UncertainRequestError('CONTROL_TRANSPORT_PROOF_MISSING')
                    except NotSentError as error:
                        if owner.go:
                            raise UncertainRequestError('CONTROL_NOT_SENT_CONTRADICTS_GO') from error
                        owner.not_sent=True;not_sent=error
                    for used,window in headers:control.calibrate(used,window)
                    if not_sent is None and status==200:
                        body=json.loads(payload)
                        if not isinstance(body,dict if directory else list):
                            raise ValueError('BINANCE_PUBLIC_RESPONSE_SHAPE_INVALID')
                    elif not_sent is None:
                        decision=control.failure(status,'',retry_after=max(retry,2**(attempt-1)))
                    self._retire_controlled(control,owner,observe)
                    if not_sent is not None:raise not_sent
                    if status==200:
                        # Retire safe resources before rejecting old/cancelled
                        # data. A discarded result is never replayed as valid.
                        if cancelled() or time.monotonic()>=deadline:
                            raise TimeoutError('PUBLIC_RESPONSE_FINISHED_AFTER_DEADLINE')
                        terminal='COMPLETED_RETIRED'
                        return body
                    if status in (403,418) or decision!='BOUNDED_RETRY':
                        raise DurableStopError(decision) if status in (403,418) else RuntimeError(decision)
                except QuarantinedRequestError:
                    observe('ERROR',error_type='QuarantinedRequestError')
                    terminal='QUARANTINED_RETIRED';raise
                except BaseException as error:
                    observe('ERROR',error_type=type(error).__name__)
                    if not owner.retired:
                        try:control.retain(owner,str(error))
                        except ControlError as retained:
                            raise DurableStopError('CONTROL_OWNER_RETAIN_UNCONFIRMED') from retained
                        terminal='RETAINED_MANUAL_REVIEW'
                        if not isinstance(error,(DurableStopError,UncertainRequestError,UnreapedRequestError)):
                            raise DurableStopError('CONTROL_REQUEST_STOP_MANUAL_REVIEW') from error
                    else:terminal='RETIRED_REJECTED'
                    raise
                finally:
                    observe('END')
            raise RuntimeError('PUBLIC_RETRY_BUDGET_EXHAUSTED')
        except BaseException as error:
            failure=type(error).__name__
            if isinstance(error,AdmissionClosed):
                raise NotSentError('CONTROL_ADMISSION_CLOSED_NOT_SENT') from error
            if request_id is None and isinstance(error,(sqlite3.Error,ControlError)):
                try:control.latch_stop('CONTROL_CALL_ALLOCATION_UNCONFIRMED')
                except ControlError:pass
                raise DurableStopError('CONTROL_CALL_ALLOCATION_UNCONFIRMED') from error
            if isinstance(error,ControlError):
                try:control.latch_stop('CONTROL_REQUEST_STATE_UNCONFIRMED')
                except ControlError:pass
                raise DurableStopError('CONTROL_REQUEST_STATE_UNCONFIRMED') from error
            raise
        finally:
            evidence=dict(error_type=failure,terminal=terminal)
            try:
                if request_id is not None:
                    try:control.end_call(request_id,terminal,evidence)
                    except BaseException as error:
                        try:control.latch_stop('CONTROL_CALL_TERMINAL_UNCONFIRMED')
                        except ControlError:pass
                        raise DurableStopError('CONTROL_CALL_TERMINAL_UNCONFIRMED') from error
            finally:
                observe('REQUEST_END',request_id=request_id,terminal=terminal,error_type=failure)

    def _legacy_request(self, path, params=None, *, timeout=30, cancelled=lambda:False, drain_on_cancel=False, observe=None):
        if observe is not None:observe=safe_observer(observe)
        params = params or {}
        url = 'https://data-api.binance.vision'+path+'?'+urllib.parse.urlencode(params)
        allowed(url)
        directory=path=='/api/v3/exchangeInfo'
        if not math.isfinite(timeout) or timeout <= 0 or timeout > 600:
            raise ValueError('PUBLIC_DEADLINE_INVALID')
        deadline = time.monotonic()+timeout
        def check():
            if cancelled() or time.monotonic() >= deadline:
                raise TimeoutError('PUBLIC_REQUEST_CANCELLED_OR_EXPIRED')
        for attempt in range(3):
            token = None
            while token is None:
                check()
                weights={'weight':20} if directory else {}
                token = coordinate(lambda:self.budget.acquire(
                    timeout=min(.25,max(0,deadline-time.monotonic())),**weights),deadline,cancelled)
                if token is None: time.sleep(min(.02,max(0,deadline-time.monotonic())))
            release = True
            status, payload, retry = 0, b'', 1
            denied = False
            if observe is not None:observe('ATTEMPT',attempt=attempt+1)
            try:
                check()
                tracking={'observe':observe} if observe is not None else {}
                with tracked(self.store, token, deadline, cancelled, cleanup_timeout=1,**tracking) as orphan:
                    release = False  # tracked commits marker+lease removal together.
                    def on_denial():
                        nonlocal denied
                        denied = True
                        try:
                            # Never spend the request deadline waiting on a DB
                            # writer while a known denied socket remains open.
                            with self.store.db(timeout=0) as db:
                                db.execute('UPDATE requests SET denied=1 WHERE token=?',(token,))
                        except sqlite3.Error as error:
                            try: orphan()  # Block clients before kill/wait/drain.
                            finally:
                                raise DurableStopError('DENIAL_STOP_NOT_DURABLE_MANUAL_REVIEW') from error
                    def before_go():
                        state = coordinate(lambda:self.budget.status(
                            timeout=max(0,min(.25,deadline-time.monotonic()))),deadline,cancelled)
                        check()  # Cancellation may occur during the SQLite read.
                        if state['halt'] or time.time_ns()//1_000_000 < state['cooldown_ms']:
                            raise RuntimeError('PUBLIC_ADMISSION_REVOKED')
                    not_sent = None
                    try:
                        # A collector session stops admission immediately but lets an
                        # already-sent request finish within its ORIGINAL deadline.
                        # The transport keeps the real predicate through GO;
                        # draining applies only after admission was sent.
                        options={'drain_on_cancel':True} if drain_on_cancel else {}
                        if observe is not None:options['observe']=observe
                        status,payload,retry = self.transport(url,deadline,cancelled,on_denial,before_go,**options)
                    except NotSentError as error:
                        not_sent = error
                    if status in (403,418) or denied:
                        try:
                            decision = self.budget.failure(403,'Binance denial requires manual review',timeout=0)
                        except sqlite3.Error as error:
                            raise DurableStopError('DENIAL_STOP_NOT_DURABLE_MANUAL_REVIEW') from error
                        # Marker can be removed only after denial is durable.
                        status = 403
                    elif not_sent is None:
                        if status == 200:
                            body = json.loads(payload)
                            if not isinstance(body,dict if directory else list):
                                raise ValueError('BINANCE_PUBLIC_RESPONSE_SHAPE_INVALID')
                        else:
                            decision = self.budget.failure(status,'',retry_after=max(retry,2**attempt),timeout=0)
                if not_sent is not None: raise not_sent
                if status == 200:
                    # The child has been reaped and its complete response
                    # validated. Retire ownership before rejecting late data.
                    check()
                    return body
            except (UnreapedRequestError,UncertainRequestError,DurableStopError,sqlite3.Error,ValueError) as error:
                release = False  # Persistent orphan marker is a global stop.
                if observe is not None:observe('ERROR',error_type=type(error).__name__)
                raise
            except BaseException as error:
                if observe is not None:observe('ERROR',error_type=type(error).__name__)
                raise
            finally:
                try:
                    if release:
                        self.budget.release(token,timeout=max(0,min(.25,deadline-time.monotonic())))
                finally:
                    if observe is not None:observe('END')
            if status == 403 or decision != 'BOUNDED_RETRY':
                raise RuntimeError(decision)
        raise RuntimeError('PUBLIC_RETRY_BUDGET_EXHAUSTED')
