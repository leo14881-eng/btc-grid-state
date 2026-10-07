"""Public market-only daemon; local observation state, no GitHub writes.

Run in a dedicated read-only checkout. aiohttp automatically answers server ping
frames with identical payloads. REST tasks and reconciliation survive WS outages.
"""
import argparse
import asyncio
import collections
import copy
import gzip
import fcntl
import json
import os
from pathlib import Path
import random
import subprocess
import time
import threading

from scripts.hunter_deployment_status import deployment_view

from research.hunter_fast_watch import Config, Watch, DualWatch, utc, bind_observed_model_routes

WS_URL = 'wss://data-stream.binance.vision/ws'
REST_URL = 'https://data-api.binance.vision'
PUBLIC_PATHS = {'/api/v3/ticker/bookTicker': 4, '/api/v3/depth': 25, '/api/v3/exchangeInfo': 20}


class PublicREST:
    def __init__(self, session, config):
        self.session, self.config = session, config
        self.semaphore = asyncio.Semaphore(config.concurrency)
        self.weights = collections.deque()
        self.external_weight = 0
        self.external_at = 0
        self.limit = config.weight_budget_per_minute
        self.blocked_until = 0
        self.failures = 0

    async def get(self, path, params):
        if path not in PUBLIC_PATHS:
            raise ValueError('PUBLIC_MARKET_ENDPOINT_REQUIRED')
        async with self.semaphore:
            now = time.time()
            while self.weights and now - self.weights[0][0] >= 60:
                self.weights.popleft()
            weight = PUBLIC_PATHS[path]
            if now < self.blocked_until:
                raise RuntimeError('CIRCUIT_OPEN')
            if sum(w for _, w in self.weights) + weight > self.limit:
                raise RuntimeError('LOCAL_WEIGHT_BUDGET_EXHAUSTED')
            if now - self.external_at < 60 and self.external_weight + weight >= self.official_limit * .8:
                raise RuntimeError('SHARED_IP_WEIGHT_PRESSURE')
            self.weights.append((now, weight))
            try:
                async with self.session.get(REST_URL + path, params=params,
                        timeout=self.config.timeout_seconds) as response:
                    self.external_weight = int(response.headers.get('X-MBX-USED-WEIGHT-1M', 0))
                    self.external_at = time.time()
                    if response.status != 200:
                        if response.status in (418, 429):
                            self.blocked_until = time.time() + max(float(response.headers.get('Retry-After', 60)), 60)
                        raise RuntimeError('HTTP_' + str(response.status))
                    data = await response.json()
                    self.failures = 0
                    return data
            except Exception as exc:
                self.failures += 1
                if self.failures >= self.config.circuit_failures:
                    delay = min(self.config.max_backoff_seconds, 2 ** min(self.failures, 6))
                    self.blocked_until = max(self.blocked_until, time.time() + delay + random.uniform(0, self.config.jitter_seconds))
                raise RuntimeError(str(exc)) from exc

    official_limit = 6000  # bootstrap conservative local cap; replace using exchangeInfo

    async def configure(self):
        info = await self.get('/api/v3/exchangeInfo', {'symbol': 'BTCUSDT'})
        limits = [r['limit'] for r in info['rateLimits'] if r['rateLimitType'] == 'REQUEST_WEIGHT'
                  and r['interval'] == 'MINUTE' and r['intervalNum'] == 1]
        if not limits:
            raise ValueError('OFFICIAL_RATE_LIMIT_UNKNOWN')
        self.official_limit = min(limits)
        self.limit = min(self.config.weight_budget_per_minute, int(self.official_limit * .05))


class BybitREST(PublicREST):
    async def configure(self):
        # Public IP limit 600/5s; local cap 300/min plus bounded concurrency.
        self.official_limit = 7200

    async def get(self, path, params):
        if path not in PUBLIC_PATHS or path == '/api/v3/exchangeInfo':
            raise ValueError('PUBLIC_MARKET_ENDPOINT_REQUIRED')
        async with self.semaphore:
            now = time.time()
            while self.weights and now-self.weights[0][0] >= 60: self.weights.popleft()
            if now < self.blocked_until: raise RuntimeError('CIRCUIT_OPEN')
            if len(self.weights) >= self.limit: raise RuntimeError('LOCAL_REQUEST_BUDGET_EXHAUSTED')
            self.weights.append((now, 1))
            route = '/v5/market/tickers' if path.endswith('bookTicker') else '/v5/market/orderbook'
            query = dict(category='spot', symbol=params['symbol'])
            if route.endswith('orderbook'): query['limit'] = 200
            try:
                async with self.session.get('https://api.bybit.com'+route, params=query,
                        timeout=self.config.timeout_seconds) as response:
                    if response.status != 200:
                        if response.status in (403, 429): self.blocked_until = time.time()+600
                        raise RuntimeError('HTTP_'+str(response.status))
                    data = await response.json()
                    if data.get('retCode') != 0: raise RuntimeError('BYBIT_'+str(data.get('retCode')))
                    self.failures = 0
                    if route.endswith('tickers'):
                        row = data['result']['list'][0]
                        return dict(symbol=row['symbol'], bidPrice=row['bid1Price'], askPrice=row['ask1Price'],
                                    source_timestamp=data['time'])
                    row = data['result']
                    return dict(symbol=row['s'], bids=row['b'], asks=row['a'], source_timestamp=row['ts'])
            except Exception as exc:
                self.failures += 1
                if self.failures >= self.config.circuit_failures:
                    self.blocked_until = max(self.blocked_until, time.time()+min(60, 2**min(self.failures, 6))+random.random())
                raise RuntimeError(str(exc)) from exc


_READBACK_LOCK = threading.Lock()
_READBACK_CACHE = {}


def read_main(repo):
    # Three daemon tasks share one private readback clone, never the portfolio writer.
    with _READBACK_LOCK:
        key = str(Path(repo).resolve())
        cached = _READBACK_CACHE.get(key)
        if cached and 0 <= time.monotonic()-cached[0] < Config().save_seconds:
            return copy.deepcopy(cached[1])
        result = _read_main(repo)
        _READBACK_CACHE[key] = (time.monotonic(), copy.deepcopy(result))
        return result


def _read_main(repo):
    # Fetch only. No checkout, reset, push, commit or portfolio mutation.
    subprocess.run(['git', '-C', str(repo), 'fetch', 'origin', 'main'], check=True,
                   capture_output=True, timeout=20)
    sha = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'origin/main'], text=True).strip()
    raw = subprocess.check_output(['git', '-C', str(repo), 'show',
        sha + ':research/results/hunter-shadow-v2-portfolio.json'], text=True)
    portfolio = json.loads(raw)
    try:
        monitor = json.loads(subprocess.check_output(['git','-C',str(repo),'show',
            sha+':research/results/hunter-position-monitor.json'],text=True))
        scan = json.loads(subprocess.check_output(['git','-C',str(repo),'show',
            sha+':research/results/hunter-cex-universe-run.json'],text=True))
        liquidity = json.loads(subprocess.check_output(['git','-C',str(repo),'show',
            sha+':research/results/hunter-liquidity-probe.json'],text=True))
        portfolio, admitted = bind_observed_model_routes(portfolio,monitor,sha,time.time(),
            research_scan=scan,research_liquidity=liquidity)
        portfolio['model_route_admitted_ids'] = admitted
    except (subprocess.CalledProcessError, ValueError):
        portfolio['model_route_admitted_ids'] = []
    return portfolio, sha


def deployment_evidence(repo, sha):
    """Reuse existing main job receipts; no independent deployment SSOT."""
    def git(*args):
        return subprocess.run(['git','-C',str(repo),*args],capture_output=True,text=True,timeout=20)
    def read(name):
        result=git('show',sha+':research/results/'+name+'.json')
        return result.stdout if result.returncode==0 else ''
    jobs={}
    for job in ('discovery','research','monitor','watchdog'):
        raw=read('hunter-runtime-'+job+'-health')
        jobs[job]=json.loads(raw) if raw else {}
    return deployment_view({},jobs,read('hunter-scheduler-health'),sha,
        lambda a,b:git('merge-base','--is-ancestor',a,b).returncode==0)


def loaded_code_sha():
    root=Path(__file__).resolve().parents[1]
    row=subprocess.run(['git','-c','safe.directory='+str(root),'-C',str(root),
        'rev-parse','HEAD'],capture_output=True,text=True,timeout=5)
    return row.stdout.strip() if row.returncode==0 else 'UNKNOWN'


def public_health(snapshot, code_sha, versions):
    """Only market health, A/B diagnostics and source receipts leave private state."""
    row=copy.deepcopy(snapshot)
    row['loaded_code_source_sha']=code_sha
    row['deployment_evidence']=versions
    for venue in row.get('venues',{}).values():
        venue.pop('counterfactual',None)
        venue['history']=[h for h in venue.get('history',[]) if h.get('kind') in
            ('REST_TAKEOVER','WS_RECOVERED','GAPPED_THROUGH_PROTECTION_WINDOW')][-20:]
    return row


def atomic_state(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def archive_state(path, value):
    """Durable daily observation snapshots; never a portfolio/trade writer.

    Append gzip members so an interrupted append preserves previous members.
    Invalid/incomplete trailing members must be reported by downstream audits.
    No automatic deletion: observation retention is an operator decision.
    """
    if value.get('mode') != 'OBSERVATION_ONLY' or value.get('capital_authority') != 'NONE_SHADOW_ONLY':
        raise ValueError('ARCHIVE_SHADOW_BOUNDARY_INVALID')
    day = __import__('datetime').datetime.fromisoformat(value['generated_at']).astimezone(
        __import__('datetime').timezone.utc).strftime('%Y%m%d')
    target = Path(str(path)+'.observations.'+day+'.jsonl.gz')
    payload = (json.dumps(value,ensure_ascii=False,allow_nan=False)+'\n').encode()
    target.parent.mkdir(parents=True,exist_ok=True)
    with target.open('ab') as output:
        output.write(gzip.compress(payload,compresslevel=6,mtime=0))
        output.flush();os.fsync(output.fileno())
    return target


def restore(watch, path, now):
    """Never restore old quotes, pending triggers or CONNECTED after restart."""
    try:
        old = json.loads(Path(path).read_text())
        if old['mode'] != 'OBSERVATION_ONLY' or old['capital_authority'] != 'NONE_SHADOW_ONLY':
            raise ValueError('RUNTIME_BOUNDARY_INVALID')
        saved = __import__('datetime').datetime.fromisoformat(old['generated_at']).timestamp()
        if not 0 <= now - saved <= watch.config.unavailable_seconds:
            return 'STALE_LOCAL_RUNTIME_DISCARDED'
        for key in watch.counterfactual:
            if key in old.get('counterfactual', {}) and old['counterfactual'][key]['tranches'] == watch.counterfactual[key]['tranches']:
                watch.counterfactual[key] = old['counterfactual'][key]
        for key, evidence in old.get('ab', {}).items():
            if (key in watch.ab and evidence.get('measurement_schema')=='LIVE_WINDOW_ONLY_V1'
                and evidence.get('tranche_fingerprint')==watch.ab[key]['tranche_fingerprint']):
                watch.ab[key]=evidence
        watch.metrics.update(old.get('metrics', {}))
        # Quote ordering high-watermarks survive reconnect/restart; freshness does not.
        for symbol, row in watch.symbols.items():
            row['sequences'] = old.get('symbols', {}).get(symbol, {}).get('sequences', {})
        return 'OBSERVATION_HISTORY_RESTORED_FRESH_QUOTES_REQUIRED'
    except (OSError, ValueError, KeyError, TypeError):
        return 'NO_VALID_LOCAL_RUNTIME'


def watchdog(path, now):
    try:
        row = json.loads(Path(path).read_text())
        age = now - __import__('datetime').datetime.fromisoformat(row['generated_at']).timestamp()
        if not 0 <= age <= Config().unavailable_seconds:
            return {'status': 'FAST_WATCH_HEALTH_STALE', 'authoritative_monitor_affected': False}
        if 'venues' in row:
            return {'status':row['status'], 'authoritative_monitor_affected':False,
                    'venues':{v:{k:r.get(k) for k in ('status','stale_symbols','rest_fallback_symbols','ws_connected')}
                              for v,r in row['venues'].items()}, 'unroutable_positions':row['unroutable_positions']}
        return {'status': row['status'], 'authoritative_monitor_affected': False,
                'stale_symbols': row['stale_symbols'], 'rest_fallback_symbols': row['rest_fallback_symbols']}
    except (OSError, KeyError, ValueError, TypeError):
        return {'status': 'FAST_WATCH_NOT_DEPLOYED_OR_UNREADABLE', 'authoritative_monitor_affected': False}


async def run(repo, state_path, duration=None, venue='BINANCE_SPOT'):
    import aiohttp  # dependency only required by the resident daemon
    watch = Watch(venue=venue)
    portfolio, sha = await asyncio.to_thread(read_main, repo)
    def subset(p):
        reference = [m for r in p['open_positions'] for m in r.get('verified_spot_markets', [])
            if m.get('base_asset')==r['asset'] and m.get('quote_asset')=='USDT' and m.get('verified') is True]
        return dict(p, verified_reference_markets=reference, open_positions=[r for r in p['open_positions'] if r.get('execution_venue') == venue
            and r.get('market_symbol') and r.get('market_type') == 'spot'])
    portfolio = subset(portfolio)
    watch.reconcile(portfolio, sha, time.time())
    watch.record('RESTART', time.time(), status=restore(watch, state_path, time.time()))
    stop = asyncio.Event()
    async with aiohttp.ClientSession() as session:
        rest = (PublicREST if venue == 'BINANCE_SPOT' else BybitREST)(session, watch.config)
        try:
            await rest.configure()
        except Exception as exc:
            watch.record('RATE_LIMIT_BOOTSTRAP_FAILED', time.time(), error=str(exc))
            # Local 300 weight/min cap still applies; retry official configuration later.

        async def probe(symbol):
            started = time.time()
            if symbol not in watch.symbols:
                return
            watch.symbols[symbol]['next_probe'] = started + watch.config.fallback_seconds
            try:
                data = await rest.get('/api/v3/ticker/bookTicker', {'symbol': symbol})
                watch.rest(symbol, data, started, time.time())
            except Exception as exc:
                watch.rest(symbol, {}, started, time.time(), str(exc))
                if symbol in watch.symbols:
                    watch.symbols[symbol]['next_probe'] = max(watch.symbols[symbol]['next_probe'], rest.blocked_until)

        async def review(key, trigger):
            try:
                raw = await rest.get('/api/v3/depth', {'symbol': trigger['symbol'], 'limit': 500})
                book = dict(raw, fetched_at=utc(time.time()), exchange='binance' if venue=='BINANCE_SPOT' else 'bybit', market='spot',
                            symbol=trigger['symbol'], price_unit='USDT', quantity_unit='BASE',
                            source_timestamp=raw.get('source_timestamp'))
                result = watch.review(key, trigger, book, time.time())
                if result['status'] != 'OBSERVATION_ONLY':
                    watch.record('REVIEW_REJECTED', time.time(), position_id=key, result=result)
            except Exception as exc:
                watch.record('DEPTH_REVIEW_FAILED', time.time(), position_id=key, error=str(exc))

        async def fallback():
            # Independent task: WS connect/recv/backoff never blocks REST.
            while not stop.is_set():
                if not watch.symbols:
                    await asyncio.sleep(1)
                    continue
                await asyncio.gather(*(probe(s) for s in watch.probe_due(time.time())))
                requests = list(watch.pending.items())
                watch.pending.clear()
                await asyncio.gather(*(review(k, t) for k, t in requests))
                await asyncio.sleep(1)

        async def save():
            next_archive = 0
            while not stop.is_set():
                row = watch.snapshot(time.time())
                row['counterfactual'] = copy.deepcopy(watch.counterfactual)
                await asyncio.to_thread(atomic_state, state_path, row)
                if time.time() >= next_archive:
                    await asyncio.to_thread(archive_state, state_path, row)
                    next_archive = time.time()+watch.config.archive_seconds
                await asyncio.sleep(watch.config.save_seconds)

        async def reconcile():
            while not stop.is_set():
                await asyncio.sleep(watch.config.reconcile_seconds)
                try:
                    portfolio, sha = await asyncio.to_thread(read_main, repo)
                    watch.reconcile(subset(portfolio), sha, time.time())
                except Exception as exc:
                    watch.record('RECONCILIATION_FAILED', time.time(), error=str(exc))

        async def websocket():
            attempts = 0
            while not stop.is_set():
                if not watch.symbols:
                    await asyncio.sleep(1)
                    continue
                try:
                    endpoint = WS_URL if venue=='BINANCE_SPOT' else 'wss://stream.bybit.com/v5/public/spot'
                    async with session.ws_connect(endpoint, autoping=True, heartbeat=30,
                            timeout=aiohttp.ClientWSTimeout(ws_close=5), max_msg_size=1024 * 1024) as ws:
                        watch.connect(time.time())
                        attempts = 0
                        request_id = 0
                        pending_commands = {}
                        async def control(method, symbols):
                            nonlocal request_id
                            if not symbols:
                                return
                            request_id += 1
                            pending_commands[request_id] = (method, symbols, time.time())
                            if venue=='BINANCE_SPOT':
                                await ws.send_json({'method': method,
                                    'params': [s.lower() + '@' + kind for s in symbols for kind in ('bookTicker', 'aggTrade')],
                                    'id': request_id})
                            else:
                                await ws.send_json({'op': method.lower(), 'args': ['orderbook.1.'+s for s in symbols], 'req_id': str(request_id)})
                            await asyncio.sleep(.3)  # controls plus heartbeat below 5 incoming/s
                        last_ping = time.time()
                        while not stop.is_set() and not watch.rotation_due(time.time()):
                            if venue=='BYBIT_SPOT' and time.time()-last_ping >= 20:
                                await ws.send_json({'op':'ping'})
                                last_ping = time.time()
                            if not pending_commands:
                                await control('UNSUBSCRIBE', sorted(watch.actual - set(watch.symbols)))
                                await control('SUBSCRIBE', sorted(set(watch.symbols) - watch.actual))
                            if any(time.time() - value[2] > 10 for value in pending_commands.values()):
                                raise RuntimeError('SUBSCRIPTION_ACK_TIMEOUT')
                            try:
                                msg = await asyncio.wait_for(ws.receive(), 1)
                            except TimeoutError:
                                continue
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                data = json.loads(msg.data)
                                if 'id' in data or data.get('op') in ('subscribe','unsubscribe'):
                                    command_id = data.get('id', data.get('req_id'))
                                    method, symbols, _ = pending_commands.pop(int(command_id), ('', [], 0))
                                    rejected = ('code' in data or data.get('result', 'MISSING') is not None) if venue=='BINANCE_SPOT' else data.get('success') is not True
                                    if rejected:
                                        raise RuntimeError('SUBSCRIPTION_REJECTED')
                                    if method == 'SUBSCRIBE': watch.actual.update(symbols)
                                    if method == 'UNSUBSCRIBE': watch.actual.difference_update(symbols)
                                elif data.get('op') in ('pong', 'ping'):
                                    continue
                                elif data.get('e') == 'serverShutdown':
                                    break
                                else:
                                    watch.event(data, time.time())
                            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                                break
                except Exception as exc:
                    watch.record('WS_ERROR', time.time(), error=str(exc))
                finally:
                    watch.disconnect(time.time())
                attempts += 1
                await asyncio.sleep(min(watch.config.max_backoff_seconds, 2 ** min(attempts, 6)) + random.uniform(0, watch.config.jitter_seconds))

        tasks = [asyncio.create_task(f()) for f in (fallback, save, reconcile, websocket)]
        try:
            if duration is None:
                await asyncio.gather(*tasks)
            else:
                await asyncio.sleep(duration)
        finally:
            stop.set()
            for task in tasks: task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            row = watch.snapshot(time.time())
            row['counterfactual'] = watch.counterfactual
            atomic_state(state_path, row)


async def run_dual(repo, state_path, duration=None, health_path=None):
    code_sha=loaded_code_sha()
    paths = {v: Path(str(state_path)+'.'+v+'.json') for v in ('BINANCE_SPOT','BYBIT_SPOT')}
    async def aggregate():
        divergent = set()
        divergence_count = 0
        while True:
            dual = DualWatch()
            try:
                portfolio, sha = await asyncio.to_thread(read_main, repo)
                dual.reconcile(portfolio, sha, time.time())
                snapshot = dual.snapshot(time.time())
                for venue, path in paths.items():
                    try:
                        evidence = json.loads(path.read_text())
                        age = time.time()-__import__('datetime').datetime.fromisoformat(evidence['generated_at']).timestamp()
                        if not 0 <= age <= Config().unavailable_seconds: raise ValueError('VENUE_HEALTH_STALE')
                        snapshot['venues'][venue] = evidence
                        dual.watches[venue].symbols = evidence['symbols']
                    except (OSError, ValueError): pass
                snapshot['cross_venue'] = dual.snapshot(time.time())['cross_venue']
                current_divergent = {x['symbol'] for x in snapshot['cross_venue'] if x['status']=='CROSS_VENUE_PRICE_DIVERGENCE'}
                divergence_count += len(current_divergent-divergent)
                divergent = current_divergent
                snapshot['cross_venue_divergence_count'] = divergence_count
                snapshot['primary_venue_unavailable_count'] = len(dual.unroutable) + sum(
                    r.get('state') in ('FAST_MARKET_DATA_DEGRADED','MARKET_DATA_UNAVAILABLE')
                    for v in snapshot['venues'].values() for r in v['symbols'].values())
                snapshot['source_sha'] = sha
                snapshot['status'] = 'PRIMARY_VENUE_IDENTITY_MISSING' if dual.unroutable else (
                    'FAST_PATH_HEALTHY' if all(x['status'] in ('FAST_PATH_HEALTHY','NOT_REQUIRED') for x in snapshot['venues'].values()) else 'PARTIAL_FAST_PATH_DEGRADED')
                snapshot['loaded_code_source_sha']=code_sha
                atomic_state(state_path, snapshot)
                if health_path is not None:
                    versions=await asyncio.to_thread(deployment_evidence,repo,sha)
                    atomic_state(health_path,public_health(snapshot,code_sha,versions))
                    os.chmod(health_path,0o644)
                print(json.dumps({'status': snapshot['status'], 'unroutable_count':len(dual.unroutable),
                    'venues': {v: {'status':x['status'],'subscriptions':x['subscription_count']} for v,x in snapshot['venues'].items()}}), flush=True)
            except Exception as exc:
                print(json.dumps({'status':'FAST_WATCH_AGGREGATION_FAILED','error':str(exc)}),flush=True)
            await asyncio.sleep(Config().reconcile_seconds)
    tasks = [asyncio.create_task(run(repo, path, venue=venue)) for venue,path in paths.items()]
    tasks.append(asyncio.create_task(aggregate()))
    try:
        if duration is None: await asyncio.gather(*tasks)
        else: await asyncio.sleep(duration)
    finally:
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', default='.')
    parser.add_argument('--state', default='/var/lib/hunter-fast-watch/state.json')
    parser.add_argument('--watchdog', action='store_true')
    parser.add_argument('--health', type=Path, help='Read-only operations health export; no credentials/counterfactual portfolio')
    parser.add_argument('--duration', type=float)
    args = parser.parse_args()
    if args.watchdog:
        print(json.dumps(watchdog(args.state, time.time())))
        return
    path = Path(args.state)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        asyncio.run(run_dual(Path(args.repo), path, args.duration, args.health))


if __name__ == '__main__':
    main()
