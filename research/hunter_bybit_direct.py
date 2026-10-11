"""Public spot metadata for the explicit SERVER_DIRECT bundle only.

Shared 24h complete-list cache, bulk ticker, one contradiction refresh. Alpha
and held-position transport are intentionally outside this adapter.
"""
import datetime as dt
import json
import time
from contextlib import contextmanager

try:
    from research.hunter_bybit_public import Client, SOURCE
    from research.hunter_bybit_stream_store import content_hash
    from research import hunter_bybit_availability as availability
except ModuleNotFoundError as error:
    if error.name != 'research': raise
    from hunter_bybit_public import Client, SOURCE
    from hunter_bybit_stream_store import content_hash
    import hunter_bybit_availability as availability

LIST_PATH = '/v5/market/instruments-info'
TICK_PATH = '/v5/market/tickers'
TTL = 86400


def receipt(body, path, now, received):
    value = dict(schema='hunter_public_spot_receipt_v1',source=SOURCE,path=path,
                 captured_at_utc=now.isoformat(),received_at_utc=received.isoformat(),body=body)
    value['sha256'] = content_hash(value)
    return value


def validate_receipt(value, path, now, ttl):
    if (not isinstance(value,dict) or value.get('schema')!='hunter_public_spot_receipt_v1'
            or value.get('source')!=SOURCE or value.get('path')!=path
            or value.get('sha256')!=content_hash({k:v for k,v in value.items() if k!='sha256'})):
        raise ValueError('PUBLIC_RECEIPT_IDENTITY_INVALID')
    stamp = dt.datetime.fromisoformat(value['captured_at_utc'])
    received = dt.datetime.fromisoformat(value['received_at_utc'])
    body = value['body']
    if (stamp.tzinfo is None or received.tzinfo is None or not 0 <= (now-stamp).total_seconds() < ttl
            or not 0 <= (received-stamp).total_seconds() <= 120
            or type(body.get('retCode')) is not int or body['retCode']!=0
            or body.get('result',{}).get('category')!='spot'
            or type(body.get('time')) is not int
            or abs(body['time']/1000-received.timestamp())>120):
        raise ValueError('PUBLIC_RECEIPT_TIME_OR_CATEGORY_INVALID')
    return body


class Adapter:
    def __init__(self, now, client=None):
        self.now, self.client, self.receipts = now, client or Client(), {}

    def healthy(self):
        status = self.client.budget.status()
        if status['halt'] or time.time_ns()//1_000_000 < status['cooldown_ms']:
            raise RuntimeError('PUBLIC_METADATA_GLOBAL_STOP')

    @contextmanager
    def listing_lock(self):
        import fcntl
        path = self.client.store.path.parent/'metadata-list.lock'
        with path.open('a+b') as handle:
            deadline = time.monotonic()+30
            while True:
                self.healthy()
                try:
                    fcntl.flock(handle,fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic()>=deadline: raise TimeoutError('PUBLIC_LIST_LOCK_DEADLINE')
                    time.sleep(.02)
            yield

    def listings(self, force_refresh=False):
        self.healthy()
        with self.listing_lock():
            with self.client.store.db(readonly=True) as db:
                row = db.execute("SELECT payload FROM metadata WHERE kind='listings'").fetchone()
            cached = json.loads(row[0]) if row else None
            fresh = False
            if cached is not None:
                # Corruption is a stop, not a reason to silently erase evidence.
                if cached.get('sha256') != content_hash({k:v for k,v in cached.items() if k!='sha256'}):
                    raise ValueError('PUBLIC_LIST_CACHE_CORRUPT')
                stamp=dt.datetime.fromisoformat(cached['captured_at_utc'])
                age=(self.now-stamp).total_seconds()
                if age<0: raise ValueError('PUBLIC_LIST_CACHE_FUTURE')
                fresh=age<TTL and not force_refresh
            if not fresh:
                body=self.client.request(LIST_PATH)
                cached=receipt(body,LIST_PATH,self.now,self.client.now())
            body=validate_receipt(cached,LIST_PATH,self.now,TTL)
            result=body['result']
            if result.get('nextPageCursor'):
                raise ValueError('PUBLIC_LIST_PARTIAL')
            instruments=availability.validate_spot_rows(result.get('list'))
            if not fresh:
                with self.client.store.db() as db:
                    db.execute("INSERT OR REPLACE INTO metadata(kind,payload) VALUES('listings',?)",
                               (json.dumps(cached,separators=(',',':')),))
            self.healthy()
            self.receipts['listings']=cached
            symbols=sorted({r['baseCoin'] for r in instruments if r['quoteCoin']=='USDT'
                            and r['status']=='Trading' and r.get('symbolType')!='xstocks'})
            stamp=dt.datetime.fromisoformat(cached['captured_at_utc'])
            return symbols,dict(network_requests=int(not fresh),cache_hit=fresh,cache_count=len(instruments),
                cache_age_seconds=round((self.now-stamp).total_seconds(),3),captured_at_utc=stamp.isoformat(),
                expires_at_utc=(stamp+dt.timedelta(seconds=TTL)).isoformat(),complete=True,ttl_seconds=TTL,
                source=SOURCE,receipt_sha256=cached['sha256'])

    def fetch(self, path, body=None):
        if path!='/bybit/tickers' or body is not None:
            raise ValueError('PUBLIC_METADATA_ROUTE_INVALID')
        self.healthy()
        value=receipt(self.client.request(TICK_PATH),TICK_PATH,self.now,self.client.now())
        out=validate_receipt(value,TICK_PATH,self.now,120.001)
        self.receipts['tickers']=value
        return out


def collect(binance_bases=(), now=None, client=None, cache_reader=None, cache_clock=None):
    try:
        from research import hunter_bybit_worker as worker
    except ModuleNotFoundError as error:
        if error.name!='research': raise
        import hunter_bybit_worker as worker
    now=now or dt.datetime.now(dt.timezone.utc)
    adapter=Adapter(now,client)
    return worker.collect(binance_bases,now,kline_source='SPOT_CACHE',cache_reader=cache_reader,
                          cache_clock=cache_clock,public_adapter=adapter)


def validate_metadata(snapshot):
    now=dt.datetime.fromisoformat(snapshot['captured_at_utc'])
    receipts=snapshot.get('public_metadata')
    if not isinstance(receipts,dict) or set(receipts)!={'listings','tickers'}:
        raise ValueError('PUBLIC_SNAPSHOT_METADATA_MISSING')
    listing=validate_receipt(receipts['listings'],LIST_PATH,now,TTL)['result']
    ticks=validate_receipt(receipts['tickers'],TICK_PATH,now,120.001)['result']
    if listing.get('nextPageCursor'):
        raise ValueError('PUBLIC_LIST_PARTIAL')
    instruments=availability.validate_spot_rows(listing.get('list'))
    try:
        from research.hunter_cex_scan import valid
    except ModuleNotFoundError as error:
        if error.name!='research': raise
        from hunter_cex_scan import valid
    bases={r['baseCoin'] for r in instruments if r['quoteCoin']=='USDT' and r['status']=='Trading'
           and r.get('symbolType')!='xstocks' and valid(r['baseCoin'])}
    rows=snapshot['rows']; quotes={}
    for item in ticks.get('list',[]):
        symbol=item.get('symbol') if isinstance(item,dict) else None
        if not symbol or symbol in quotes: raise ValueError('PUBLIC_TICKERS_DUPLICATE_OR_INVALID')
        quotes[symbol]=item
    if {r['base'] for r in rows}!=bases:
        raise ValueError('PUBLIC_LIST_ROSTER_MISMATCH')
    for row in rows:
        q=quotes.get(row['pair'],{})
        if (row['price']!=float(q['lastPrice']) or row['volume_24h_usdt']!=float(q['turnover24h'])
                or row['change_24h_pct']!=float(q['price24hPcnt'])*100):
            raise ValueError('PUBLIC_TICKER_CONTENT_MISMATCH')
    cache=snapshot['venue_status']['listing_cache']
    if cache.get('source')!=SOURCE or cache.get('receipt_sha256')!=receipts['listings']['sha256']:
        raise ValueError('PUBLIC_LIST_RECEIPT_MISMATCH')
