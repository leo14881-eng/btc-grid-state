"""Isolated Binance read process; no exchange/network/writes or trading authority.

Not a replacement for the public consumer's proof/fence validation. Every
returned generation is revalidated in the caller immediately before delivery.
"""
from contextlib import contextmanager
import datetime as dt
import json
from pathlib import Path
import subprocess
import sys
import time

from research.hunter_binance_stream_store import Store

# Allowlisted non-authoritative denial identities only. No database, error
# strings, credentials, stack traces or market rows leave a rejected child.
SAFE_DENIAL_REASONS=frozenset((
    'CONTROL_PUBLICATION_PENDING','CONTROL_READ_FENCE_CHANGED',
    'BINANCE_FINAL_READ_REVOKED','BINANCE_FINAL_TOPIC_REVOKED',
    'BINANCE_FINAL_GENERATION_HASH','BINANCE_FINAL_READ_STALE',
    'BINANCE_CACHE_UNKNOWN:BINANCE_TOPIC_STALE_OR_NOT_READY',
    'BINANCE_CACHE_UNKNOWN:BINANCE_CURRENT_WS_REQUIRED',
    'BINANCE_CACHE_UNKNOWN:BINANCE_TRADING_STATUS_UNKNOWN_OR_INVALID',
    'BINANCE_CACHE_UNKNOWN:BINANCE_UNRECONCILED_RECEIPT',
    'BINANCE_CACHE_UNKNOWN:BINANCE_QUIET_REVERIFICATION_REQUIRED',
    'BINANCE_CACHE_UNKNOWN:BINANCE_HEALTH_DRAINING_OR_STOPPED',
))



class ReadOnlyStore(Store):
    @contextmanager
    def db(self, readonly=False, timeout=10, *, write_kind='market'):
        if not readonly:
            raise RuntimeError('BINANCE_ISOLATED_READER_WRITE_FORBIDDEN')
        import sqlite3
        # Set read-only invariants BEFORE starting a read transaction.
        # Never call writable Store.db() from a subprocess, even transiently.
        uri=self.path.as_uri()+'?mode=ro'
        with sqlite3.connect(uri,uri=True,timeout=min(timeout,.25)) as db:
            db.row_factory=sqlite3.Row
            db.execute('PRAGMA query_only=1')
            db.execute('PRAGMA busy_timeout=250')
            db.execute('BEGIN')
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise


def _payload(request):
    """Child-only execution. No trusted decision crosses this JSON boundary."""
    from research import hunter_binance_stream_consumer as consumer
    path=Path(request['database'])
    if not path.is_absolute():
        raise ValueError('BINANCE_READER_ABSOLUTE_DB_REQUIRED')
    names=request['names']
    read_at=request['read_at_ms']
    now=dt.datetime.fromtimestamp(request['cut_ms']/1000,dt.timezone.utc)
    reader=ReadOnlyStore.reader(path)
    frozen=consumer.read(names,now,reader=reader,clock=lambda:read_at,
                         expected_roster=request.get('expected_roster'))
    # This includes the original all-topic proof and independent control check.
    frozen.check_delivery(started=read_at)
    return dict(generation=frozen.generation, fence=frozen._fence,
                names=names,read_at_ms=read_at)


def read_in_process(names, now, *, database, clock=lambda:time.time_ns()//1_000_000,
                    expected_roster=None, timeout=20, before_delivery=None):
    """Return a validated Frozen, never a child-provided READY boolean.

    Caller must preserve its original full requested scope; the returned
    Frozen retains all existing late check_delivery() methods.
    """
    from research import hunter_binance_stream_consumer as consumer
    names=sorted(names)
    if not names or len(names)!=len(set(names)):
        raise ValueError('BINANCE_ISOLATED_READER_SCOPE_INVALID')
    path=Path(database).resolve()
    read_at=clock()
    request=dict(database=str(path),names=names,cut_ms=int(now.timestamp()*1000),
                 read_at_ms=read_at,expected_roster=expected_roster)
    root=Path(__file__).resolve().parents[1]
    response=subprocess.run([sys.executable,'-B','-m','research.hunter_binance_process_reader','--child'],
        input=json.dumps(request,separators=(',',':')),text=True,capture_output=True,
        cwd=root,timeout=timeout,check=False)
    if response.returncode!=0:
        # A denied child can return ONLY an allowlisted reason. Its result
        # cannot carry data or grant readiness, even if it is syntactically JSON.
        try:
            failure=json.loads(response.stdout)
            reason=failure.get('reason') if failure.get('status')=='DENIED' else None
        except (ValueError,TypeError,AttributeError):
            reason=None
        if reason in SAFE_DENIAL_REASONS:
            raise ValueError(reason)
        raise ValueError('BINANCE_ISOLATED_READER_DENIED')
    try:
        result=json.loads(response.stdout)
        if (result.get('names')!=names or result.get('read_at_ms')!=read_at
                or not isinstance(result.get('fence'),dict)
                or not isinstance(result.get('generation'),dict)):
            raise ValueError('BINANCE_ISOLATED_READER_ENVELOPE_INVALID')
    except (TypeError,ValueError,json.JSONDecodeError) as error:
        raise ValueError('BINANCE_ISOLATED_READER_ENVELOPE_INVALID') from error
    # The child has exited; only fresh independent parent readback may deliver.
    if before_delivery is not None:
        before_delivery()  # Isolated fixture seam, not a way to bypass checks.
    reader=ReadOnlyStore.reader(path)
    frozen=consumer.Frozen(result['generation'],checked_at=read_at,reader=reader,
        fence=result['fence'],names=names,clock=clock,started_at=read_at)
    frozen.check_delivery()
    return frozen


def main():
    if len(sys.argv)!=2 or sys.argv[1]!='--child':
        raise SystemExit('BINANCE_ISOLATED_READER_CHILD_ONLY')
    try:
        request=json.loads(sys.stdin.read())
        result=_payload(request)
    except BaseException as error:
        # Never emit partial generations, proof internals or stack traces.
        reason=str(error)
        safe=reason if reason in SAFE_DENIAL_REASONS else 'BINANCE_ISOLATED_READER_DENIED'
        print(json.dumps(dict(status='DENIED',reason=safe)),flush=True)
        raise SystemExit(1)
    print(json.dumps(result,separators=(',',':'),ensure_ascii=True),flush=True)


if __name__=='__main__':
    main()
