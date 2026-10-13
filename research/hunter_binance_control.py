"""Explicit, independent Binance HTTP coordination. Never a market-data writer.

Provisioning is offline and refuses unresolved legacy ownership. Every online
transaction is short: no HTTP, market DB, Future or blocking flock is awaited
while holding this database. A separate preprovisioned halt latch fails readers
closed when a control mutation cannot be confirmed.
"""
import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid

FORMAT = 'binance_control_v1'
MAX_BODY = 1024 * 1024
MAX_WEIGHT_MINUTE = 600  # Existing 100 ms per weight pacing, now also window bounded.


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def boot_id():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


class ControlError(ValueError):
    pass


class ScopeQuarantined(ControlError):
    pass


class AdmissionClosed(ControlError):
    pass


class NativeLock:
    """The original open file description is the ownership capability."""
    def __init__(self, path, *, create=False, shared=False):
        import fcntl
        self.path = Path(path)
        flags = (os.O_RDONLY if shared else os.O_RDWR) | getattr(os, 'O_NOFOLLOW', 0)
        if create:
            flags |= os.O_CREAT | os.O_EXCL
        self.fd = os.open(self.path, flags, 0o660)
        try:
            stat = os.fstat(self.fd)
            self.identity = (stat.st_dev, stat.st_ino)
            fcntl.flock(self.fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except BaseException as error:
            if isinstance(error,BlockingIOError):
                stat=os.fstat(self.fd)
                error.owner_identity=(stat.st_dev,stat.st_ino)
                error.owner_nlink=stat.st_nlink
            os.close(self.fd)
            self.fd = None
            raise

    def verify(self):
        if self.fd is None:
            raise ControlError('CONTROL_OWNER_CLOSED')
        a, b = os.fstat(self.fd), os.stat(self.path, follow_symlinks=False)
        if self.identity != (a.st_dev, a.st_ino) or self.identity != (b.st_dev, b.st_ino) or a.st_nlink != 1:
            raise ControlError('CONTROL_OWNER_IDENTITY_CHANGED')

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class Owner:
    def __init__(self, control, token, lock, request_id, attempt, context):
        self.control, self.token, self.lock = control, token, lock
        self.request_id, self.attempt, self.context = request_id, attempt, context
        self.go = self.complete = self.reaped = self.not_sent = False
        self.status = None
        self.quarantine_id = None
        self.retired = False
        self.cleanup_attempts = 0
        self.cleanup_errors = []

    def verify(self):
        self.lock.verify()
        if self.retired:
            raise ControlError('CONTROL_OWNER_ALREADY_RETIRED')


class Control:
    def __init__(self, path, *, clock=time.monotonic, wall=time.time,
                 boot=None, lock_factory=NativeLock, readonly=False):
        self.path = Path(path)
        if not self.path.is_absolute() or not self.path.is_file():
            raise ControlError('CONTROL_EXISTING_ABSOLUTE_DATABASE_REQUIRED')
        self.clock, self.wall = clock, wall
        self.boot = boot if boot is not None else boot_id()
        self.lock_factory = lock_factory
        self.readonly = readonly
        self.gate_path = self.path.with_name(self.path.name + '.gate')
        self._owners, self._owners_lock = {}, threading.Lock()
        self._writer_lock = threading.RLock()
        self._admission_local = threading.local()
        self._latched_guard = None
        self._read_latch()
        with self._db(readonly=True) as db:
            meta = dict(db.execute('SELECT key,value FROM meta'))
        if meta.get('format') != FORMAT or meta.get('boot') != self.boot:
            raise ControlError('CONTROL_FORMAT_OR_BOOT_REVIEW_REQUIRED')

    @classmethod
    def reader(cls,path,**kwargs):
        return cls(path,readonly=True,**kwargs)

    @classmethod
    def provision(cls, path, legacy_path, **kwargs):
        """Caller must hold the service's exclusive OFFLINE provisioning lock."""
        path, legacy_path = Path(path), Path(legacy_path)
        if (not path.is_absolute() or not legacy_path.is_absolute()
                or not path.parent.is_dir() or path.exists() or not legacy_path.is_file()):
            raise ControlError('CONTROL_PROVISION_PRECONDITION')
        clock = kwargs.get('clock', time.monotonic)
        wall = kwargs.get('wall', time.time)
        boot = kwargs.get('boot') or boot_id()
        now_ns, now_ms = int(clock()*1e9), int(wall()*1000)
        # This is the only control operation allowed to inspect the legacy DB.
        # Its transaction blocks legacy admission during this offline snapshot.
        with contextlib.closing(sqlite3.connect(legacy_path.as_uri()+'?mode=rw', uri=True, timeout=.25)) as old:
            old.row_factory = sqlite3.Row
            old.execute('BEGIN IMMEDIATE')
            try:
                b = dict(old.execute('SELECT * FROM budget WHERE id=1').fetchone())
                domain = old.execute('SELECT boot FROM budget_clock WHERE id=1').fetchone()
                owners = sum(old.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in ('requests','leases'))
                if owners or b['halt']:
                    raise ControlError('CONTROL_LEGACY_OWNER_OR_HALT_REQUIRES_REVIEW')
                if domain and domain[0] != boot:
                    raise ControlError('CONTROL_LEGACY_BOOT_REQUIRES_REVIEW')
                if not domain and any(b[k] for k in ('next_ms','last_ms','cooldown_ms')):
                    raise ControlError('CONTROL_LEGACY_CLOCK_REQUIRES_REVIEW')
                if b['cooldown_ms'] > now_ns//1_000_000:
                    raise ControlError('CONTROL_LEGACY_COOLDOWN_REQUIRES_REVIEW')
                gate = path.with_name(path.name+'.gate')
                with gate.open('xb') as f:
                    f.write(b'0'); f.flush(); os.fsync(f.fileno())
                with contextlib.closing(sqlite3.connect(path)) as db:
                    db.executescript('''
                    CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                    CREATE TABLE budget(id INTEGER PRIMARY KEY CHECK(id=1),window_ms INTEGER,window_end_ns INTEGER,
                        used INTEGER,next_ns INTEGER,cooldown_ns INTEGER,halt TEXT,charged_total INTEGER,
                        revocation INTEGER DEFAULT 0);
                    CREATE TABLE owners(token TEXT PRIMARY KEY,request_id TEXT,attempt INTEGER,
                        epoch TEXT,job_key TEXT,scope TEXT,scope_hash TEXT,endpoint TEXT,
                        weight INTEGER,deadline_ns INTEGER,retire_deadline_ns INTEGER,
                        state TEXT,dev INTEGER,inode INTEGER);
                    CREATE TABLE requests(token TEXT PRIMARY KEY,denied INTEGER DEFAULT 0);
                    CREATE TABLE attempts(token TEXT PRIMARY KEY,request_id TEXT,attempt INTEGER,
                        epoch TEXT,job_key TEXT,scope TEXT,endpoint TEXT,weight INTEGER,
                        admitted_ns INTEGER,terminal TEXT,evidence TEXT);
                    CREATE TABLE calls(id TEXT PRIMARY KEY,epoch TEXT,job_key TEXT,scope TEXT,
                        started_ns INTEGER,terminal TEXT,evidence TEXT);
                    CREATE TABLE quarantines(id TEXT PRIMARY KEY,token TEXT UNIQUE,epoch TEXT,
                        job_key TEXT,scope TEXT,reason TEXT,state TEXT,requeue_state TEXT);
                    CREATE TABLE versions(topic TEXT PRIMARY KEY,version INTEGER NOT NULL);
                    CREATE TABLE publications(id TEXT PRIMARY KEY,epoch TEXT,job_key TEXT,scope TEXT,
                        scope_hash TEXT,payload_hash TEXT,payload TEXT,recovery_id TEXT,state TEXT,receipt TEXT,
                        created_seq INTEGER UNIQUE NOT NULL);
                    CREATE INDEX publications_epoch_state ON publications(epoch,state);
                    CREATE INDEX publications_supersede ON publications(epoch,job_key,scope_hash,state);
                    CREATE INDEX quarantines_epoch_state ON quarantines(epoch,state);
                    CREATE INDEX quarantines_requeue ON quarantines(epoch,job_key,requeue_state);
                    CREATE INDEX calls_epoch ON calls(epoch);
                    CREATE INDEX attempts_epoch ON attempts(epoch);
                    ''')
                    prior_spend = bool(b['last_ms'] or b['next_ms'])
                    # Legacy had no window total. Charge the remainder of this
                    # window conservatively rather than inventing a fresh budget.
                    used = MAX_WEIGHT_MINUTE if prior_spend else 0
                    db.execute('INSERT INTO budget(id,window_ms,window_end_ns,used,next_ns,cooldown_ns,halt,charged_total) VALUES(1,?,?,?,?,?,NULL,?)',
                        (now_ms//60000*60000,now_ns+(60000-now_ms%60000)*1_000_000,
                         used,max(now_ns,b['next_ms']*1_000_000),0,used))
                    db.executemany('INSERT INTO meta VALUES(?,?)',
                        [('format',FORMAT),('boot',boot),('phase','OFFLINE'),
                         ('legacy_path',str(legacy_path)),('epoch',''),('source_head',''),
                         ('manifest_sha256',''),('roster_hash',''),('publication_seq','0')])
                    db.commit()
            finally:
                old.rollback()
        return cls(path, **kwargs)

    def _read_latch(self):
        try:
            if self.gate_path.read_bytes() != b'0':
                raise ControlError('CONTROL_GLOBAL_HALT_LATCH')
        except OSError as error:
            raise ControlError('CONTROL_GATE_UNREADABLE') from error

    def latch(self, reason):
        """Independent from SQLite; never clear this latch automatically."""
        if self.readonly:raise ControlError('CONTROL_READONLY_MUTATION')
        try:
            with self.gate_path.open('r+b', buffering=0) as f:
                f.write(b'1'); os.fsync(f.fileno())
        except BaseException:
            # A held exclusive guard makes all final read fences fail closed
            # even if the filesystem cannot confirm the durable latch write.
            try:
                self._latched_guard = self.lock_factory(self.gate_path)
            except BaseException:
                pass
            with self._owners_lock:
                for owner in self._owners.values():
                    owner.lock.close()  # Persistent owner rows become orphan gates.
        raise ControlError(reason)

    def latch_stop(self,reason):
        return self.latch(reason)

    @contextlib.contextmanager
    def _db(self, readonly=False, timeout=.25):
        if self.readonly and not readonly:raise ControlError('CONTROL_READONLY_MUTATION')
        if not math.isfinite(timeout) or timeout < 0 or timeout > 10:
            raise ValueError('CONTROL_DB_TIMEOUT_INVALID')
        # Serialize the process's short writes before entering SQLite. This
        # lock never encloses market work, HTTP, callbacks or Future waits.
        held=False;db=None;phase='SERIALIZE'
        try:
            if not readonly:
                held=self._writer_lock.acquire(timeout=min(timeout,.25))
                if not held:raise sqlite3.OperationalError('database is locked')
            phase='CONNECT'
            db = sqlite3.connect(self.path.as_uri()+('?mode=ro' if readonly else '?mode=rw'),
                                 uri=True, timeout=min(timeout,.25))
            db.row_factory = sqlite3.Row
            phase='BEGIN'
            db.execute('BEGIN' if readonly else 'BEGIN IMMEDIATE')
            phase='BODY'
            yield db
            phase='COMMIT'
            db.commit()
        except BaseException as error:
            if isinstance(error,sqlite3.Error):
                error.control_phase=phase
            if db is not None:db.rollback()
            raise
        finally:
            if db is not None:db.close()
            if held:self._writer_lock.release()

    def _meta(self, db, epoch=None):
        meta = dict(db.execute('SELECT key,value FROM meta'))
        if meta.get('boot') != self.boot:
            raise ControlError('CONTROL_BOOT_MISMATCH')
        if epoch is not None and meta.get('epoch') != epoch:
            raise ControlError('CONTROL_EPOCH_MISMATCH')
        if meta.get('phase') != 'ACTIVE':
            raise AdmissionClosed('CONTROL_EPOCH_NOT_ACTIVE')
        return meta

    def begin_epoch(self, epoch, source_head, roster_hash, *, manifest_sha256, names):
        names = sorted(set(names))
        if (not names or digest(names) != roster_hash or not re.fullmatch('[a-f0-9]{32}',epoch)
                or not re.fullmatch('[a-f0-9]{40}',source_head)
                or not re.fullmatch('[a-f0-9]{64}',manifest_sha256)):
            raise ValueError('CONTROL_EPOCH_BINDING_INVALID')
        self._read_latch()
        with self._db() as db:
            if db.execute('SELECT 1 FROM owners LIMIT 1').fetchone():
                raise ControlError('CONTROL_PRIOR_OWNER_REQUIRES_REVIEW')
            if db.execute("SELECT 1 FROM quarantines WHERE state!='RESOLVED' LIMIT 1").fetchone():
                raise ControlError('CONTROL_PRIOR_QUARANTINE_REQUIRES_REVIEW')
            db.execute("UPDATE publications SET state='DISCARDED_EPOCH' WHERE state='PREPARED' AND epoch!=?",(epoch,))
            db.executemany('INSERT OR REPLACE INTO meta VALUES(?,?)',
                [('epoch',epoch),('phase','ACTIVE'),('source_head',source_head),
                 ('manifest_sha256',manifest_sha256),('roster_hash',roster_hash)])
            db.execute('DELETE FROM versions')
            db.executemany('INSERT INTO versions VALUES(?,0)',[(n,) for n in names])

    def end_epoch(self, epoch, reason):
        try:
            with self._db() as db:
                if dict(db.execute('SELECT key,value FROM meta')).get('epoch')!=epoch:
                    raise ControlError('CONTROL_EPOCH_MISMATCH')
                db.execute("UPDATE meta SET value='STOPPED' WHERE key='phase'")
                db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('stop_reason',str(reason)))
        except (sqlite3.Error, OSError) as error:
            self.latch('CONTROL_END_EPOCH_UNCONFIRMED')

    def begin_draining(self,epoch):
        with self._db() as db:
            meta=dict(db.execute('SELECT key,value FROM meta'))
            if meta.get('epoch')!=epoch:raise ControlError('CONTROL_EPOCH_MISMATCH')
            db.execute("UPDATE meta SET value='DRAINING' WHERE key='phase'")

    def _scope(self, scope):
        if not isinstance(scope,(list,tuple)) or not scope or any(not isinstance(n,str) for n in scope):
            raise ValueError('CONTROL_SCOPE_REQUIRED')
        return sorted(set(scope))

    def _owner_path(self, token):
        return self.path.with_name(self.path.name+'.owner-'+token+'.lock')

    def _check_owners(self, db):
        for row in db.execute('SELECT * FROM owners'):
            token = row['token']
            with self._owners_lock:
                owner = self._owners.get(token)
            if owner:
                owner.verify()
                if owner.lock.identity != (row['dev'],row['inode']):
                    raise ControlError('CONTROL_OWNER_BINDING_MISMATCH')
            else:
                try:
                    probe = self.lock_factory(self._owner_path(token),shared=True)
                except BlockingIOError as error:
                    try:current=os.stat(self._owner_path(token),follow_symlinks=False)
                    except OSError as missing:raise ControlError('CONTROL_OWNER_UNVERIFIABLE') from missing
                    if (getattr(error,'owner_identity',None)!=(row['dev'],row['inode'])
                            or getattr(error,'owner_nlink',None)!=1
                            or (current.st_dev,current.st_ino)!=(row['dev'],row['inode']) or current.st_nlink!=1):
                        raise ControlError('CONTROL_OWNER_BINDING_MISMATCH') from error
                    continue
                except OSError as error:
                    raise ControlError('CONTROL_OWNER_UNVERIFIABLE') from error
                else:
                    probe.close()
                    raise ControlError('CONTROL_ORPHAN_REQUIRES_REVIEW')

    def _blocked(self, db, scope, epoch, recovery_id=None):
        blocked = []
        rows=list(db.execute("SELECT * FROM quarantines WHERE epoch=? AND state!='RESOLVED'",(epoch,)))
        recovery=next((q for q in rows if q['id']==recovery_id and q['requeue_state'] in ('PENDING','CLAIMED')
                       and set(json.loads(q['scope']))==set(scope)),None)
        for q in rows:
            if set(scope).intersection(json.loads(q['scope'])):
                if recovery and q['state']=='REVERIFY_PENDING':
                    continue
                blocked.append(q['id'])
        now = int(self.clock()*1e9)
        for o in db.execute("SELECT * FROM owners WHERE state='RETIRING'"):
            if o['retire_deadline_ns'] <= now and set(scope).intersection(json.loads(o['scope'])):
                blocked.append(o['token'])
        return blocked

    def fence(self, names, epoch=None):
        scope = self._scope(names)
        self._read_latch()
        guard = self.lock_factory(self.gate_path,shared=True)
        try:
            with self._db(readonly=True) as db:
                meta = self._meta(db,epoch)
                b = dict(db.execute('SELECT * FROM budget WHERE id=1').fetchone())
                if b['halt']:
                    raise ControlError(b['halt'])
                if int(self.clock()*1e9)<b['cooldown_ns']:
                    raise ControlError('CONTROL_GLOBAL_COOLDOWN')
                self._check_owners(db)
                if self._blocked(db,scope,meta['epoch']):
                    raise ScopeQuarantined('CONTROL_SCOPE_QUARANTINED')
                rows = dict(db.execute('SELECT topic,version FROM versions'))
                if not set(scope) <= rows.keys():
                    raise ControlError('CONTROL_SCOPE_OUTSIDE_ROSTER')
                for p in db.execute("SELECT scope FROM publications WHERE epoch=? AND state IN ('PREPARED','FAILED')",(meta['epoch'],)):
                    if set(scope).intersection(json.loads(p['scope'])):
                        raise ScopeQuarantined('CONTROL_PUBLICATION_PENDING')
                result = dict(epoch=meta['epoch'],source_head=meta['source_head'],
                    manifest_sha256=meta['manifest_sha256'],roster_hash=meta['roster_hash'],
                    budget_version=b['revocation'],
                    versions={n:rows[n] for n in scope})
            self._read_latch()
            return result
        finally:
            guard.close()

    def validate_fence(self, fence, names, epoch=None):
        if self.fence(names,epoch) != fence:
            raise ControlError('CONTROL_READ_FENCE_CHANGED')
        return True

    def status(self, timeout=.05):
        self._read_latch()
        with self._db(readonly=True,timeout=timeout) as db:
            b = dict(db.execute('SELECT * FROM budget WHERE id=1').fetchone())
            meta=dict(db.execute('SELECT key,value FROM meta'))
            b.update({key:meta.get(key) for key in ('epoch','phase','source_head','manifest_sha256','roster_hash')})
            try:
                self._check_owners(db)
            except ControlError as error:
                b['halt'] = b['halt'] or str(error)
            b['cooldown_ms'] = int(self.wall()*1000)+max(0,(b['cooldown_ns']-int(self.clock()*1e9))//1_000_000) if b['cooldown_ns']>int(self.clock()*1e9) else 0
            b['active_owners'] = db.execute('SELECT COUNT(*) FROM owners').fetchone()[0]
            return b

    def can_schedule(self, scope, epoch, requeue_id=None):
        return self.schedule_permissions(epoch,[(scope,requeue_id)])[0]

    def request_scope(self,endpoint,params):
        with self._db(readonly=True) as db:
            names=[r[0] for r in db.execute('SELECT topic FROM versions')]
        if endpoint=='/api/v3/exchangeInfo':
            symbols=set(json.loads(params['symbols']))
            result=sorted(n for n in names if n.split('@kline_')[0].upper() in symbols)
            if {n.split('@kline_')[0].upper() for n in result}!=symbols:
                raise ControlError('CONTROL_DIRECTORY_SCOPE_OUTSIDE_ROSTER')
            return result
        result=[params['symbol'].lower()+'@kline_'+params['interval']]
        if not set(result)<=set(names):raise ControlError('CONTROL_KLINE_SCOPE_OUTSIDE_ROSTER')
        return result

    def schedule_permissions(self,epoch,scopes):
        self._read_latch()
        with self._db(readonly=True) as db:
            self._meta(db,epoch)
            quarantines=[dict(r) for r in db.execute("SELECT * FROM quarantines WHERE epoch=? AND state!='RESOLVED'",(epoch,))]
            expired=[dict(r) for r in db.execute("SELECT * FROM owners WHERE state='RETIRING' AND retire_deadline_ns<=?",(int(self.clock()*1e9),))]
        results=[]
        for scope,recovery_id in scopes:
            scope=set(self._scope(scope))
            allowed_recovery=next((q for q in quarantines if q['id']==recovery_id
                and q['requeue_state'] in ('PENDING','CLAIMED') and set(json.loads(q['scope']))==scope),None)
            blocked=any(scope.intersection(json.loads(q['scope'])) and not
                (allowed_recovery and q['state']=='REVERIFY_PENDING') for q in quarantines)
            blocked=blocked or any(scope.intersection(json.loads(o['scope'])) for o in expired)
            results.append(not blocked)
        return results

    def begin_call(self, context):
        identity = uuid.uuid4().hex
        with self._db() as db:
            meta=dict(db.execute('SELECT key,value FROM meta'))
            if meta.get('boot')!=self.boot or meta.get('epoch')!=context['epoch']:
                raise ControlError('CONTROL_CALL_BINDING_MISMATCH')
            if meta.get('phase') not in ('ACTIVE','DRAINING'):
                raise AdmissionClosed('CONTROL_EPOCH_NOT_ACTIVE')
            db.execute('INSERT INTO calls VALUES(?,?,?,?,?,NULL,NULL)',(identity,context['epoch'],
                canonical(context['job_key']),canonical(self._scope(context['scope'])),int(self.clock()*1e9)))
        return identity

    def end_call(self, request_id, terminal, evidence):
        with self._db() as db:
            row = db.execute('SELECT terminal FROM calls WHERE id=?',(request_id,)).fetchone()
            if row is None:
                raise ControlError('CONTROL_CALL_UNKNOWN')
            if row[0] is None:
                db.execute('UPDATE calls SET terminal=?,evidence=? WHERE id=?',(terminal,canonical(evidence),request_id))

    def admit(self, context, request_id, attempt, endpoint, weight, deadline):
        self._read_latch()
        scope, epoch = self._scope(context['scope']), context['epoch']
        if type(weight) is not int or not 1 <= weight <= MAX_WEIGHT_MINUTE:
            raise ValueError('CONTROL_WEIGHT_INVALID')
        # Waiting for pacing or a slot must not create lock files or compete
        # with cleanup/publication writers. This snapshot only rejects; the
        # write transaction below revalidates every admission condition.
        self._admission_local.delay = .05
        with self._db(readonly=True) as db:
            self._meta(db,epoch); self._check_owners(db)
            b = dict(db.execute('SELECT * FROM budget WHERE id=1').fetchone())
            now,wall=int(self.clock()*1e9),int(self.wall()*1000)
            window,window_end,used=self._budget_window(b,now,wall)
            wait_until=max(b['next_ns'],b['cooldown_ns'])
            if used+weight>MAX_WEIGHT_MINUTE:wait_until=max(wait_until,window_end)
            if now<wait_until or used+weight>MAX_WEIGHT_MINUTE:
                self._admission_local.delay=max(.02,min(.25,(wait_until-now)/1e9))
                return None
            if (db.execute('SELECT COUNT(*) FROM owners').fetchone()[0]>=4
                    or self._blocked(db,scope,epoch,context.get('requeue_id'))):
                return None
        token = uuid.uuid4().hex
        lock = self.lock_factory(self._owner_path(token),create=True)
        owner = Owner(self,token,lock,request_id,attempt,context)
        try:
            with self._db() as db:
                self._meta(db,epoch); self._check_owners(db)
                recovery = context.get('requeue_id')
                if self._blocked(db,scope,epoch,recovery):
                    return None
                if recovery:
                    q = db.execute('SELECT * FROM quarantines WHERE id=?',(recovery,)).fetchone()
                    if not q or q['epoch']!=epoch or q['job_key']!=canonical(context['job_key']) or q['requeue_state']!='CLAIMED':
                        raise ControlError('CONTROL_REQUEUE_NOT_CLAIMED')
                known = {r[0] for r in db.execute('SELECT topic FROM versions')}
                if not set(scope)<=known:
                    raise ControlError('CONTROL_SCOPE_OUTSIDE_ROSTER')
                b = dict(db.execute('SELECT * FROM budget WHERE id=1').fetchone())
                now, wall = int(self.clock()*1e9),int(self.wall()*1000)
                window,window_end,used=self._budget_window(b,now,wall)
                if now < max(b['next_ns'],b['cooldown_ns']) or used+weight>MAX_WEIGHT_MINUTE or db.execute('SELECT COUNT(*) FROM owners').fetchone()[0]>=4:
                    return None
                db.execute('UPDATE budget SET window_ms=?,window_end_ns=?,used=?,next_ns=?,charged_total=charged_total+? WHERE id=1',
                    (window,window_end,used+weight,now+weight*100_000_000,weight))
                db.execute('INSERT INTO owners VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (token,request_id,attempt,epoch,canonical(context['job_key']),canonical(scope),digest(scope),
                     endpoint,weight,int(deadline*1e9),None,'ACTIVE',*lock.identity))
                db.execute('INSERT INTO requests(token) VALUES(?)',(token,))
                db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,NULL,NULL)',
                    (token,request_id,attempt,epoch,canonical(context['job_key']),canonical(scope),endpoint,weight,now))
            with self._owners_lock:
                self._owners[token] = owner
            return owner
        finally:
            with self._owners_lock:
                registered = token in self._owners
            if not registered:
                lock.close()
                self._owner_path(token).unlink(missing_ok=True)

    @staticmethod
    def _budget_window(b,now,wall):
        if b['halt']:raise ControlError(b['halt'])
        window=wall//60000*60000
        if window<b['window_ms']:raise ControlError('CONTROL_CLOCK_ROLLBACK')
        reset=window>b['window_ms'] and now>=b['window_end_ns']
        return (window if reset else b['window_ms'],
            now+(60000-wall%60000)*1_000_000 if reset else b['window_end_ns'],
            0 if reset else b['used'])

    def admission_delay(self):
        return getattr(self._admission_local,'delay',.05)

    def before_go(self, owner):
        owner.verify(); self._read_latch()
        with self._db(readonly=True) as db:
            self._meta(db,owner.context['epoch']); self._check_owners(db)
            b = dict(db.execute('SELECT * FROM budget WHERE id=1').fetchone())
            if b['halt'] or int(self.clock()*1e9)<b['cooldown_ns']:
                raise ControlError(b['halt'] or 'CONTROL_ADMISSION_REVOKED')

    def observe(self, owner, event, **fields):
        if event=='GO': owner.go=True
        elif event=='RESPONSE': owner.complete=True;owner.status=fields['status']
        elif event=='REAPED': owner.reaped=True
        elif event=='WEIGHT': self.calibrate(fields['used'],fields['window_ms'])

    def calibrate(self, used, window_ms):
        if type(used) is not int or used<0 or type(window_ms) is not int or window_ms%60000:
            raise ValueError('CONTROL_WEIGHT_HEADER_INVALID')
        with self._db() as db:
            b = dict(db.execute('SELECT * FROM budget WHERE id=1').fetchone())
            # Never advance/reset a local window from a delayed response.
            if window_ms == b['window_ms']:
                db.execute('UPDATE budget SET used=MAX(used,?) WHERE id=1',(used,))

    def failure(self, status, text='', retry_after=1, timeout=.05, **kwargs):
        with self._db(timeout=timeout) as db:
            if status in (403,418):
                db.execute("UPDATE budget SET halt='BINANCE_DENIAL_MANUAL_REVIEW',revocation=revocation+1 WHERE id=1")
                return 'BINANCE_DENIAL_MANUAL_REVIEW'
            if status==429 or status==0 or 500<=status<600:
                seconds = max(1,min(600,retry_after)) if math.isfinite(retry_after) else 60
                db.execute('UPDATE budget SET cooldown_ns=MAX(cooldown_ns,?),revocation=revocation+1 WHERE id=1',
                    (int((self.clock()+seconds)*1e9),))
                return 'BOUNDED_RETRY'
            return 'NONRETRYABLE_RESPONSE'

    def prepare_retire(self, owner, deadline):
        owner.verify()
        if not owner.reaped and not (owner.not_sent and not owner.go):
            raise ControlError('CONTROL_REAP_PROOF_REQUIRED')
        if not owner.complete and not owner.not_sent:
            raise ControlError('CONTROL_COMPLETION_PROOF_REQUIRED')
        with self._db() as db:
            row = db.execute('SELECT * FROM owners WHERE token=?',(owner.token,)).fetchone()
            if not row or (row['dev'],row['inode'])!=owner.lock.identity:
                raise ControlError('CONTROL_RETIRE_OWNER_MISMATCH')
            db.execute("UPDATE owners SET state='RETIRING',retire_deadline_ns=? WHERE token=?",
                (int(deadline*1e9),owner.token))

    def quarantine(self, owner, reason):
        owner.verify()
        identity = digest([owner.token,'quarantine'])
        with self._db() as db:
            row = db.execute('SELECT * FROM owners WHERE token=?',(owner.token,)).fetchone()
            if not row:
                raise ControlError('CONTROL_QUARANTINE_OWNER_MISSING')
            existing = db.execute('SELECT id FROM quarantines WHERE token=?',(owner.token,)).fetchone()
            if not existing:
                db.execute('INSERT INTO quarantines VALUES(?,?,?,?,?,?,?,?)',
                    (identity,owner.token,row['epoch'],row['job_key'],row['scope'],str(reason),'WAIT_RETIRE','BLOCKED'))
                db.executemany('UPDATE versions SET version=version+1 WHERE topic=?',[(n,) for n in json.loads(row['scope'])])
        owner.quarantine_id = identity
        return identity

    def _evidence(self, owner):
        return dict(go=owner.go,response_complete=owner.complete,process_reaped=owner.reaped,
            not_sent=owner.not_sent,http_status=owner.status,owner_retired=owner.retired,
            cleanup_attempts=owner.cleanup_attempts,cleanup_errors=owner.cleanup_errors)

    def retire(self, owner):
        owner.verify()
        if not owner.reaped and not (owner.not_sent and not owner.go):
            raise ControlError('CONTROL_REAP_PROOF_REQUIRED')
        if not owner.complete and not owner.not_sent:
            raise ControlError('CONTROL_COMPLETION_PROOF_REQUIRED')
        with self._db() as db:
            row = db.execute('SELECT * FROM owners WHERE token=?',(owner.token,)).fetchone()
            if not row or row['state']!='RETIRING' or (row['dev'],row['inode'])!=owner.lock.identity:
                raise ControlError('CONTROL_RETIRE_OWNER_MISMATCH')
            if db.execute('SELECT 1 FROM requests WHERE token=?',(owner.token,)).fetchone() is None:
                raise ControlError('CONTROL_RETIRE_REQUEST_MISSING')
            evidence = dict(self._evidence(owner),owner_retired=True)
            db.execute('DELETE FROM requests WHERE token=?',(owner.token,))
            db.execute('DELETE FROM owners WHERE token=?',(owner.token,))
            # COMMIT itself can cross the prearmed deadline. Revoking on every
            # retirement is the small, provable rule; a held consumer snapshot
            # retries if a relevant HTTP owner retires before its final fence.
            db.executemany('UPDATE versions SET version=version+1 WHERE topic=?',
                [(n,) for n in json.loads(row['scope'])])
            db.execute('UPDATE attempts SET terminal=?,evidence=? WHERE token=?',
                ('QUARANTINED_RETIRED' if owner.quarantine_id else 'RETIRED',canonical(evidence),owner.token))
            if owner.quarantine_id:
                db.execute("UPDATE quarantines SET state='REVERIFY_PENDING',requeue_state='PENDING' WHERE id=?",
                    (owner.quarantine_id,))
        owner.retired=True
        owner.lock.close()
        self._owner_path(owner.token).unlink(missing_ok=True)
        with self._owners_lock:
            self._owners.pop(owner.token,None)

    def retain(self, owner, reason):
        try:
            with self._db() as db:
                db.execute('UPDATE attempts SET terminal=?,evidence=? WHERE token=?',
                    ('RETAINED_MANUAL_REVIEW',canonical(dict(self._evidence(owner),reason=str(reason))),owner.token))
                db.execute('UPDATE budget SET halt=? WHERE id=1',(str(reason),))
        except BaseException:
            self.latch('CONTROL_RETAIN_UNCONFIRMED')
        finally:
            owner.lock.close()  # No owner row or rate charge is removed.

    def claim_requeue(self, epoch, job_key):
        with self._db() as db:
            self._meta(db,epoch)
            row = db.execute("SELECT id FROM quarantines WHERE epoch=? AND job_key=? AND requeue_state='PENDING' ORDER BY id LIMIT 1",
                (epoch,canonical(job_key))).fetchone()
            if row is None:return None
            db.execute("UPDATE quarantines SET requeue_state='CLAIMED' WHERE id=?",(row[0],))
            return row[0]

    def publication_intent(self, epoch, job_key, scope, payload_hash, *, payload=None, recovery_id=None):
        scope = self._scope(scope)
        raw = canonical(payload)
        if len(raw.encode())>MAX_BODY*2 or not re.fullmatch('[a-f0-9]{64}',payload_hash):
            raise ValueError('CONTROL_PUBLICATION_INVALID')
        if payload is not None and digest(payload)!=payload_hash:
            raise ValueError('CONTROL_PUBLICATION_HASH_MISMATCH')
        identity = digest([epoch,job_key,payload_hash])
        with self._db() as db:
            self._meta(db,epoch)
            existing = db.execute('SELECT * FROM publications WHERE id=?',(identity,)).fetchone()
            if existing:
                if existing['scope_hash']!=digest(scope) or existing['recovery_id']!=recovery_id:
                    raise ControlError('CONTROL_PUBLICATION_IDENTITY_COLLISION')
                return identity
            if recovery_id:
                q=db.execute('SELECT * FROM quarantines WHERE id=?',(recovery_id,)).fetchone()
                if not q or q['epoch']!=epoch or q['job_key']!=canonical(job_key) or q['requeue_state']!='CLAIMED':
                    raise ControlError('CONTROL_RECOVERY_PUBLICATION_MISMATCH')
            sequence=int(db.execute("SELECT value FROM meta WHERE key='publication_seq'").fetchone()[0])+1
            db.execute("UPDATE meta SET value=? WHERE key='publication_seq'",(str(sequence),))
            db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,NULL,?)',
                (identity,epoch,canonical(job_key),canonical(scope),digest(scope),payload_hash,raw,recovery_id,'PREPARED',sequence))
            db.executemany('UPDATE versions SET version=version+1 WHERE topic=?',[(n,) for n in scope])
        return identity

    @staticmethod
    def _intent(row):
        value=dict(row);value['intent_id']=value['id']
        for key in ('job_key','scope','payload'):
            value[key]=json.loads(value[key])
        return value

    def get_intent(self, identity):
        with self._db(readonly=True) as db:
            row=db.execute('SELECT * FROM publications WHERE id=?',(identity,)).fetchone()
            if row is None:raise ControlError('CONTROL_INTENT_MISSING')
            return self._intent(row)

    def pending_intents(self, epoch=None):
        with self._db(readonly=True) as db:
            sql="SELECT * FROM publications WHERE state='PREPARED'"
            rows=db.execute(sql+(' AND epoch=?' if epoch is not None else ''),() if epoch is None else (epoch,)).fetchall()
            return [self._intent(r) for r in rows]

    def publication_applied(self, identity, market_receipt):
        return self.reconcile(identity,market_receipt)

    def reject_publication(self,identity,reason):
        with self._db() as db:
            row=db.execute('SELECT state FROM publications WHERE id=?',(identity,)).fetchone()
            if row is None:raise ControlError('CONTROL_INTENT_MISSING')
            if row['state']=='APPLIED':raise ControlError('CONTROL_REJECT_APPLIED_INTENT')
            db.execute("UPDATE publications SET state='FAILED',receipt=? WHERE id=?",(canonical({'reason':str(reason)}),identity))

    def reconcile(self, identity, market_receipt, *, supersede_failed=False):
        with self._db() as db:
            row=db.execute('SELECT * FROM publications WHERE id=?',(identity,)).fetchone()
            if row is None:raise ControlError('CONTROL_INTENT_MISSING')
            self._meta(db,row['epoch'])
            expected=dict(intent_id=identity,epoch=row['epoch'],payload_hash=row['payload_hash'],scope_hash=row['scope_hash'])
            if market_receipt != expected:raise ControlError('CONTROL_MARKET_RECEIPT_MISMATCH')
            if row['state']=='APPLIED':
                if supersede_failed:
                    cursor=db.execute("UPDATE publications SET state='SUPERSEDED' WHERE epoch=? AND job_key=? AND scope_hash=? AND state='FAILED' AND created_seq<?",
                        (row['epoch'],row['job_key'],row['scope_hash'],row['created_seq']))
                    if cursor.rowcount:
                        db.executemany('UPDATE versions SET version=version+1 WHERE topic=?',[(n,) for n in json.loads(row['scope'])])
                return True
            if row['state']!='PREPARED':raise ControlError('CONTROL_INTENT_NOT_PENDING')
            db.execute("UPDATE publications SET state='APPLIED',receipt=? WHERE id=?",(canonical(expected),identity))
            if supersede_failed:
                db.execute("UPDATE publications SET state='SUPERSEDED' WHERE epoch=? AND job_key=? AND scope_hash=? AND state='FAILED' AND created_seq<?",
                    (row['epoch'],row['job_key'],row['scope_hash'],row['created_seq']))
            if row['recovery_id']:
                db.execute("UPDATE quarantines SET state='RESOLVED',requeue_state='DONE' WHERE id=? AND requeue_state='CLAIMED'",(row['recovery_id'],))
            db.executemany('UPDATE versions SET version=version+1 WHERE topic=?',[(n,) for n in json.loads(row['scope'])])
            return True

    def terminal_summary(self, epoch):
        with self._db(readonly=True) as db:
            calls=[dict(r) for r in db.execute('SELECT id,terminal FROM calls WHERE epoch=?',(epoch,))]
            attempts=[dict(r) for r in db.execute('SELECT token,request_id,attempt,terminal,evidence FROM attempts WHERE epoch=?',(epoch,))]
            active=db.execute('SELECT COUNT(*) FROM owners WHERE epoch=?',(epoch,)).fetchone()[0]
            return dict(calls=calls,attempts=attempts,active_owners=active,
                allocated=len(calls),terminal=sum(c['terminal'] is not None for c in calls),
                attempts_allocated=len(attempts),attempts_terminal=sum(a['terminal'] is not None for a in attempts),
                held=active,reaped=sum(bool(json.loads(a['evidence'] or '{}').get('process_reaped')) for a in attempts),
                complete=all(c['terminal'] is not None for c in calls) and all(a['terminal'] is not None for a in attempts),
                all_retired=active==0)
