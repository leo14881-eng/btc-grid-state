"""Build a deterministic, offline installer bundle from pinned reviewed Git objects.

No network and no server operation. Destination must be new; never overwrite it.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile

OLD = 'f4e8ad4de377a19763f4803e4fd56882c27d887a'
NEW = '33abf19c7a91855ba2efc2e65e1d383055494d75'
HELPERS = {
    'upgrade.py': ('db0d59e3f7a6f15d1f99a678c07c2336e4474867', 'scripts/hunter_binance_upgrade.py',
                   'e0615746c73809d258b858f4058cbd346b2df9e011a8d7df819f8e5f77808ddc'),
    'evidence.py': ('7bd133584a21d5cc2af1147594f24031e29fd635', 'scripts/hunter_binance_recover_three_once.py',
                    '05aab679ee1f4b3cc53ad4fcf366959340fe79a0856e55fa066b07a77c93fdfa')}
CONFIG = 'research/results/hunter-shadow-rules.json'
UNITS = {f'deploy/systemd/hunter-{v}-stream.service' for v in ('binance', 'bybit')}
CHANGED = {'research/hunter_binance_http_child.py', 'research/hunter_binance_public.py',
    'research/hunter_binance_stream.py', 'research/hunter_binance_stream_consumer.py',
    'research/hunter_binance_stream_store.py', 'scripts/check_binance_public_transport.py'}
ADDED = {'research/hunter_binance_health.py', 'research/hunter_binance_health_store.py',
    'scripts/check_binance_health_offline.py', 'scripts/hunter_binance_health_check.py'}


def sha(data): return hashlib.sha256(data).hexdigest()


def git(root, *args):
    return subprocess.check_output(['git', '-c', 'core.autocrlf=false', '-C', str(root), *args], timeout=60)


def runtime(root, head):
    assert git(root, 'rev-parse', head + '^{commit}').decode().strip() == head
    all_names = git(root, 'ls-tree', '-r', '--name-only', head).decode().splitlines()
    names = sorted(n for n in all_names if (n.endswith('.py') and n.startswith(('research/', 'scripts/')))
                   or n == CONFIG or n in UNITS)
    data = git(root, 'archive', '--format=tar', head, '--', *names)
    files = {}
    with tarfile.open(fileobj=io.BytesIO(data)) as bundle:
        for item in bundle:
            if item.isdir(): continue
            assert item.isfile() and item.name in names and item.name not in files
            payload = bundle.extractfile(item).read()
            files[item.name] = {'sha256': sha(payload), 'mode': 0o555 if item.mode & 0o111 else 0o444}
    assert set(names) == files.keys()
    return data, files


def build(root, target):
    old, old_files = runtime(root, OLD); new, new_files = runtime(root, NEW)
    assert set(new_files) - set(old_files) == ADDED
    assert not set(old_files) - set(new_files)
    assert {p for p in old_files if old_files[p] != new_files[p]} == CHANGED
    lock = {'schema': 'binance_health_upgrade_bundle_v1', 'old': OLD, 'new': NEW,
            'old_files': old_files, 'new_files': new_files,
            'old_archive_sha256': sha(old), 'new_archive_sha256': sha(new)}
    lock_bytes = (json.dumps(lock, sort_keys=True, separators=(',', ':')) + '\n').encode()
    files = {'runtime-old.tar': old, 'runtime-new.tar': new, 'runtime-lock.json': lock_bytes}
    for name, (commit, path, expected) in HELPERS.items():
        files[name] = git(root, 'show', commit + ':' + path)
        assert sha(files[name]) == expected
    files['entry.py'] = (root / 'scripts/hunter_binance_health_upgrade_once.py').read_text(encoding='utf-8').encode()
    files['launch.sh'] = (root / 'scripts/hunter_binance_health_upgrade_launch.sh').read_text(encoding='utf-8').encode()
    files['journal.py'] = (root / 'scripts/hunter_binance_health_upgrade_journal.py').read_text(encoding='utf-8').encode()
    assert ('JOURNAL_HASH = ' + repr(sha(files['journal.py']))).encode() in files['entry.py']
    # entry embeds the exact runtime lock hash; change needs another review.
    assert ('LOCK_HASH = ' + repr(sha(lock_bytes))).encode() in files['entry.py']
    assert not target.exists()
    target.mkdir()
    for name, payload in sorted(files.items()):
        (target / name).write_bytes(payload)
    manifest = {name: sha(payload) for name, payload in sorted(files.items())}
    (target / 'SHA256SUMS').write_text(''.join(digest + '  ' + name + '\n' for name, digest in manifest.items()), encoding='ascii', newline='\n')
    print(json.dumps({'bundle': str(target), 'old': OLD, 'new': NEW, 'files': manifest,
                      'old_runtime_files': len(old_files), 'new_runtime_files': len(new_files)}, sort_keys=True))
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    build(Path(__file__).resolve().parents[1], args.output.absolute())


if __name__ == '__main__': main()
