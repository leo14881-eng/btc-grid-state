"""Wait for natural snapshots, prepare one real origin/main readback, start only collectors."""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time

REPO = Path('/opt/shadow-runner/btc-grid-state')
INSTALLER = Path('/root/hunter-kline-repair-3da03c9.py')
INSTALLER_SHA256 = '00e8c0e9f2ff48c27d7421af5c2120f760bc528afd44c530b3220bfbd760bdf3'
UNITS = ('hunter-bybit-stream.service', 'hunter-binance-stream.service')
SOURCES = (
    ('bybit', 'research/results/hunter-bybit-worker-snapshot.json', 'captured_at_utc', 900),
    ('binance', 'research/results/hunter-cex-universe-latest.json', 'as_of_utc', 1800),
)
WAIT_SECONDS = 4500
POLL_SECONDS = 120
HEADROOM_SECONDS = 300  # Stricter admission; leaves time for fetch/prepare/start.
PREFIX_READ = """import urllib.request,sys
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open(sys.argv[1], timeout=10) as response:
    sys.stdout.buffer.write(response.read(4096))
"""
ENV = dict(PATH='/usr/sbin:/usr/bin:/sbin:/bin', LANG='C.UTF-8',
    SYSTEMD_PAGER='cat', SYSTEMD_COLORS='0', GIT_TERMINAL_PROMPT='0',
    GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, PYTHONDONTWRITEBYTECODE='1')


def require(ok, reason):
    if not ok:
        raise RuntimeError(reason)


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


class Runner:
    def __init__(self, work):
        self.work = Path(work)
        self.serial = 0

    def log(self, stage, **fields):
        line = json.dumps(dict(at_utc=utcnow().isoformat(), stage=stage, **fields), sort_keys=True)
        print(line, flush=True)
        with (self.work/'events.jsonl').open('a', encoding='utf-8') as handle:
            handle.write(line+'\n')

    def run(self, stage, args, timeout):
        self.serial += 1
        self.log(stage, event='BEGIN', timeout_seconds=timeout)
        started = time.monotonic()
        process = subprocess.Popen([str(a) for a in args], env=ENV, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        try:
            while True:
                remaining = timeout-(time.monotonic()-started)
                if remaining <= 0:
                    raise TimeoutError(stage+'_TIMEOUT')
                try:
                    output, errors = process.communicate(timeout=min(10, remaining))
                    break
                except subprocess.TimeoutExpired:
                    self.log(stage, event='RUNNING', elapsed_seconds=round(time.monotonic()-started))
        except BaseException:
            # SIGINT first lets the verified installer reap its own detached child.
            try: os.killpg(process.pid, signal.SIGINT)
            except ProcessLookupError: pass
            try: output, errors = process.communicate(timeout=12)
            except subprocess.TimeoutExpired:
                try: os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                output, errors = process.communicate(timeout=10)
            self.save_output(stage, output, errors)
            raise
        self.save_output(stage, output, errors)
        self.log(stage, event='END', exit_code=process.returncode)
        require(process.returncode == 0, stage+'_FAILED_'+str(process.returncode))
        return output

    def save_output(self, stage, output, errors):
        prefix = str(self.serial)+'-'+stage
        for suffix, data in (('.stdout', output), ('.stderr', errors)):
            path = self.work/(prefix+suffix)
            with path.open('xb') as handle:
                handle.write(data)
        if errors:
            self.log(stage, stderr_file=str(self.work/(prefix+'.stderr')))


def git(runner, repo, stage, *args, timeout=20):
    return runner.run(stage, ['git', '-C', repo, *args], timeout)


def sha(value):
    value = value.decode().strip()
    require(re.fullmatch('[0-9a-f]{40}', value), 'INVALID_READBACK_SHA')
    return value


def origin_head(runner, repo):
    return sha(git(runner, repo, 'PIN_ORIGIN_MAIN', 'rev-parse', 'origin/main'))


def ages(stamps, now, margin=0):
    result = {}
    for venue, _, _, maximum in SOURCES:
        stamp = dt.datetime.fromisoformat(stamps[venue].replace('Z', '+00:00'))
        require(stamp.tzinfo is not None, 'SNAPSHOT_TIMEZONE_MISSING_'+venue)
        age = (now-stamp).total_seconds()
        result[venue] = dict(captured_at_utc=stamps[venue], age_seconds=round(age, 1),
            status='FUTURE' if age < 0 else 'STALE' if age > maximum else
                   'INSUFFICIENT_HEADROOM' if age > maximum-margin else 'FRESH')
    return result


def fresh(result):
    return all(item['status'] == 'FRESH' for item in result.values())


def probe(runner, repo, deadline=None, clock=time.monotonic):
    def budget():
        remaining = 20 if deadline is None else min(20, deadline-clock())
        if remaining <= 0:
            raise TimeoutError('NATURAL_SNAPSHOT_WAIT_DEADLINE')
        return remaining
    remote = git(runner, repo, 'PROBE_MAIN_REF', 'ls-remote', 'origin', 'refs/heads/main', timeout=budget())
    fields = remote.decode().split()
    require(len(fields) == 2 and fields[1] == 'refs/heads/main', 'REMOTE_MAIN_REF_INVALID')
    candidate = sha(fields[0].encode())
    stamps = {}
    for venue, path, key, _ in SOURCES:
        url = 'https://raw.githubusercontent.com/leo14881-eng/btc-grid-state/'+candidate+'/'+path
        data = runner.run('PROBE_'+venue.upper(), [sys.executable, '-I', '-B', '-c', PREFIX_READ, url], budget())
        matches = re.findall(r'"'+key+r'"\s*:\s*"([^"]+)"', data.decode('utf-8', errors='replace'))
        require(len(matches) == 1, 'SNAPSHOT_PREFIX_TIME_UNAVAILABLE_'+venue)
        stamps[venue] = matches[0]
    result = ages(stamps, utcnow(), HEADROOM_SECONDS)
    runner.log('NATURAL_SNAPSHOT_PROBE', candidate_sha=candidate, snapshots=result)
    return fresh(result)


def wait_for_natural(runner, repo, seconds=WAIT_SECONDS, clock=time.monotonic, sleep=time.sleep):
    deadline = clock()+seconds
    while clock() < deadline:
        if probe(runner, repo, deadline=deadline, clock=clock):
            return
        remaining = deadline-clock()
        if remaining <= 0:
            break
        runner.log('WAIT_NATURAL_PUBLICATION', next_probe_seconds=min(POLL_SECONDS, round(remaining)))
        sleep(min(POLL_SECONDS, remaining))
    raise TimeoutError('NO_FRESH_NATURAL_SNAPSHOT_WITHIN_WAIT_NO_FETCH_NO_START')


def state(runner):
    output = runner.run('COLLECTOR_STATE', ['systemctl', '--no-pager', 'show', *UNITS,
        '-p', 'Id', '-p', 'LoadState', '-p', 'ActiveState', '-p', 'SubState', '-p', 'MainPID', '-p', 'ExecMainStatus'], 20)
    states = [dict(line.split('=', 1) for line in block.splitlines() if '=' in line)
              for block in output.decode().strip().split('\n\n')]
    require({s.get('Id') for s in states} == set(UNITS), 'COLLECTOR_STATE_INCOMPLETE')
    runner.log('COLLECTOR_STATE_READBACK', units=states)
    return states


def inactive(runner):
    require(all(s.get('LoadState') == 'loaded' and s.get('ActiveState') == 'inactive' for s in state(runner)),
            'COLLECTOR_NOT_INACTIVE_PRESERVED_NO_RESTART')


def verify_helper(path=INSTALLER):
    st = path.lstat()
    require(path.is_file() and not path.is_symlink() and st.st_uid == 0 and not st.st_mode & 0o022,
            'INSTALLER_OWNER_OR_MODE_INVALID')
    require(hashlib.sha256(path.read_bytes()).hexdigest() == INSTALLER_SHA256, 'INSTALLER_SHA256_MISMATCH')


def prepare(runner, repo, readback):
    runner.run('PREPARE_EXACT_READBACK', [sys.executable, '-I', '-B', INSTALLER,
        '--repo', repo, '--prepare-readback', readback], 180)


def fetch_prepare_start(runner, repo):
    # A named branch and an explicit, non-forced tracking ref. Never fetch only
    # the probe SHA: FETCH_HEAD alone does not satisfy the installer's guard.
    git(runner, repo, 'FETCH_MAIN_ONCE', 'fetch', '--no-tags', 'origin',
        'main:refs/remotes/origin/main', timeout=60)
    readback = origin_head(runner, repo)  # Remote may have advanced since probe.
    stamps = {}
    for venue, path, key, _ in SOURCES:
        data = git(runner, repo, 'READ_'+venue.upper(), 'show', readback+':'+path, timeout=20)
        stamps[venue] = json.loads(data)[key]
    result = ages(stamps, utcnow(), HEADROOM_SECONDS)
    runner.log('FETCHED_SNAPSHOT_TIMES', readback_sha=readback, snapshots=result)
    require(fresh(result), 'FETCHED_SNAPSHOT_NOT_FRESH_PRESERVED_NO_PREPARE')
    inactive(runner)
    require(origin_head(runner, repo) == readback, 'ORIGIN_MAIN_CHANGED_BEFORE_PREPARE')
    prepare(runner, repo, readback)  # Original exact-origin/main guard remains active.
    require(origin_head(runner, repo) == readback, 'ORIGIN_MAIN_CHANGED_AFTER_PREPARE_NO_START')
    require(fresh(ages(stamps, utcnow())), 'PREPARED_SNAPSHOT_EXPIRED_NO_START')
    inactive(runner)
    runner.run('START_COLLECTORS_ONLY', ['systemctl', 'start', *UNITS], 60)
    observed = state(runner)  # One post-start readback, no readiness polling here.
    require(all(s.get('ActiveState') == 'active' and s.get('SubState') == 'running'
                and int(s.get('MainPID', '0')) > 0 for s in observed), 'START_READBACK_NOT_RUNNING')
    runner.log('STARTED_CACHE_ACCEPTANCE_PENDING', readback_sha=readback,
        publication_verified=False, consumers_changed=False, monitor_changed=False)
    return readback


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true')
    args = parser.parse_args()
    if not args.run:
        print(json.dumps(dict(status='PLAN_ONLY', wait_seconds=WAIT_SECONDS, probe_interval_seconds=POLL_SECONDS,
            snapshot_prefix_bytes=4096, maximum_full_fetches=1, starts=UNITS, consumers_changed=False)))
        return 0
    require(sys.platform == 'linux' and os.geteuid() == 0, 'AUTHORIZED_LINUX_ROOT_REQUIRED')
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(InterruptedError('TERMINATED')))
    os.umask(0o077)
    work = Path(tempfile.mkdtemp(prefix='hunter-kline-start-'))
    runner = Runner(work)
    runner.log('BEGIN', evidence_directory=str(work))
    try:
        verify_helper()
        inactive(runner)
        wait_for_natural(runner, REPO)
        fetch_prepare_start(runner, REPO)
        return 0
    except BaseException as error:
        runner.log('STOP_PRESERVE_ALL_STATE', error_type=type(error).__name__,
            reason=str(error) if isinstance(error, (RuntimeError, TimeoutError, InterruptedError)) else 'INSPECT_LAST_STAGE',
            automatic_retry=False, automatic_worker_fallback=False)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
