#!/usr/bin/env python3
"""Offline phase replay: real monitor/publisher, synthetic HTTP, local Git only."""
import argparse
import collections
import contextlib
import datetime as dt
import hashlib
import io
import json
import os
import platform
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from unittest import mock


def git(root, *args, binary=False):
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT='0', GIT_ALLOW_PROTOCOL='file')
    return subprocess.check_output(['git', '-c', 'core.hooksPath=' + os.devnull,
        '-c', 'core.autocrlf=false', '-c', 'commit.gpgsign=false', *args],
        cwd=root, env=env, stderr=subprocess.PIPE, text=not binary)


def export(source, ref, dest, paths=(), skip_results=False):
    raw = git(source, 'archive', '--format=tar', ref, *paths, binary=True)
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        for member in archive.getmembers():
            if skip_results and (member.name=='research/results' or member.name.startswith('research/results/')):
                continue
            target = dest / member.name
            if not target.resolve().is_relative_to(dest.resolve()) or not (member.isfile() or member.isdir()):
                raise RuntimeError('UNSAFE_REPLAY_ARCHIVE')
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.extractfile(member).read())


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class UnexpectedNetwork(BaseException):
    pass


def worker(args):
    # Only this disposable directory may be mutated. No production origin exists.
    root = Path.cwd().resolve()
    if Path(git(root, 'remote', 'get-url', 'origin').strip()).resolve() != root.parent / 'origin.git':
        raise RuntimeError('REPLAY_REQUIRES_TEMP_LOCAL_ORIGIN')
    sys.path.insert(0, str(root))
    from research import hunter_position_monitor as monitor
    from research import hunter_scheduler_health as health
    from research import hunter_fast_reconciliation as reconciliation
    from scripts import hunter_monitor_persist as publisher
    from scripts import hunter_monitor_runtime as runtime
    engine = monitor.eng
    now = dt.datetime.fromisoformat(args.now)
    real_datetime = dt.datetime
    class FrozenDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)
        @classmethod
        def utcnow(cls):
            return now.replace(tzinfo=None)
    states = [monitor.load(monitor.V1), monitor.load(monitor.V2)]
    inputs = [(str(p.relative_to(root)),p.stat().st_size,hashlib.sha256(p.read_bytes()).hexdigest())
              for p in sorted((root/'research/results').rglob('*')) if p.is_file()]
    prices = {'BTC': 60000.0, 'USDC': 1.0, 'FDUSD': 1.0, 'TUSD': 1.0}
    for state in states:
        for p in state.get('open_positions', []) + state.get('closed_positions', []):
            value = p.get('last_price') or p.get('exit_reference_price') or engine.weighted_entry(p)
            prices[p['asset']] = float(value)
    calls, spans, measurements = [], [], collections.defaultdict(list)
    call_lock = threading.Lock()
    current_lane = [None]
    def response(request, timeout=None, **kwargs):
        start = time.perf_counter()
        url = request.full_url if hasattr(request, 'full_url') else str(request)
        parsed = urllib.parse.urlparse(url)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        if parsed.hostname not in ('data-api.binance.vision', 'fapi.binance.com', 'api.binance.com'):
            raise UnexpectedNetwork('UNEXPECTED_REPLAY_HOST')
        symbol = q.get('symbol', 'BTCUSDT'); price = prices.get(symbol.removesuffix('USDT'), 1.0)
        category = 'quote'
        if parsed.path == '/api/v3/ticker/24hr':
            payload = [dict(symbol=a+'USDT', lastPrice=str(p), priceChangePercent='0') for a,p in prices.items()]
        elif parsed.path == '/api/v3/klines':
            step = {'5m': 300000, '15m': 900000, '1h': 3600000}[q['interval']]
            count = int(q.get('limit', 16)); end = int(q.get('endTime', int(now.timestamp()*1000)))
            if 'startTime' in q:
                category = 'historical'; first = ((int(q['startTime'])+step-1)//step)*step
            else:
                first = (end//step-count)*step
            payload = [[t,str(price),str(price*1.01),str(price*.99),str(price),'1000',t+step-1,'100000',1,'1','1','0']
                       for t in range(first, min(first+count*step, end), step)]
        elif parsed.path == '/api/v3/depth':
            payload = dict(lastUpdateId=1,bids=[[str(price*.999),str(1000000/price)]],
                           asks=[[str(price*1.001),str(1000000/price)]])
        elif parsed.path == '/api/v3/ticker/price':
            payload = {'price': '1'}
        elif parsed.path == '/fapi/v1/premiumIndex':
            payload = dict(markPrice='60000',indexPrice='60000',lastFundingRate='0')
        elif parsed.path == '/fapi/v1/openInterest':
            payload = dict(openInterest='100000')
        else:
            raise UnexpectedNetwork('UNEXPECTED_REPLAY_ENDPOINT ' + parsed.path)
        delay = args.history_delay if category == 'historical' else args.quote_delay
        time.sleep(delay)
        raw = json.dumps(payload).encode()
        stop = time.perf_counter()
        with call_lock:
            calls.append(dict(category=category,path=parsed.path,query=q,lane=current_lane[0],
                              seconds=stop-start,bytes=len(raw),response_sha256=hashlib.sha256(raw).hexdigest()))
            spans.append((start,stop,category))
        return io.BytesIO(raw)
    def wrap(name, fn):
        def measured(*a, **kw):
            begin = time.perf_counter()
            try:
                return fn(*a, **kw)
            finally:
                measurements[name].append(time.perf_counter()-begin)
        return measured
    original_lane = monitor.run_lane
    def lane(*a, **kw):
        current_lane[0] = a[1]
        try:
            return wrap(a[1]+'_lifecycle',original_lane)(*a, **kw)
        finally:
            current_lane[0] = None
    base = git(root, 'rev-parse', 'HEAD').strip()
    phases = {}
    log = io.StringIO()
    with contextlib.ExitStack() as stack:
        stack.enter_context(contextlib.redirect_stdout(log))
        stack.enter_context(mock.patch.object(dt, 'datetime', FrozenDateTime))
        stack.enter_context(mock.patch.object(urllib.request, 'urlopen', side_effect=response))
        stack.enter_context(mock.patch('socket.socket.connect', side_effect=UnexpectedNetwork('LIVE_NETWORK_FORBIDDEN')))
        stack.enter_context(mock.patch('socket.getaddrinfo', side_effect=UnexpectedNetwork('LIVE_DNS_FORBIDDEN')))
        stack.enter_context(mock.patch.object(engine, '_execution_source_sha', return_value=args.fixture_ref))
        stack.enter_context(mock.patch.object(reconciliation, 'observe', return_value={'status':'OFFLINE_FIXTURE','formal_portfolio_mutated':False}))
        stack.enter_context(mock.patch.object(monitor, 'run_lane', side_effect=lane))
        for name in ('manage_existing_positions','refresh_closed_observations','backfill_opportunity_history','build_summary','atomic_json_write','load_portfolio'):
            stack.enter_context(mock.patch.object(engine, name, side_effect=wrap(name,getattr(engine,name))))
        start = time.perf_counter(); monitor.main(); phases['monitor_body'] = time.perf_counter()-start
        generation = health.generation_id(now)
        previous = health.load_health()
        health.atomic_write(health.HEALTH, health.build_success_health(now, now.isoformat(), previous,
            'VULTR_SYSTEMD', args.fixture_ref, generation))
        expected = publisher.snapshot()
        start=time.perf_counter(); publisher.validate(expected,generation); phases['current_state_validation']=time.perf_counter()-start
        stack.enter_context(mock.patch.object(runtime,'publish_verified',side_effect=wrap('proof_publication_including_readback',runtime.publish_verified)))
        start=time.perf_counter(); publisher.persist(base,generation); phases['persistence_including_proof']=time.perf_counter()-start
    output = {n: json.loads(raw) for n,raw in expected.items()}
    recorded_timings=output['hunter-position-monitor.json'].pop('timing_seconds',{})
    recorded_pages={r['lane']:r['historical_backfill_http_page_attempts']
        for r in output['hunter-position-monitor.json']['results'] if 'historical_backfill_http_page_attempts' in r}
    if recorded_pages:
        actual_pages=collections.Counter(c['lane'] for c in calls if c['category']=='historical')
        if any(recorded_pages[lane]!=actual_pages[lane] for lane in recorded_pages):
            raise AssertionError('RECORDED_HISTORY_PAGE_COUNT_MISMATCH')
        for lane in ('v1','v2'):
            for phase in ('management_decisions','closed_history','open_history','portfolio_write','summary','summary_write'):
                if lane+'_'+phase not in recorded_timings:raise AssertionError('MISSING_RECORDED_PHASE')
    decisions = {n:{k:v for k,v in d.items() if k in ('decisions','events')}
                 for n,d in output.items() if n.endswith('-portfolio.json')}
    def union_seconds(category):
        merged=[]
        for start,stop,kind in sorted(spans):
            if kind!=category:continue
            if merged and start<=merged[-1][1]:merged[-1][1]=max(stop,merged[-1][1])
            else:merged.append([start,stop])
        return sum(b-a for a,b in merged)
    report = dict(code_ref=args.code_ref,fixture_ref=args.fixture_ref,now=args.now,
        python=platform.python_version(),platform=platform.platform(),history_workers=args.history_workers,
        input_manifest_sha256=digest(inputs),input_files=len(inputs),input_bytes=sum(x[1] for x in inputs),
        synthetic_responses=True,actual_network_measured=False,history_delay=args.history_delay,
        quote_delay=args.quote_delay,backfill_budget=engine.OPPORTUNITY_BACKFILL_BUDGET,
        positions_before=[len(s['open_positions']) for s in states],phases=phases,
        recorded_monitor_timings=recorded_timings,recorded_history_page_attempts=recorded_pages,
        http_wall_union_seconds={kind:union_seconds(kind) for kind in ('quote','historical')},
        function_seconds={k:sum(v) for k,v in measurements.items()},
        function_calls={k:len(v) for k,v in measurements.items()},http_requests=calls,
        request_counts=dict(collections.Counter(c['category'] for c in calls)),
        historical_by_lane=dict(collections.Counter(c['lane'] for c in calls if c['category']=='historical')),
        output_sha256=digest(output),decisions_events_sha256=digest(decisions),
        published_verified='SHADOW_STATE_PUSH_VERIFIED' in log.getvalue(),
        proof_verified='HUNTER_MONITOR_PRIMARY_PROOF_VERIFIED' in log.getvalue(),
        real_trading_enabled=False,capital_authority='NONE_SHADOW_ONLY')
    Path(args.output).write_text(json.dumps(report,indent=2),encoding='utf-8')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--worker',action='store_true')
    p.add_argument('--source',default='.')
    p.add_argument('--code-ref',required=True)
    p.add_argument('--fixture-ref',required=True)
    p.add_argument('--now',default='2026-10-11T22:22:01.259258+00:00')
    p.add_argument('--history-delay',type=float,default=0)
    p.add_argument('--quote-delay',type=float,default=0)
    p.add_argument('--history-workers',type=int,choices=(1,2,4),default=1)
    p.add_argument('--output',required=True)
    args=p.parse_args()
    os.environ['HUNTER_MONITOR_HISTORY_WORKERS']=str(args.history_workers)
    if args.worker:
        worker(args);return
    source=Path(args.source).resolve();output=Path(args.output).resolve()
    before=git(source,'status','--porcelain')
    with tempfile.TemporaryDirectory(prefix='hunter-phase-replay-') as directory:
        root=Path(directory);seed=root/'seed';seed.mkdir()
        # The fixture owns the entire results tree, including file membership.
        # A newer code revision must not contribute extra mutable result files.
        export(source,args.code_ref,seed,skip_results=True)
        export(source,args.fixture_ref,seed,('research/results',))
        git(root,'init','--bare','--initial-branch=main',str(root/'origin.git'))
        git(seed,'init','--initial-branch=main')
        for k,v in [('user.name','offline-replay'),('user.email','offline@localhost')]:git(seed,'config',k,v)
        git(seed,'remote','add','origin',str(root/'origin.git'))
        git(seed,'add','.');git(seed,'commit','-m','isolated fixture');git(seed,'push','origin','main')
        env=dict(os.environ,GIT_ALLOW_PROTOCOL='file',GIT_CONFIG_NOSYSTEM='1',GIT_CONFIG_GLOBAL=os.devnull,
                 GIT_TERMINAL_PROMPT='0',GITHUB_ACTIONS='false',PYTHONDONTWRITEBYTECODE='1',PYTHONUTF8='1')
        subprocess.run([sys.executable,str(Path(__file__).resolve()),'--worker','--code-ref',args.code_ref,
            '--fixture-ref',args.fixture_ref,'--now',args.now,'--history-delay',str(args.history_delay),
            '--quote-delay',str(args.quote_delay),'--history-workers',str(args.history_workers),
            '--output',str(output)],cwd=seed,env=env,check=True,timeout=300)
    if git(source,'status','--porcelain')!=before:raise RuntimeError('REPLAY_SOURCE_MUTATED')
    r=json.loads(output.read_text(encoding='utf-8'))
    print(json.dumps({k:v for k,v in r.items() if k!='http_requests'},indent=2))


if __name__=='__main__':main()
