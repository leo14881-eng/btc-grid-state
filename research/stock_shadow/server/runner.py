"""Execute one stock generation in an isolated checkout; preview is non-publishing."""
import argparse
from datetime import datetime, timezone
import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

from .persist import ROOT, changed_paths, persist, snapshot, validate_outputs, PublicationUnverified
from .runtime import JOBS, admission, check_fence, environment, git, require

PROVIDER_ENV_KEYS = ('APCA_API_KEY_ID', 'APCA_API_SECRET_KEY',
                     'STOCKFIT_API_KEY', 'FMP_API_KEY', 'SEC_USER_AGENT', 'SEC_TRANSPORT_STATE')

COMMANDS = {
    'main': [('research/stock_shadow/fundamentals_observer.py', 480),
             ('research/stock_shadow/stock_shadow_v1.py', 900),
             ('research/stock_shadow/stock_review.py', 60)],
    'monitor': [('research/stock_shadow/stock_position_monitor.py', 300),
                ('research/stock_shadow/stock_review.py', 60)],
    'replay': [('research/stock_shadow/stock_replay.py', 1200)],
}


def canonical_inventory():
    """Detect test changes to all canonical files, even Git-ignored additions."""
    inventory = {}
    base = Path(ROOT)
    if base.is_symlink():
        return {ROOT: ('symlink', os.readlink(base))}
    for path in sorted(base.rglob('*')):
        if path.is_symlink():
            inventory[str(path)] = ('symlink', os.readlink(path))
        elif path.is_file():
            inventory[str(path)] = ('file', path.stat().st_mode,
                                    hashlib.sha256(path.read_bytes()).hexdigest())
    return inventory


def test_environment(env):
    """Unit tests must never inherit the live data-provider credentials."""
    return {key: value for key, value in env.items() if key not in PROVIDER_ENV_KEYS}


def run(job, preview, runtime_dir):
    require(not changed_paths(), 'SOURCE_CHECKOUT_NOT_CLEAN')
    source = git('rev-parse', 'HEAD').stdout.strip()
    run_id = 'server-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:12]
    a = admission('server', job, run_id, source)
    runtime_dir = Path(runtime_dir).resolve()
    checkout = Path.cwd().resolve()
    require(runtime_dir != checkout and checkout not in runtime_dir.parents,
            'RUNTIME_DIRECTORY_MUST_BE_OUTSIDE_CHECKOUT')
    runtime_dir.mkdir(parents=True, exist_ok=True)
    if not preview and not a['admitted']:
        print('STOCK_SERVER_NOT_ADMITTED', job, a['epoch'])
        return {'status': 'SKIPPED_NOT_OWNER', **a}
    git('fetch', '--quiet', 'origin', 'main')
    if not preview:
        check_fence(a, 'origin/main')
    # Preview is still based on a known main ancestor, never stale abandoned code.
    require(git('merge-base', '--is-ancestor', source, 'origin/main', check=False).returncode == 0,
            'SOURCE_NOT_AUTHORITATIVE')
    if preview:
        git('remote', 'set-url', '--push', 'origin', 'disabled://stock-preview')
    else:
        require(os.environ.get('APCA_API_KEY_ID') and os.environ.get('APCA_API_SECRET_KEY'),
                'STOCK_PROVIDER_CREDENTIALS_NOT_CONFIGURED')
    env = dict(os.environ, **environment(a), PYTHONDONTWRITEBYTECODE='1')
    # Stable runtime state shared by preview/publish and fresh checkouts; never in Git.
    env['SEC_TRANSPORT_STATE'] = str(runtime_dir / 'sec-transport.sqlite3')
    # No flag or environment inheritance is allowed to turn forced diagnostics
    # into a scheduler policy. The unchanged engine applies NY calendar gates.
    env.pop('STOCK_SHADOW_FORCE_MONITOR', None)
    env.pop('STOCK_SHADOW_FORCE', None)
    record = {'schema': 'stock_server_run_v1', **a, 'preview': preview,
              'started_at': datetime.now(timezone.utc).isoformat(),
              'status': 'STARTED', 'verified': False}
    path = runtime_dir / ('run-' + a['generation'] + '.json')

    def save():
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(record, indent=2, sort_keys=True) + '\n')
        temp.replace(path)

    save()
    deadline = time.monotonic() + (360 if job == 'monitor' else 1200)
    engine_started = False
    canonical_before_tests = None
    try:
        canonical_before_tests = canonical_inventory()
        subprocess.run([sys.executable, '-m', 'pytest', '-q', '--durations=10', 'tests/test_stock_shadow.py',
                        'tests/test_stock_server_migration.py'], env=test_environment(env), check=True, timeout=180)
        require(not changed_paths() and canonical_inventory() == canonical_before_tests,
                'STOCK_TESTS_MUTATED_CHECKOUT')
        for script, timeout in COMMANDS[job]:
            remaining = deadline - time.monotonic()
            require(remaining > 0, 'STOCK_RUN_DEADLINE_EXCEEDED')
            engine_started = True
            subprocess.run([sys.executable, script], env=env, check=True, timeout=min(timeout, remaining))
        record['reporting_audit'] = validate_outputs(snapshot(job, a), job, a)
        if preview:
            record['status'] = 'PREVIEW_COMPLETE'
        else:
            record.update(persist(job, a))
            record['status'] = 'PUBLISHED_AND_READ_BACK'
    except Exception as exc:
        record.update(status='FAILED', error_type=type(exc).__name__, error=str(exc)[:300])
        if isinstance(exc, PublicationUnverified):
            record.update(status='PUBLICATION_UNVERIFIED', candidate_commit=exc.commit)
        # Preserve successful old book on any stage failure. Only a matching
        # engine-generated FAILED health document can take this narrow path.
        health = Path(ROOT + job + '-run-health-v1.json')
        if not engine_started and not preview and job in ('main', 'monitor'):
            try:
                # A test-created/modified artifact is never trusted as health.
                # Publish only a runner-authored failure, with the old canonical
                # book intact. The existing publisher rechecks epoch and all CAS
                # inputs, writes only health+manifest, and verifies remote bytes.
                clean = (canonical_before_tests is not None and not changed_paths()
                         and canonical_inventory() == canonical_before_tests)
                if not clean:
                    record['failure_health_skipped'] = 'PREFLIGHT_FAILURE_CHECKOUT_NOT_CLEAN'
                else:
                    h = {'schema': 'stock_server_preflight_health_v1',
                         'status': 'FAILED', 'stage': 'PREFLIGHT', 'job': job,
                         'source_commit': source, 'run_id': run_id,
                         'generation': a['generation'], 'owner': a['owner'], 'epoch': a['epoch'],
                         'updated_at': datetime.now(timezone.utc).isoformat(),
                         'simulation_only': True, 'real_orders': False,
                         'engine_started': False, 'error_type': type(exc).__name__}
                    preflight_health = Path(ROOT + job + '-preflight-health-v1.json')
                    preflight_health.write_text(json.dumps(h, indent=2, sort_keys=True) + '\n')
                    record['failure_health_publication'] = persist('preflight', a)
            except Exception as error:
                record['failure_health_error'] = type(error).__name__ + ': ' + str(error)[:200]
                if isinstance(error, PublicationUnverified):
                    record['failure_health_candidate_commit'] = error.commit
        if engine_started and not preview and job in ('main', 'monitor') and health.exists():
            h = json.loads(health.read_text())
            if h.get('status') == 'FAILED' and h.get('run_id') == a['run_id']:
                try:
                    record['failure_health_publication'] = persist('health', a)
                except Exception as error:
                    record['failure_health_error'] = type(error).__name__ + ': ' + str(error)[:200]
        raise
    finally:
        if preview:
            destination = runtime_dir / 'previews' / a['generation']
            destination.mkdir(parents=True, exist_ok=True)
            if Path(ROOT).is_symlink():
                record['preview_archive_error'] = 'CANONICAL_ROOT_IS_SYMLINK'
            else:
                shutil.copytree(ROOT, destination / 'stock-shadow', dirs_exist_ok=True, symlinks=True)
                record['preview_outputs'] = str(destination)
        record['finished_at'] = datetime.now(timezone.utc).isoformat()
        save()
    print(json.dumps(record, sort_keys=True))
    return record


def main():
    p = argparse.ArgumentParser()
    p.add_argument('job', choices=JOBS)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--preview', action='store_true')
    mode.add_argument('--publish', action='store_true')
    p.add_argument('--runtime-dir', required=True)
    args = p.parse_args()
    run(args.job, args.preview, args.runtime_dir)


if __name__ == '__main__':
    main()
