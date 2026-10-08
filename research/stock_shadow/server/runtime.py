"""Git-authoritative stock writer admission, shared by GitHub and the server."""
import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

CONFIG = '.github/stock-runtime.json'
JOBS = ('main', 'monitor', 'replay')


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def git(*args, input=None, env=None, check=True):
    # Scoped to stock commands, including fetches during CAS/readback. Keep
    # background repack out of the service CPU budget and inherited writer lock.
    return subprocess.run(['git', '-c', 'gc.auto=0', '-c', 'maintenance.auto=false', *args], input=input, text=True,
                          capture_output=True, check=check, env=env)


def read_config(ref='HEAD'):
    value = json.loads(git('show', f'{ref}:{CONFIG}').stdout)
    require(value.get('schema') == 'stock_shadow_runtime_v1', 'INVALID_RUNTIME_SCHEMA')
    require(value.get('owner') in ('github', 'server', 'paused'), 'INVALID_RUNTIME_OWNER')
    require(type(value.get('epoch')) is int and value['epoch'] > 0, 'INVALID_RUNTIME_EPOCH')
    require(value.get('jobs') == list(JOBS), 'INVALID_RUNTIME_JOBS')
    require(value.get('shadow_only') is True and value.get('automatic_failover') is False,
            'INVALID_RUNTIME_SAFETY')
    return value


def generation(source, owner, epoch, job, run_id, attempt=1):
    require(re.fullmatch(r'[a-f0-9]{40}', source) is not None, 'INVALID_SOURCE_COMMIT')
    require(owner in ('github', 'server') and job in JOBS, 'INVALID_RUN_CONTEXT')
    require(type(epoch) is int and epoch > 0, 'INVALID_RUN_EPOCH')
    require(re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', run_id) is not None, 'INVALID_RUN_ID')
    require(type(attempt) is int and attempt > 0, 'INVALID_RUN_ATTEMPT')
    identity = f'{source}:{owner}:{epoch}:{job}:{run_id}:{attempt}'
    return hashlib.sha256(identity.encode()).hexdigest()


def admission(owner, job, run_id, source=None, attempt=1):
    source = source or git('rev-parse', 'HEAD').stdout.strip()
    require(git('rev-parse', 'HEAD').stdout.strip() == source, 'SOURCE_HEAD_MISMATCH')
    config = read_config(source)
    require(owner in ('github', 'server') and job in JOBS, 'INVALID_RUN_CONTEXT')
    return {
        'admitted': config['owner'] == owner,
        'owner': owner, 'epoch': config['epoch'], 'job': job, 'run_id': run_id,
        'source_commit': source, 'attempt': attempt,
        'generation': generation(source, owner, config['epoch'], job, run_id, attempt),
    }


def from_environment():
    fields = ('SOURCE_COMMIT', 'WRITER', 'EPOCH', 'JOB', 'RUN_ID', 'GENERATION', 'ATTEMPT')
    require(all(os.environ.get('STOCK_SHADOW_' + field) for field in fields),
            'MISSING_STOCK_ADMISSION')
    a = admission(os.environ['STOCK_SHADOW_WRITER'], os.environ['STOCK_SHADOW_JOB'],
                  os.environ['STOCK_SHADOW_RUN_ID'], os.environ['STOCK_SHADOW_SOURCE_COMMIT'],
                  int(os.environ['STOCK_SHADOW_ATTEMPT']))
    require(a['admitted'], 'SOURCE_WRITER_NOT_ADMITTED')
    require(str(a['epoch']) == os.environ['STOCK_SHADOW_EPOCH'] and
            a['generation'] == os.environ['STOCK_SHADOW_GENERATION'], 'ADMISSION_BINDING_MISMATCH')
    return a


def environment(a):
    return {'STOCK_SHADOW_' + key: str(a[value]) for key, value in {
        'SOURCE_COMMIT': 'source_commit', 'WRITER': 'owner', 'EPOCH': 'epoch',
        'JOB': 'job', 'RUN_ID': 'run_id', 'GENERATION': 'generation', 'ATTEMPT': 'attempt',
    }.items()}


def check_fence(a, ref):
    config = read_config(ref)
    require(config['owner'] == a['owner'] and config['epoch'] == a['epoch'],
            'STOCK_WRITER_FENCED')


def main():
    p = argparse.ArgumentParser()
    p.add_argument('command', choices=['admit'])
    p.add_argument('--owner', choices=['github', 'server'], required=True)
    p.add_argument('--job', choices=JOBS, required=True)
    p.add_argument('--run-id', required=True)
    p.add_argument('--run-attempt', type=int, default=1)
    args = p.parse_args()
    a = admission(args.owner, args.job, args.run_id, attempt=args.run_attempt)
    # Even a freshly checked out workflow must bind current authority; malformed,
    # missing, changed or paused control state fails closed.
    git('fetch', '--quiet', 'origin', 'main')
    if a['admitted']:
        check_fence(a, 'origin/main')
    output = os.environ.get('GITHUB_OUTPUT')
    if output:
        with open(output, 'a') as f:
            f.write('should_run=' + str(a['admitted']).lower() + '\n')
    if a['admitted'] and os.environ.get('GITHUB_ENV'):
        with open(os.environ['GITHUB_ENV'], 'a') as f:
            for name, value in environment(a).items():
                f.write(name + '=' + value + '\n')
    print(json.dumps(a, sort_keys=True))


if __name__ == '__main__':
    main()
