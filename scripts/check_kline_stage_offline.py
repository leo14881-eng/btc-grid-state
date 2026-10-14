"""Exercise actual release staging in isolated interpreters; no market/state IO."""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import hunter_kline_install as install

# Only the staged tree is added to an isolated interpreter's import path.
# Audit hook forbids any network, child process, database or filesystem write.
BOOTSTRAP = r'''
import sys, os, json, datetime as dt
from pathlib import Path
stage = Path.cwd().resolve()
sys.path.insert(0, str(stage))
def guard(event, args):
    if event.startswith(('socket.', 'subprocess.', 'os.exec', 'os.spawn')) or event in (
            'sqlite3.connect', 'os.system', 'os.mkdir', 'os.remove', 'os.rename', 'os.chmod', 'os.chown'):
        raise RuntimeError('FORBIDDEN_STAGE_EFFECT_'+event)
    if event == 'open':
        value, mode, flags = args
        if ((isinstance(mode, str) and any(c in mode for c in 'wax+')) or
                isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)):
            raise RuntimeError('FORBIDDEN_STAGE_WRITE')
        if not isinstance(value, int):
            path = Path(value).resolve()
            if path.name == 'hunter-shadow-rules.json' and stage not in path.parents:
                raise RuntimeError('CONFIG_ESCAPED_STAGE')
sys.addaudithook(guard)
venue, source, stamp, snapshot = json.load(sys.stdin)
module = __import__('scripts.hunter_'+venue+'_stream_ops', fromlist=['prepare'])
receipt = module.prepare(snapshot, source, dt.datetime.fromisoformat(stamp))
for name, imported in tuple(sys.modules.items()):
    if name.split('.')[0] in ('research', 'scripts') and getattr(imported, '__file__', None):
        assert stage in Path(imported.__file__).resolve().parents, name
assert receipt['real_trading_enabled'] is False
assert receipt['capital_authority'] == 'NONE_SHADOW_ONLY'
print(json.dumps(dict(venue=venue, topics=len(receipt['topics']), status='STAGED_PREPARE_OK')))
'''


def main():
    # Parent uses only local git objects, then only new scratch directories.
    entries = install.code_entries(ROOT)
    now = dt.datetime(2026, 10, 12, 13, 0, tzinfo=dt.timezone.utc)
    bybit = dict(schema='hunter_bybit_worker_v1', source='OFFICIAL_BYBIT_V5_VIA_WORKER',
        captured_at_utc=now.isoformat(),
        rows=[dict(base='GRASS', pair='GRASSUSDT', venue='bybit', price=1, volume_24h_usdt=10, change_24h_pct=0)],
        venue_status=dict(valid_pairs=1, active_pairs=1, missing_or_invalid=[], signal_expected_bases=['GRASS']),
        early_signals={})
    bybit['snapshot_sha256'] = hashlib.sha256(json.dumps(bybit, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    binance = dict(binance_complete=True, generation_id='offline-fixture', as_of_utc=now.isoformat(),
        coins={s: dict(pairs=[dict(venue='binance', base=s, pair=s+'USDT')]) for s in ('BTC', 'ETH')})
    child_env = {k: os.environ[k] for k in ('PATH', 'SYSTEMROOT', 'WINDIR') if k in os.environ}
    child_env['PYTHONPATH'] = str(ROOT)  # Deliberate poison; -I must ignore the full repository.
    with tempfile.TemporaryDirectory(prefix='kline-staged-offline-') as temporary:
        root = Path(temporary)
        def stage(name, package):
            prefix = root/name
            prefix.mkdir()
            install.extract_code(prefix, package)
            return prefix/'code'
        broken = stage('old-python-only', [e for e in entries if e[0] not in install.RUNTIME_CONFIG])
        fixed = stage('fixed-package', entries)
        checks = [
            (broken, 'bybit', bybit, now, 'FileNotFoundError', 'hunter-shadow-rules.json'),
            (fixed, 'bybit', bybit, now, None, None),
            (fixed, 'binance', binance, now, None, None),
            (fixed, 'bybit', bybit, now+dt.timedelta(seconds=901), 'ValueError', 'BYBIT_WORKER_SNAPSHOT_STALE_OR_FUTURE'),
            (fixed, 'bybit', dict(bybit, snapshot_sha256='0'*64), now, 'ValueError', 'CHECKSUM_MISMATCH'),
            (fixed, 'binance', binance, now+dt.timedelta(seconds=1801), 'ValueError', 'BINANCE_SCAN_NOT_FRESH_COMPLETE'),
            (fixed, 'binance', dict(binance, binance_complete=False), now, 'ValueError', 'BINANCE_SCAN_NOT_FRESH_COMPLETE'),
        ]
        for directory, venue, snapshot, clock, error, detail in checks:
            before = {p.relative_to(directory): p.read_bytes() for p in directory.rglob('*') if p.is_file()}
            result = subprocess.run([sys.executable, '-I', '-B', '-c', BOOTSTRAP], cwd=directory, env=child_env,
                input=json.dumps([venue, install.RELEASE, clock.isoformat(), snapshot]).encode(), capture_output=True, timeout=30)
            stderr = result.stderr.decode('utf-8', errors='replace')
            if error:
                assert result.returncode != 0 and error in stderr and detail in stderr, stderr
            else:
                assert result.returncode == 0, stderr
                report = json.loads(result.stdout)
                assert report['topics'] == (4 if venue == 'bybit' else 7), report
            after = {p.relative_to(directory): p.read_bytes() for p in directory.rglob('*') if p.is_file()}
            assert before == after, 'STAGED_CONTENT_CHANGED'
            print('STAGED_CHECK_OK', venue, error or 'prepare', detail or 'isolated', flush=True)
    print('KLINE_STAGED_ACCEPTANCE checks=7 skips=0 passed=true')


if __name__ == '__main__': main()
