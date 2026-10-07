"""CAS publication for discovery/replay; never publish shadow portfolios."""
import argparse
import pathlib
import subprocess
import tempfile

from scripts.hunter_job_runner import (output_paths, push_failure_category,
                                       push_failure_exception, safe_diagnostic)
from scripts.hunter_monitor_persist import git, require


def paths_for(job):
    require(job in ('discovery', 'blind-replay', 'missed-replay'), 'INVALID_AUX_JOB')
    paths = ['research/results/' + p for p in output_paths(job)]
    if job == 'blind-replay':
        paths.append('hunter-replay-v1.json')
    return paths


def protected(path, job):
    if path in paths_for(job):
        return True
    if job == 'discovery' and path in (
            'research/results/hunter-early-signals.json',
            'research/results/hunter-forward-research.json'):
        return True
    return (path.startswith(('research/hunter_', 'scripts/hunter_',
                            'tests/test_hunter', '.github/workflows/hunter-',
                            'deploy/systemd/hunter-')) or
            path in ('.github/hunter-runtime.json',
                     'research/results/hunter-shadow-rules.json'))


def cas(base, job):
    git('fetch', 'origin', 'main')
    changed = git('diff', '--name-only', base, 'origin/main').stdout.splitlines()
    conflicts = [p for p in changed if protected(p, job)]
    require(not conflicts, 'AUX_CAS_REJECTED_STALE_WRITER ' + ' '.join(conflicts))


def persist(job, base):
    allowed = paths_for(job)
    expected = {p: pathlib.Path(p).read_bytes() for p in allowed if pathlib.Path(p).is_file()}
    required = allowed[:4] if job in ('discovery', 'blind-replay') else allowed
    require(all(p in expected for p in required), 'AUX_OUTPUT_MISSING')
    require(all(expected[p] for p in required), 'AUX_OUTPUT_EMPTY')
    remote = git('remote', 'get-url', 'origin').stdout.strip()
    # actions/checkout stores its scoped authentication header in local config.
    # Local clones do not inherit it. Keep it inside the disposable checkout;
    # never print it or embed it in the remote URL.
    headers = git('config', '--local', '--get-regexp',
                  r'^http\..*\.extraheader$', check=False).stdout.splitlines()
    last_push = None
    for attempt in range(1, 6):
        cas(base, job)
        with tempfile.TemporaryDirectory(prefix='hunter-aux-') as directory:
            subprocess.run(['git', 'clone', '--quiet', '--shared', '.', directory], check=True)
            def aux(*args, check=True):
                return subprocess.run(['git', '-C', directory, *args], text=True,
                                      capture_output=True, check=check)
            aux('remote', 'set-url', 'origin', remote)
            for header in headers:
                key, value = header.split(' ', 1)
                aux('config', '--local', key, value)
            aux('fetch', 'origin', 'main')
            # Recheck after the second fetch; a newer same-job writer must win.
            changed = aux('diff', '--name-only', base, 'origin/main').stdout.splitlines()
            require(not any(protected(p, job) for p in changed), 'AUX_CAS_REJECTED_STALE_WRITER')
            aux('checkout', '--detach', 'origin/main')
            for path, raw in expected.items():
                target = pathlib.Path(directory, path)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw)
            aux('config', 'user.name', 'hunter-shadow-publisher')
            aux('config', 'user.email', 'hunter-shadow-publisher@localhost')
            aux('add', '-f', '--', *expected)
            if aux('diff', '--cached', '--quiet', check=False).returncode:
                aux('commit', '-m', 'state: publish verified Hunter ' + job)
                pushed = aux('push', 'origin', 'HEAD:main', check=False)
                if pushed.returncode:
                    category = push_failure_category(pushed)
                    last_push = pushed
                    if category not in ('RACE', 'TRANSPORT_FAILED'):
                        raise push_failure_exception('AUX_PUSH', pushed, category)
                    print('HUNTER_AUX_PUSH_RETRY', job, attempt, category,
                          safe_diagnostic(pushed.stderr), flush=True)
                    continue
            commit = aux('rev-parse', 'HEAD').stdout.strip()
            aux('fetch', 'origin', 'main')
            require(aux('merge-base', '--is-ancestor', commit, 'origin/main',
                        check=False).returncode == 0, 'AUX_COMMIT_NOT_AUTHORITATIVE')
            for path, raw in expected.items():
                actual = subprocess.run(['git', '-C', directory, 'show', 'origin/main:' + path],
                                        capture_output=True, check=True).stdout
                require(actual == raw, 'AUX_MAIN_READBACK_MISMATCH ' + path)
            print('HUNTER_AUX_MAIN_READBACK_OK', job, commit, len(expected), flush=True)
            return commit
    raise push_failure_exception('AUX_PUSH_RETRY_EXHAUSTED', last_push)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('job', choices=('discovery', 'blind-replay', 'missed-replay'))
    args = parser.parse_args()
    persist(args.job, git('rev-parse', 'HEAD').stdout.strip())


if __name__ == '__main__':
    main()
