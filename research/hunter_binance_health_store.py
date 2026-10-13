"""Opt-in spot health evidence and durable receipt accounting, separate from bars."""
import hashlib
import json
import re
from decimal import Decimal
from research.hunter_binance_market_control import HEALTH_CONTROL_CONTRACT

from research.hunter_binance_stream_store import (
    Store as BaseStore, topic, split_topic, roster_hash, valid_row,
    FRESH_MS, STEPS, LIMITS, WS_SOURCE, REST_SOURCE)

CONTRACT='binance_health_v1'
KINDS={'connection','instrument','kline','quiet'}


def payload_hash(message):
    # Hash canonical decoded JSON, not a claim to preserve wire bytes.
    return hashlib.sha256(json.dumps(message,sort_keys=True,separators=(',',':'),
                                    ensure_ascii=True,allow_nan=True).encode()).hexdigest()


def identity(symbol,interval,exchange='BINANCE',market='spot'):
    if exchange!='BINANCE' or market!='spot':
        raise ValueError('BINANCE_SPOT_IDENTITY_REQUIRED')
    topic(symbol,interval)  # No prefix stripping, ASCII folding or cross-market match.
    return dict(exchange=exchange,market=market,symbol=symbol,interval=interval)


def proof(kind,name,meta,source,observed,verified,expires,result,reason,**extra):
    if kind not in KINDS:raise ValueError('VALIDITY_KIND_INVALID')
    symbol,interval=split_topic(name)
    return dict(schema=CONTRACT,kind=kind,**identity(symbol,interval),
                epoch=meta['epoch'],generation=meta['epoch'],roster_hash=meta['roster_hash'],
                manifest_sha256=meta['scope_hash'],source=source,observed_at=observed,
                verified_at=verified,expires_at=expires,result=result,reason=reason,**extra)


def valid_proof(value,kind,name,meta,now):
    try:
        symbol,interval=split_topic(name)
        expected=dict(schema=CONTRACT,kind=kind,**identity(symbol,interval),
                      epoch=meta['epoch'],generation=meta['epoch'],roster_hash=meta['roster_hash'],
                      manifest_sha256=meta['scope_hash'])
        if any(value.get(k)!=v for k,v in expected.items()):return False
        if value['result']!='VALID' or value.get('reason') is not None:return False
        source={'connection':'OFFICIAL_BINANCE_SPOT_WS_CONNECTION',
                'instrument':'OFFICIAL_BINANCE_SPOT_REST_EXCHANGE_INFO',
                'kline':WS_SOURCE,'quiet':REST_SOURCE}[kind]
        if value['source']!=source:return False
        a,b,c=value['observed_at'],value['verified_at'],value['expires_at']
        if any(type(x) is not int for x in (a,b,c,now)):return False
        if not int(meta['health_epoch_started'])<=a<=b<=now<=c or c-a>FRESH_MS:return False
        if kind=='instrument' and value.get('trading_status')!='TRADING':return False
        if kind=='quiet' and (value.get('comparison')!='SAME_CLOSED_NATIVE_BAR'
                             or type(value.get('closed_start')) is not int
                             or value['closed_start']!=(now//STEPS[interval]-1)*STEPS[interval]):return False
        if kind=='connection' and value.get('evidence') not in ('MATCHING_PONG','VALIDATED_MARKET_FRAME'):return False
        if kind=='kline' and (not re.fullmatch('[0-9a-f]{64}',value.get('window_sha256',''))
                or value.get('generation_end_ms')!=b or type(value.get('ws_event_time')) is not int
                or not value['ws_event_time']<=a or c>value['ws_event_time']+FRESH_MS):return False
        return True
    except (KeyError,TypeError,ValueError):return False


def topic_gate_values(name,cut,meta,instrument,unresolved,reverify):
    """Extra fail-closed gates only for the explicitly selected health contract."""
    if meta.get('health_contract') not in (CONTRACT,HEALTH_CONTROL_CONTRACT):return 'BINANCE_HEALTH_CONTRACT_INVALID'
    if meta.get('health_epoch')!=meta.get('epoch'):return 'BINANCE_HEALTH_EPOCH_REQUIRED'
    if meta.get('health_phase')!='ACTIVE':return 'BINANCE_HEALTH_DRAINING_OR_STOPPED'
    if not instrument or not valid_proof(json.loads(instrument),'instrument',name,meta,cut):
        return 'BINANCE_TRADING_STATUS_UNKNOWN_OR_INVALID'
    if unresolved:
        return 'BINANCE_UNRECONCILED_RECEIPT'
    if reverify:
        return 'BINANCE_QUIET_REVERIFICATION_REQUIRED'
    return None


def topic_gate(db,name,cut,meta):
    row=db.execute('SELECT payload FROM bn_validity WHERE kind=? AND topic=?',('instrument',name)).fetchone()
    return topic_gate_values(name,cut,meta,row[0] if row else None,
        bool(db.execute('SELECT 1 FROM bn_unprocessed WHERE topic=? AND resolved_at IS NULL LIMIT 1',(name,)).fetchone()),
        bool(db.execute('SELECT 1 FROM metadata WHERE kind=?',('quiet_reverify:'+name,)).fetchone()))


class Store(BaseStore):
    def enable_health(self):
        with self.db() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS bn_validity(kind TEXT,topic TEXT,payload TEXT NOT NULL,
                PRIMARY KEY(kind,topic));
              CREATE TABLE IF NOT EXISTS bn_counts(epoch TEXT,topic TEXT,committed INTEGER DEFAULT 0,
                duplicate INTEGER DEFAULT 0,invalid INTEGER DEFAULT 0,unprocessed_recorded INTEGER DEFAULT 0,
                revisions_changed INTEGER DEFAULT 0,PRIMARY KEY(epoch,topic));
              CREATE TABLE IF NOT EXISTS bn_keys(epoch TEXT,topic TEXT,open_time INTEGER,event_time INTEGER,
                payload_sha256 TEXT,PRIMARY KEY(epoch,topic,open_time,event_time));
              CREATE TABLE IF NOT EXISTS bn_unprocessed(id INTEGER PRIMARY KEY,epoch TEXT,topic TEXT,
                received_at INTEGER,open_time INTEGER,event_time INTEGER,payload_sha256 TEXT,reason TEXT,category TEXT,resolved_at INTEGER);
              CREATE TABLE IF NOT EXISTS bn_protocol_invalid(id INTEGER PRIMARY KEY,epoch TEXT,
                received_at INTEGER,payload_sha256 TEXT,reason TEXT);
              CREATE TABLE IF NOT EXISTS bn_publications(intent_id TEXT PRIMARY KEY,epoch TEXT,
                payload_hash TEXT,scope_hash TEXT,outcome TEXT NOT NULL);
            ''')
            if 'event_time' not in {r[1] for r in db.execute('PRAGMA table_info(bn_unprocessed)')}:
                db.execute('ALTER TABLE bn_unprocessed ADD COLUMN event_time INTEGER')
            if 'outcome' not in {r[1] for r in db.execute('PRAGMA table_info(bn_publications)')}:
                db.execute("ALTER TABLE bn_publications ADD COLUMN outcome TEXT NOT NULL DEFAULT 'UNKNOWN'")
            contract=HEALTH_CONTROL_CONTRACT if getattr(self,'control',None) else CONTRACT
            db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('health_contract',contract))

    def begin_health(self,epoch,names,started):
        with self.db() as db:
            self.check_epoch(db,epoch)
            db.execute('DELETE FROM bn_validity')
            db.execute('DELETE FROM bn_keys')
            for key,value in [('health_epoch',epoch),('health_epoch_started',str(started)),('health_phase','ACTIVE')]:
                db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',(key,value))
            db.executemany('INSERT INTO bn_counts(epoch,topic) VALUES(?,?)',[(epoch,n) for n in names])

    def metadata(self):
        with self.db(readonly=True) as db:return dict(db.execute('SELECT key,value FROM meta'))

    def save_proof(self,value):
        name=topic(value['symbol'],value['interval'])
        with self.db() as db:
            self.check_epoch(db,value['epoch'])
            db.execute('INSERT OR REPLACE INTO bn_validity VALUES(?,?,?)',
                       (value['kind'],name,json.dumps(value,sort_keys=True)))

    def get_proof(self,kind,name):
        with self.db(readonly=True) as db:
            row=db.execute('SELECT payload FROM bn_validity WHERE kind=? AND topic=?',(kind,name)).fetchone()
            return json.loads(row[0]) if row else None

    def instrument_batch(self,epoch,names,body,requested,received):
        meta=self.metadata();self._times(meta,requested,received)
        if meta['epoch']!=epoch:raise ValueError('BINANCE_METADATA_EPOCH')
        if not isinstance(body,dict) or not isinstance(body.get('symbols'),list):
            raise ValueError('BINANCE_EXCHANGE_INFO_SHAPE')
        symbols={}
        for row in body['symbols']:
            symbol=row.get('symbol')
            if symbol in symbols:raise ValueError('BINANCE_EXCHANGE_INFO_DUPLICATE')
            symbols[symbol]=row
        expected={split_topic(n)[0] for n in names}
        if set(symbols)!=expected:raise ValueError('BINANCE_EXCHANGE_INFO_SCOPE')
        values=[]
        for name in names:
            symbol,_=split_topic(name);row=symbols[symbol]
            if (row.get('baseAsset')+'USDT'!=symbol or row.get('quoteAsset')!='USDT'
                    or row.get('symbol')!=symbol):raise ValueError('BINANCE_METADATA_IDENTITY')
            good=row.get('status')=='TRADING' and row.get('isSpotTradingAllowed') is True
            if getattr(self,'control',None) is not None:
                filters=row.get('filters')
                if (not isinstance(filters,list) or not filters
                        or any(not isinstance(f,dict) or not isinstance(f.get('filterType'),str) for f in filters)
                        or len({f['filterType'] for f in filters})!=len(filters)):
                    raise ValueError('BINANCE_METADATA_FILTERS_INVALID')
                # Preserve exchange filter values verbatim in the evidence
                # digest; do not invent or change trading thresholds.
                required={'PRICE_FILTER','LOT_SIZE'}
                kinds={f['filterType'] for f in filters}
                if not required<=kinds or not kinds & {'MIN_NOTIONAL','NOTIONAL'}:
                    raise ValueError('BINANCE_METADATA_FILTERS_INCOMPLETE')
            values.append(proof('instrument',name,meta,'OFFICIAL_BINANCE_SPOT_REST_EXCHANGE_INFO',
                requested,received,requested+FRESH_MS,'VALID' if good else 'INVALID',
                None if good else 'SYMBOL_NOT_TRADING_SPOT',trading_status=row.get('status'),
                instrument_sha256=payload_hash(row)))
        with self.db(write_kind='directory_proof') as db:
            self.check_epoch(db,epoch)
            db.executemany('INSERT OR REPLACE INTO bn_validity VALUES(?,?,?)',
                [('instrument',topic(v['symbol'],v['interval']),json.dumps(v,sort_keys=True)) for v in values])

    @staticmethod
    def _times(meta,requested,received):
        if (type(requested) is not int or type(received) is not int
                or not int(meta['health_epoch_started'])<=requested<=received<=requested+FRESH_MS):
            raise ValueError('BINANCE_VERIFICATION_TIME_INVALID')

    def _record(self,db,epoch,name,message,received,reason,category):
        opened=message.get('k',{}).get('t')
        if type(opened) is not int:opened=None
        event=message.get('E')
        if type(event) is not int:event=None
        db.execute('INSERT INTO bn_unprocessed(epoch,topic,received_at,open_time,event_time,payload_sha256,reason,category) VALUES(?,?,?,?,?,?,?,?)',
                   (epoch,name,received,opened,event,payload_hash(message),reason,category))
        db.execute('UPDATE topics SET gap=1,error=? WHERE topic=?',(reason,name))
        db.execute('UPDATE bn_counts SET '+category+'='+category+'+1 WHERE epoch=? AND topic=?',(epoch,name))

    def unprocessed(self,epoch,observations,reason):
        with self.db(write_kind='ws_receipts') as db:
            self.check_epoch(db,epoch)
            for message,received in observations:
                name=topic(message['s'],message['k']['i'])
                self._record(db,epoch,name,message,received,reason,'unprocessed_recorded')

    def ingest_accounted(self,epoch,observations):
        """Exactly one durable outcome per market frame; control frames are excluded."""
        results=[]
        with self.db(write_kind='ws_receipts') as db:
            self.check_epoch(db,epoch)
            for message,received in observations:
                name=topic(message['s'],message['k']['i']);k=message['k']
                try:
                    if type(k.get('t')) is not int or type(message.get('E')) is not int:
                        raise ValueError('BINANCE_EVENT_KEY_INVALID')
                    key=(epoch,name,k['t'],message['E']);digest=payload_hash(message)
                    prior=db.execute('SELECT payload_sha256 FROM bn_keys WHERE epoch=? AND topic=? AND open_time=? AND event_time=?',key).fetchone()
                    if prior:
                        if prior[0]!=digest:raise ValueError('BINANCE_EVENT_KEY_CONFLICT')
                        db.execute('UPDATE bn_counts SET duplicate=duplicate+1 WHERE epoch=? AND topic=?',(epoch,name))
                        results.append('duplicate');continue
                    state=db.execute('SELECT ws_ts FROM topics WHERE topic=?',(name,)).fetchone()
                    if state is None or message['E']<=state[0]:raise ValueError('BINANCE_OLD_UNVERIFIABLE_EVENT')
                    # Roll back a semantically rejected observation, retaining its audit separately.
                    db.execute('SAVEPOINT observation')
                    try:changed=super()._ingest_ws(db,epoch,message,received)
                    except (ValueError,KeyError,TypeError,OverflowError):
                        db.execute('ROLLBACK TO observation');raise
                    finally:db.execute('RELEASE observation')
                    db.execute('INSERT INTO bn_keys VALUES(?,?,?,?,?)',(*key,digest))
                    db.execute('DELETE FROM bn_keys WHERE epoch=? AND topic=? AND event_time<?',
                               (epoch,name,message['E']-180000))
                    db.execute('UPDATE bn_counts SET committed=committed+1,revisions_changed=revisions_changed+? WHERE epoch=? AND topic=?',
                               (int(changed),epoch,name))
                    results.append('committed')
                except (ValueError,KeyError,TypeError,OverflowError) as error:
                    reason=str(error) if re.fullmatch('[A-Z0-9_]+',str(error)) else 'BINANCE_INVALID_MARKET_FRAME'
                    self._record(db,epoch,name,message,received,reason,'invalid');results.append('invalid')
        return results

    def ingest_rest(self,epoch,name,rows,requested,received):
        self._times(self.metadata(),requested,received)
        super().ingest_rest(epoch,name,rows,requested,received)
        with self.db() as db:
            self.check_epoch(db,epoch)
            # The new authoritative window is after each covered discarded receipt.
            # A crash between bar commit and this update leaves consumers blocked.
            db.executemany('''UPDATE bn_unprocessed SET resolved_at=? WHERE topic=? AND resolved_at IS NULL
                       AND received_at<=? AND open_time=?''',
                       [(received,name,requested,row[0]) for row in rows])

    def protocol_invalid(self,epoch,received,message,reason):
        with self.db() as db:
            self.check_epoch(db,epoch)
            db.execute('INSERT INTO bn_protocol_invalid(epoch,received_at,payload_sha256,reason) VALUES(?,?,?,?)',
                       (epoch,received,payload_hash(message),reason))

    def verify_quiet(self,epoch,name,rows,requested,received):
        meta=self.metadata();self._times(meta,requested,received)
        if meta['epoch']!=epoch:raise ValueError('BINANCE_QUIET_EPOCH')
        _,interval=split_topic(name);step=STEPS[interval]
        rows=[valid_row(row,interval) for row in rows]
        count=len(rows)
        minimum=15 if interval=='1d' else LIMITS[interval]
        if not minimum<=count<=LIMITS[interval]:raise ValueError('BINANCE_QUIET_WINDOW_INVALID')
        expected=sorted((requested//step-i)*step for i in range(count))
        if [row[0] for row in rows]!=expected:raise ValueError('BINANCE_QUIET_WINDOW_INVALID')
        closed=(requested//step-1)*step
        remote=next(row for row in rows if row[0]==closed)
        with self.db(readonly=True) as db:
            local=db.execute('SELECT row_json,confirmed FROM bars WHERE topic=? AND start=?',(name,closed)).fetchone()
        status=self.get_proof('instrument',name)
        def comparable(row):
            return [row[i] if i in (0,6,8) else Decimal(str(row[i])) for i in range(11)]
        same=bool(local and local['confirmed'] and comparable(json.loads(local['row_json']))==comparable(remote))
        good=same and status is not None and valid_proof(status,'instrument',name,meta,received)
        reason=None if good else ('CLOSED_BAR_MISMATCH' if not same else 'TRADING_STATUS_UNVERIFIED')
        value=proof('quiet',name,meta,REST_SOURCE,requested,received,min(requested+FRESH_MS,(requested//step+1)*step-1),
                    'VALID' if good else 'UNKNOWN',reason,comparison='SAME_CLOSED_NATIVE_BAR',closed_start=closed)
        if good and not valid_proof(value,'quiet',name,meta,received):
            good=False
            value.update(result='UNKNOWN',reason='VERIFICATION_EXPIRED_OR_NATIVE_BOUNDARY')
        with self.db() as db:
            self.check_epoch(db,epoch)
            db.execute('INSERT OR REPLACE INTO bn_validity VALUES(?,?,?)',('quiet',name,json.dumps(value,sort_keys=True)))
            if not same:
                db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)',('quiet_reverify:'+name,str(received)))
            elif good:
                db.execute('DELETE FROM metadata WHERE kind=?',('quiet_reverify:'+name,))
        if not same:
            # Explicit repair, preserving REST source and the current WS requirement.
            self.ingest_rest(epoch,name,rows,requested,received)
        return value

    def counts(self,epoch):
        with self.db(readonly=True) as db:
            return {r['topic']:dict(r) for r in db.execute('SELECT * FROM bn_counts WHERE epoch=?',(epoch,))}

    def store_health(self,value):
        with self.db() as db:
            meta=dict(db.execute('SELECT key,value FROM meta'))
            if meta.get('epoch')!=value['epoch']:raise ValueError('STALE_SESSION')
            if value.get('service_status')!='STOPPED':
                self.check_epoch(db,value['epoch'])
                if meta.get('health_phase')!='ACTIVE':raise ValueError('HEALTH_NOT_ACTIVE')
            else:
                db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('health_phase','STOPPED'))
            db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)',(CONTRACT,json.dumps(value,sort_keys=True)))

    def start_draining(self,epoch):
        # Revoke publication atomically before socket/HTTP cleanup. Keep the
        # epoch writable only to finish bounded receipt accounting, then disconnect.
        control=getattr(self,'control',None)
        if control:
            try:control.begin_draining(epoch)
            except Exception:
                # Do not leave old ACTIVE reads available for the socket/HTTP
                # shutdown deadline when the revocation write cannot commit.
                control.latch_stop('CONTROL_DRAINING_UNCONFIRMED')
                raise
        with self.db(write_kind='draining') as db:
            self.check_epoch(db,epoch)
            db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('health_phase','DRAINING'))
            db.execute('DELETE FROM bn_validity')
            row=db.execute('SELECT payload FROM metadata WHERE kind=?',(CONTRACT,)).fetchone()
            value=json.loads(row[0]) if row else {}
            value.update(contract=CONTRACT,epoch=epoch,service_status='DRAINING',data_status='UNKNOWN')
            db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)',(CONTRACT,json.dumps(value,sort_keys=True)))

    def connection_proofs(self,protocol,shards,now):
        meta=self.metadata();values=[]
        for name,index in protocol.topic_shards.items():
            shard=shards[index];observed=shard['observed_at']
            good=observed>0 and 0<=now-observed<=FRESH_MS
            values.append(proof('connection',name,meta,'OFFICIAL_BINANCE_SPOT_WS_CONNECTION',
                observed,now,observed+FRESH_MS,'VALID' if good else 'UNKNOWN',
                None if good else 'SHARD_EVIDENCE_EXPIRED',evidence=shard['evidence'],shard=index))
        with self.db() as db:
            self.check_epoch(db,protocol.epoch)
            db.executemany('INSERT OR REPLACE INTO bn_validity VALUES(?,?,?)',
                [('connection',topic(v['symbol'],v['interval']),json.dumps(v,sort_keys=True)) for v in values])

    def read_health(self):
        with self.db(readonly=True) as db:
            row=db.execute('SELECT payload FROM metadata WHERE kind=?',(CONTRACT,)).fetchone()
            return json.loads(row[0]) if row else None
