"""Opt-in Bybit spot sidecar; no strategy, ticker, listing or ledger execution.

Default CLI is a local plan. Live collection requires --collect and a separately
provisioned shared state directory. Production adapters are deliberately untouched.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import hashlib
import json
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from research.hunter_bybit_stream_store import Budget, FRESH_MS, LIMITS, STATE_PATH, Store, split_topic, topic

WS_URL = 'wss://stream.bybit.com/v5/public/spot'
REST_URL = 'https://api.bybit.com/v5/market/kline'
MAX_CONNECTION_ATTEMPTS = 5
MAX_REST_ATTEMPTS = 3


def millis():
    return time.time_ns()//1_000_000


def roster(snapshot, now):
    # Use exactly the already captured collector scope, not all exchange symbols.
    # validate is pure: no listing refresh, strategy run, or market request.
    from research.hunter_bybit_worker import validate
    validate(snapshot, now)
    bases = snapshot['venue_status'].get('signal_expected_bases')
    if not isinstance(bases, list) or not bases or len(set(bases)) != len(bases):
        raise ValueError('CAPTURED_SCOPE_MISSING')
    available = {r['base'] for r in snapshot['rows']}
    if any(b not in available or b == 'BTC' for b in bases):
        raise ValueError('CAPTURED_SCOPE_INVALID')
    return sorted({topic(b+'USDT', i) for b in bases+['BTC'] for i in LIMITS})


def subscriptions(topics, epoch):
    # JSON serialization length is stricter than sum(topic lengths).
    if len(json.dumps(topics, separators=(',', ':'))) > 21_000:
        raise ValueError('CONNECTION_ARGS_LIMIT_REQUIRES_REVIEWED_SHARDING')
    return [{'req_id':f'{epoch}-{n//10}', 'op':'subscribe', 'args':topics[n:n+10]}
            for n in range(0, len(topics), 10)]


def plan(topics):
    n = len(topics)
    return dict(topics=n, symbols=n//2, subscriptions=len(subscriptions(topics, 'plan')),
                rest_requests_per_attempt=n, admission_floor_seconds=max(0, n-1)*.2,
                deadline_seconds=600,
                single_attempt_10s_latency_floor_seconds=(n+3)//4*10,
                note='No measured network SLA; timeout/stale topics remain UNKNOWN. Warm cache is continuous.')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('REDIRECT_FORBIDDEN')


def direct_get(url):
    """No proxy, credentials, alternate domain, redirect or Worker fallback."""
    if not url.startswith(REST_URL+'?'):
        raise ValueError('HOST_NOT_ALLOWED')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(url, headers={'Accept':'application/json', 'User-Agent':'Hunter-Spot-Sidecar/1'})
    try:
        response = opener.open(request, timeout=10)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        payload = response.read(1_048_577)
        if len(payload) > 1_048_576:
            raise ValueError('RESPONSE_TOO_LARGE')
        retry = response.headers.get('Retry-After', '1')
        return response.code, payload, float(retry) if retry.isdigit() else 1


def repair(store, epoch, name, deadline, get=direct_get, clock=millis, sleep=time.sleep):
    """Every initialization, retry and gap repair uses the same host budget.

A full 25/5 rolling window bounds long-outage recovery. Never append thousands
of historical bars or retroactively serve the missed generations.
"""
    symbol, interval = split_topic(name)
    budget = Budget(store)
    for attempt in range(MAX_REST_ATTEMPTS):
        token = None
        while token is None:
            now = clock()
            if now >= deadline:
                raise TimeoutError('RECOVERY_DEADLINE')
            state = budget.status()
            if state['halt'] or state['cooldown_ms']-now >= 590_000:
                raise RuntimeError(state['halt'] or 'GLOBAL_COOLDOWN_10_MINUTES')
            token = budget.acquire() if clock is millis else budget.acquire(now)
            if token is None:
                sleep(.05)
        try:
            end = clock()  # New observation time, never the end of a missed generation.
            url = REST_URL+'?'+urllib.parse.urlencode(dict(category='spot', symbol=symbol,
                                      interval=interval, limit=LIMITS[interval], end=end))
            try:
                status, payload, retry_after = get(url)
            except (TimeoutError, urllib.error.URLError, OSError):
                status, payload, retry_after = 0, b'', 2**attempt
            now = clock()
            # A denied response must persist the host-wide stop even when it
            # arrives too late for this recovery's data deadline.
            if status == 403:
                decision = budget.failure(status, payload[:4096].decode('utf-8', 'replace'), now)
                raise RuntimeError(decision)
            if now >= deadline:
                raise TimeoutError('RECOVERY_DEADLINE')
            if status == 200:
                body = json.loads(payload)
                if type(body.get('retCode')) is int and body['retCode'] == 0:
                    store.ingest_rest(epoch, name, body, end, now)
                    return
                # A reported API quota error consumes the same bounded budget.
                if body.get('retCode') in (10006, 10016):
                    status = 429 if body['retCode'] == 10006 else 503
                else:
                    raise ValueError('BYBIT_RET_REJECTED')
            decision = budget.failure(status, payload[:4096].decode('utf-8', 'replace'), now,
                                      max(retry_after, 2**attempt))
            if decision != 'BOUNDED_RETRY':
                raise RuntimeError(decision)
        finally:
            budget.release(token)
    raise RuntimeError('REST_RETRY_BUDGET_EXHAUSTED')


class Protocol:
    """ACK and topic state are separate from ping/pong liveness."""
    def __init__(self, store, topics, scope_hash, now_ms):
        self.store = store
        self.epoch = store.begin_session(topics, scope_hash)
        self.requests = subscriptions(topics, self.epoch)
        self.pending = {r['req_id']:r['args'] for r in self.requests}
        self.started = self.last_pong = self.last_ping = now_ms

    def receive(self, message, now_ms):
        if message.get('op') == 'subscribe':
            args = self.pending.pop(message.get('req_id'), None)
            if args is None or message.get('success') is not True:
                raise ValueError('SUBSCRIPTION_ACK_FAILED_OR_UNKNOWN')
            self.store.ack(self.epoch, args)
        elif message.get('op') == 'pong' or (message.get('op') == 'ping' and message.get('ret_msg') == 'pong'):
            self.last_pong = now_ms
        elif 'topic' in message:
            self.store.ingest_ws(self.epoch, message, now_ms)
        else:
            raise ValueError('UNEXPECTED_WS_MESSAGE')

    def tick(self, now_ms):
        if now_ms-self.last_pong > 60_000 or (self.pending and now_ms-self.started > 30_000):
            raise TimeoutError('HEARTBEAT_OR_ACK_TIMEOUT')
        if now_ms-self.last_ping >= 20_000:
            self.last_ping = now_ms
            return {'op':'ping', 'req_id':self.epoch+'-ping'}


def open_ws():
    # Optional runtime dependency, not imported by pure cache readers or fixtures.
    from websockets.sync.client import connect
    return connect(WS_URL, proxy=None, open_timeout=10, close_timeout=5,
                   ping_interval=None, max_size=262_144, max_queue=1024)


def session(store, topics, scope_hash, ws, stop, clock=millis, sleeper=time.sleep, repairer=repair):
    protocol = Protocol(store, topics, scope_hash, clock())
    jobs = {}
    completed = set()
    deadline = clock()+plan(topics)['deadline_seconds']*1000
    # Only four submitted jobs at a time: no unbounded pending retries on 403.
    with ThreadPoolExecutor(max_workers=4) as pool:
        try:
            for request in protocol.requests:
                ws.send(json.dumps(request))
                sleeper(.1)
            while not stop():
                now = clock()
                global_state = Budget(store).status()
                if global_state['halt'] or global_state['cooldown_ms']-now >= 590_000:
                    raise RuntimeError(global_state['halt'] or 'GLOBAL_COOLDOWN_10_MINUTES')
                for name, job in list(jobs.items()):
                    if job.done():
                        job.result()  # Failure stops this session; no hidden retry loop.
                        completed.add(name)
                        del jobs[name]
                states = store.states()
                for state in states:
                    name = state['topic']
                    if state['ack'] and state['gap'] and name not in jobs and len(jobs) < 4:
                        if name in completed:
                            # A second gap gets a new bounded reconnect recovery epoch.
                            raise RuntimeError('NEW_GAP_RECONNECT_REQUIRED')
                        jobs[name] = pool.submit(repairer, store, protocol.epoch, name, deadline)
                    if state['ack'] and now-protocol.started > FRESH_MS and now-state['ws_rx'] > FRESH_MS:
                        raise TimeoutError('TOPIC_STALE_DESPITE_HEARTBEAT')
                ping = protocol.tick(now)
                if ping:
                    ws.send(json.dumps(ping))
                try:
                    message = ws.recv(timeout=1)
                except TimeoutError:
                    continue
                protocol.receive(json.loads(message), clock())
        finally:
            store.disconnect(protocol.epoch)
            for job in jobs.values():
                job.cancel()


def collect(store, topics, scope_hash, connect=open_ws, clock=millis, sleep=time.sleep, stop=lambda:False):
    """Bounded reconnects; a permanent deny survives process restart in SQLite."""
    last_error = 'NOT_STARTED'
    for attempt in range(MAX_CONNECTION_ATTEMPTS):
        state = Budget(store).status()
        if state['halt'] or clock() < state['cooldown_ms']:
            raise RuntimeError(state['halt'] or 'GLOBAL_COOLDOWN_ACTIVE')
        try:
            with connect() as ws:
                session(store, topics, scope_hash, ws, stop, clock, sleep)
            return
        except (OSError, TimeoutError, ValueError, RuntimeError) as error:
            last_error = type(error).__name__
            # Do not emit arbitrary exception text (may contain headers/body).
            print(json.dumps(dict(event='BYBIT_SIDECAR_RECOVERY', attempt=attempt+1,
                                  error_type=last_error, status='UNKNOWN')), flush=True)
        except Exception as error:
            # websockets exceptions aren't OSError. A denied HTTP handshake is
            # classified globally; protocol/closed errors share the bounded loop.
            response = getattr(error, 'response', None)
            status = getattr(response, 'status_code', 0)
            if status:
                Budget(store).failure(status, bytes(getattr(response, 'body', b'')).decode('utf-8','replace'), clock())
            last_error = type(error).__name__
        if stop():
            return
        if attempt+1 < MAX_CONNECTION_ATTEMPTS:
            sleep(min(60, 5*2**attempt))
    raise RuntimeError('CONNECTION_BUDGET_EXHAUSTED:'+last_error)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True, type=Path, help='Existing captured Worker snapshot, read-only roster')
    parser.add_argument('--collect', action='store_true', help='Opt-in live sidecar only; requires separately approved rollout')
    args = parser.parse_args()
    snapshot = json.loads(args.manifest.read_text(encoding='utf-8'))
    topics = roster(snapshot, dt.datetime.now(dt.timezone.utc))
    print(json.dumps(plan(topics), sort_keys=True))
    if not args.collect:
        return
    import fcntl  # Deployment target Linux; fail explicitly elsewhere.
    # A single host-wide lock, independent of checkout and manifest paths. No
    # command-line/env override that accidentally creates an independent budget.
    with (STATE_PATH.parent/'collector.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        collect(Store(), topics, snapshot['snapshot_sha256'])


if __name__ == '__main__':
    main()
