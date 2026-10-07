"""Local systemd job receipts; never a portfolio or GitHub writer."""
import fcntl
import json
import os
import pathlib
import tempfile
import datetime as dt

JOBS = ('discovery', 'research', 'watchdog', 'blind-replay', 'missed-replay')


def directory():
    return pathlib.Path(os.environ.get('HUNTER_LOCAL_JOB_HEALTH_DIR',
                                      '/var/lib/hunter-job-health'))


def write(job, doc):
    if (job not in JOBS or doc.get('job') != job or
            doc.get('schema') != 'hunter_runtime_job_health_v1' or
            doc.get('source') != 'VULTR_SYSTEMD' or
            doc.get('capital_authority') != 'NONE_SHADOW_ONLY' or
            doc.get('real_trading_enabled') is not False or
            doc.get('status') not in ('SUCCESS', 'FAILURE')):
        raise RuntimeError('LOCAL_JOB_HEALTH_INVALID')
    started = dt.datetime.fromisoformat(doc['started_at_utc'])
    if started.tzinfo is None:
        raise RuntimeError('LOCAL_JOB_HEALTH_TIME_INVALID')
    root = directory()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / ('hunter-runtime-' + job + '-health.json')
    lock_path = root / (job + '.lock')
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists():
            old = json.loads(path.read_text())
            old_started = dt.datetime.fromisoformat(old['started_at_utc'])
            if old_started.tzinfo is None or old_started > started:
                raise RuntimeError('LOCAL_JOB_HEALTH_REJECTED_STALE_WRITER')
            if old_started == started and old.get('status') != doc.get('status'):
                raise RuntimeError('LOCAL_JOB_HEALTH_CONFLICT')
        local = dict(doc, local_publication_only=True,
                     github_health_publication_verified=False)
        raw = json.dumps(local, indent=2, sort_keys=True) + '\n'
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', dir=root, delete=False) as output:
                temporary = pathlib.Path(output.name)
                os.fchmod(output.fileno(), 0o600)
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(path)
            directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
    return path
