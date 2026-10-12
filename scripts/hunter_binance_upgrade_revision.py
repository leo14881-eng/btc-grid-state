"""Pinned revision adapter for the reviewed code-only upgrade; never recovers state."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import types

ENTRY = 'db0d59e3f7a6f15d1f99a678c07c2336e4474867'
ENTRY_HASH = 'e0615746c73809d258b858f4058cbd346b2df9e011a8d7df819f8e5f77808ddc'
PAIR = ('99836e5362340725f806d5df6df1ca9835065bdd',
        '3a940b3effba5e1f959659fbf83619ae6f10ba67')


def require_pair(old, new):
    if (old, new) != PAIR:
        raise ValueError('UNREVIEWED_RELEASE_PAIR')


def configure(source, old, new):
    require_pair(old, new)
    if hashlib.sha256(source).hexdigest() != ENTRY_HASH:
        raise ValueError('UPGRADE_ENTRY_HASH_MISMATCH')
    module = types.ModuleType('verified_code_only_upgrade')
    exec(compile(source, '<verified-code-only-upgrade>', 'exec'), module.__dict__)
    module.OLD, module.NEW = old, new
    return module


def load(repo, old, new):
    require_pair(old, new)
    source = subprocess.run(['git', '-C', str(repo), 'show',
        ENTRY+':scripts/hunter_binance_upgrade.py'], check=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=30, env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C.UTF-8'}).stdout
    return configure(source, old, new)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--expected-old', required=True)
    parser.add_argument('--expected-new', required=True)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--check', action='store_true')
    actions.add_argument('--apply', action='store_true')
    actions.add_argument('--rollback', type=Path)
    args = parser.parse_args(argv)
    require_pair(args.expected_old, args.expected_new)
    if not (args.check or args.apply or args.rollback):
        print(json.dumps(dict(status='PLAN_ONLY', old=args.expected_old, new=args.expected_new,
            upgrade_entry=ENTRY, state_action='PRESERVE_ALL_STATE', start=False, recovery=False)))
        return
    upgrade = load(args.repo, args.expected_old, args.expected_new)
    forwarded = ['verified-upgrade', '--repo', str(args.repo)]
    forwarded += ['--rollback', str(args.rollback)] if args.rollback else ['--apply' if args.apply else '--check']
    previous = sys.argv
    try:
        sys.argv = forwarded
        upgrade.main()
    finally:
        sys.argv = previous


if __name__ == '__main__': main()
