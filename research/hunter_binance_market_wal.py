"""Explicit OFFLINE-only Binance market WAL migration (never a runtime journal switch).

A durable marker forces manual recovery after an interrupted migration. The
caller must stop the collector, hold its existing native lock and separately
verify that no readers, writers, or sessions are active before using this tool.
"""
import argparse
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import time

MARKER_SUFFIX = '.wal-migration.json'


class WalMigrationError(ValueError):
    pass



def _wal_reset_fixed(version):
    """Only upstream releases known to include the WAL-reset corruption fix.

    3.52.0 is deliberately excluded because it was withdrawn for unrelated
    compatibility defects. Backports are only recognized on their own lines.
    Version-number acceptance is necessary, never complete deployment proof.
    """
    if type(version) is not tuple or len(version) < 3:
        return False
    major, minor, patch = version[:3]
    if any(type(x) is not int for x in (major, minor, patch)):
        return False
    return ((major,minor) == (3,44) and patch >= 6
            or (major,minor) == (3,50) and patch >= 7
            or (major,minor) == (3,51) and patch >= 3
            or (major,minor) >= (3,53))


def _require_fixed_sqlite():
    if not _wal_reset_fixed(sqlite3.sqlite_version_info):
        raise WalMigrationError('MARKET_WAL_SQLITE_WAL_RESET_FIX_UNVERIFIED:' +
                                sqlite3.sqlite_version)


def _reject_if_link(path):
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise WalMigrationError('MARKET_WAL_UNSAFE_FILE_TYPE:' + path.name)
    return True


def _sync_directory(directory):
    fd = os.open(str(directory), os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _phase(marker, value, *, create):
    # O_EXCL at PREPARED, atomic replacement for terminal phase; no reset.
    body = (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()
    target = marker if create else marker.with_name(marker.name + '.next')
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(str(target), flags, 0o600)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        if not create:
            os.replace(target, marker)
        _sync_directory(marker.parent)
    except BaseException:
        # Keep the PREPARED marker for conservative operator review.
        if not create:
            try: target.unlink()
            except OSError: pass
        raise


def _check_quiescent(connection, *, control_path):
    meta = dict(connection.execute('SELECT key,value FROM meta'))
    connected = meta.get('connected')
    if connected == '1':
        raise WalMigrationError('MARKET_WAL_ACTIVE_EPOCH')
    if connected != '0':
        # Empty never-started SQLite stores are allowed; unknown active state is not.
        if connected is not None or connection.execute('SELECT COUNT(*) FROM topics').fetchone()[0]:
            raise WalMigrationError('MARKET_WAL_UNKNOWN_EPOCH')
    for table in ('leases', 'requests'):
        if connection.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]:
            raise WalMigrationError('MARKET_WAL_LEGACY_OWNERS:' + table)
    if control_path.exists():
        _reject_if_link(control_path)
        with contextlib.closing(sqlite3.connect(control_path.as_uri() + '?mode=ro', uri=True, timeout=.25)) as c:
            for table in ('owners', 'requests'):
                if c.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]:
                    raise WalMigrationError('MARKET_WAL_CONTROL_OWNERS:' + table)
            phase = dict(c.execute('SELECT key,value FROM meta')).get('phase')
            if phase not in ('OFFLINE', 'STOPPED'):
                raise WalMigrationError('MARKET_WAL_CONTROL_PHASE_NOT_OFFLINE')


def migrate(state):
    if sys.platform != 'linux':
        raise WalMigrationError('MARKET_WAL_REQUIRES_LINUX')
    state = Path(state)
    if not state.is_absolute() or not _reject_if_link(state):
        raise WalMigrationError('MARKET_WAL_EXISTING_ABSOLUTE_DATABASE_REQUIRED')
    marker = Path(str(state) + MARKER_SUFFIX)
    lock_path = state.with_name('collector.lock')
    if not _reject_if_link(lock_path):
        raise WalMigrationError('MARKET_WAL_EXISTING_COLLECTOR_LOCK_REQUIRED')
    import fcntl
    flags = os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(str(lock_path), flags)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _require_fixed_sqlite()
        identity = state.stat()
        if _reject_if_link(marker) or Path(str(marker) + '.next').exists():
            raise WalMigrationError('MARKET_WAL_PREVIOUS_MIGRATION_REVIEW_REQUIRED')
        for suffix in ('-wal', '-shm'):
            if Path(str(state) + suffix).exists():
                raise WalMigrationError('MARKET_WAL_EXISTING_SIDECAR_REVIEW_REQUIRED')
        uri = state.as_uri() + '?mode=rw'
        with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=.25, isolation_level=None)) as db:
            _check_quiescent(db, control_path=state.with_name('control.sqlite3'))
            before = db.execute('PRAGMA journal_mode').fetchone()[0].upper()
            if before != 'DELETE':
                raise WalMigrationError('MARKET_WAL_UNEXPECTED_PREVIOUS_MODE:' + before)
        prepared = dict(schema='binance_market_wal_migration_v1', status='PREPARED',
                        state=str(state), dev=identity.st_dev, ino=identity.st_ino,
                        previous_mode='DELETE', target_mode='WAL', control_mode='UNCHANGED',
                        python_sqlite_version=sqlite3.sqlite_version,
                        prepared_at_ns=time.time_ns())
        _phase(marker, prepared, create=True)
        # A crash from here onward is fail-closed until manual recovery.
        with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=.25, isolation_level=None)) as db:
            changed = db.execute('PRAGMA journal_mode=WAL').fetchone()[0].upper()
            if changed != 'WAL':
                raise WalMigrationError('MARKET_WAL_MODE_SWITCH_FAILED:' + changed)
            check = db.execute('PRAGMA quick_check').fetchone()[0]
            if check != 'ok':
                raise WalMigrationError('MARKET_WAL_QUICK_CHECK_FAILED')
            checkpoint = tuple(db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone())
            if checkpoint[0] != 0:
                raise WalMigrationError('MARKET_WAL_CHECKPOINT_BUSY')
        with contextlib.closing(sqlite3.connect(state.as_uri() + '?mode=ro', uri=True, timeout=.25)) as db:
            verified = db.execute('PRAGMA journal_mode').fetchone()[0].upper()
            if verified != 'WAL':
                raise WalMigrationError('MARKET_WAL_REOPEN_VERIFY_FAILED:' + verified)
        if (state.stat().st_dev, state.stat().st_ino) != (identity.st_dev, identity.st_ino):
            raise WalMigrationError('MARKET_WAL_DATABASE_IDENTITY_CHANGED')
        result = dict(prepared, status='VERIFIED', verified_mode='WAL',
                      checkpoint=list(checkpoint), verified_at_ns=time.time_ns())
        _phase(marker, result, create=False)
        return result
    finally:
        os.close(fd)


def require_no_interrupted_migration(state):
    """Called by normal market store/reader construction; never mutates SQLite."""
    state = Path(state)
    marker = Path(str(state) + MARKER_SUFFIX)
    if not marker.exists() and not marker.is_symlink():
        return
    if not _reject_if_link(marker):
        raise WalMigrationError('MARKET_WAL_MARKER_UNREADABLE')
    _require_fixed_sqlite()
    try:
        record = json.loads(marker.read_text())
        if (record.get('schema') != 'binance_market_wal_migration_v1'
                or record.get('state') != str(state)
                or record.get('status') != 'VERIFIED'
                or record.get('verified_mode') != 'WAL'
                or (record.get('dev'), record.get('ino')) != (state.stat().st_dev, state.stat().st_ino)):
            raise WalMigrationError('MARKET_WAL_UNVERIFIED_MIGRATION')
        with contextlib.closing(sqlite3.connect(state.as_uri() + '?mode=ro', uri=True, timeout=.25)) as db:
            if db.execute('PRAGMA journal_mode').fetchone()[0].upper() != 'WAL':
                raise WalMigrationError('MARKET_WAL_JOURNAL_MODE_DRIFT')
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as error:
        raise WalMigrationError('MARKET_WAL_UNVERIFIED_MIGRATION') from error


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(migrate(args.state), sort_keys=True))
