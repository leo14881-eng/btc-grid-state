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
    from research.hunter_bybit_stream_store import Store, Budget
    from research.hunter_bybit_http_child import allowed, MAX_BODY
except ModuleNotFoundError as error:
    if error.name != 'research': raise
    from hunter_bybit_stream_store import Store, Budget
    from hunter_bybit_http_child import allowed, MAX_BODY

SOURCE = 'OFFICIAL_BYBIT_V5_SPOT_DIRECT'
SELECTOR = 'HUNTER_BYBIT_SPOT_SOURCE'


class DurableStopError(RuntimeError):
    pass


class UnreapedRequestError(RuntimeError):
    pass


class UncertainRequestError(RuntimeError):
    pass


class NotSentError(TimeoutError):
    pass


def selected():
    value = os.getenv(SELECTOR, 'WORKER')
    if value not in ('WORKER','SERVER_DIRECT'):
        raise ValueError('PUBLIC_SPOT_SOURCE_UNKNOWN')
    if value == 'SERVER_DIRECT':
        if os.getenv('HUNTER_BYBIT_DIRECT_DISABLED') == '1':
            raise ValueError('PUBLIC_DIRECT_DISABLED')
        if os.getenv('HUNTER_BYBIT_KLINE_SOURCE', 'SPOT_CACHE') != 'SPOT_CACHE':
            raise ValueError('PUBLIC_SPOT_SELECTOR_CONFLICT')
    return value


def worker_command(url):
    return [sys.executable, '-B', str(Path(__file__).with_name('hunter_bybit_http_child.py')), url, str(os.getpid())]


def exchange(url, deadline, cancelled, on_denial, before_go=lambda:None):
    """Monotonic deadline includes start/DNS/TLS/headers/body; cleanup is awaited.

    kill+wait is a required closure acknowledgement, not a Future timeout.
    An OS process that cannot be reaped keeps its lease and orphan marker.
    """
    allowed(url)
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
                    process.stdin.write(b'GO\n'); process.stdin.flush(); process.stdin.close()
            elif item.get('kind') == 'headers' and sent and header is None:
                header = item.get('status')
                if type(header) is not int or not 100 <= header <= 599:
                    raise ValueError('PUBLIC_HTTP_STATUS_INVALID')
                if header == 403: on_denial()
            elif item.get('kind') == 'response' and sent and result is None and item.get('status') == header:
                payload = base64.b64decode(item['payload'], validate=True)
                retry = item.get('retry')
                if len(payload)>MAX_BODY or type(retry) is not int or not 0 <= retry <= 600:
                    raise ValueError('PUBLIC_HTTP_RESPONSE_INVALID')
                result = (header,payload,retry)
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
                if cancelled() or time.monotonic() >= deadline:
                    break
                if not chunk and not eof:
                    poll.select(min(.02,max(0,deadline-time.monotonic())))
    except BaseException as error:
        problem = error
    finally:
        if process.poll() is None: process.kill()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired as error:
            raise UnreapedRequestError('PUBLIC_HTTP_PROCESS_NOT_REAPED') from error
        finally:
            if not process.stdin.closed: process.stdin.close()
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
def tracked(store, token, deadline):
    import fcntl
    path = store.path.parent/('request-'+token+'.lock')
    handle = path.open('xb')
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    remove = False
    try:
        with store.db(timeout=max(0,min(.25,deadline-time.monotonic()))) as db:
            db.execute('INSERT INTO requests(token) VALUES(?)',(token,))
        yield
        with store.db(timeout=max(0,min(.25,deadline-time.monotonic()))) as db:
            db.execute('DELETE FROM requests WHERE token=?',(token,))
            db.execute('DELETE FROM leases WHERE token=?',(token,))
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

    def request(self, path, params=None, *, timeout=30, cancelled=lambda:False):
        params = params or {'category':'spot'}
        url = 'https://api.bybit.com'+path+'?'+urllib.parse.urlencode(params)
        allowed(url)
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
                token = self.budget.acquire(timeout=min(.25,max(0,deadline-time.monotonic())))
                if token is None: time.sleep(min(.02,max(0,deadline-time.monotonic())))
            release = True
            status, payload, retry = 0, b'', 1
            denied = False
            try:
                check()
                with tracked(self.store, token, deadline):
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
                            raise DurableStopError('DENIAL_STOP_NOT_DURABLE_MANUAL_REVIEW') from error
                    def before_go():
                        state = self.budget.status(timeout=max(0,min(.25,deadline-time.monotonic())))
                        if state['halt'] or time.time_ns()//1_000_000 < state['cooldown_ms']:
                            raise RuntimeError('PUBLIC_ADMISSION_REVOKED')
                    not_sent = None
                    try:
                        status,payload,retry = self.transport(url,deadline,cancelled,on_denial,before_go)
                    except NotSentError as error:
                        not_sent = error
                    if status == 403 or denied:
                        try:
                            decision = self.budget.failure(403,payload[:4096].decode('utf-8','replace'),timeout=0)
                        except sqlite3.Error as error:
                            raise DurableStopError('DENIAL_STOP_NOT_DURABLE_MANUAL_REVIEW') from error
                        # Marker can be removed only after denial is durable.
                        status = 403
                    elif not_sent is None:
                        check()
                        if status == 200:
                            body = json.loads(payload)
                            if (not isinstance(body,dict) or type(body.get('retCode')) is not int
                                    or not isinstance(body.get('result'),dict)):
                                raise ValueError('PUBLIC_RESPONSE_SHAPE_INVALID')
                            if body['retCode'] == 0:
                                if body['result'].get('category') != 'spot':
                                    raise ValueError('PUBLIC_RESPONSE_CATEGORY_INVALID')
                                check()
                                return body
                            status = {10006:429,10016:503}.get(body['retCode'],400)
                        decision = self.budget.failure(status,'',retry_after=max(retry,2**attempt),timeout=0)
                if not_sent is not None: raise not_sent
            except (UnreapedRequestError,UncertainRequestError,DurableStopError,sqlite3.Error,ValueError):
                release = False  # Persistent orphan marker is a global stop.
                raise
            finally:
                if release: self.budget.release(token)
            if status == 403 or decision != 'BOUNDED_RETRY':
                raise RuntimeError(decision)
        raise RuntimeError('PUBLIC_RETRY_BUDGET_EXHAUSTED')
