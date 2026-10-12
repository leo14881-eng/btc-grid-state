"""Explicit server-only routing for existing jobs; no extra result writer.

The workflow's hosted/default route stays unchanged. Only an installed systemd
drop-in may select SERVER_DIRECT; conflicting selectors stop before job IO.
"""
import argparse
import json
import os
import runpy
import sys

MODULES = {'discovery': 'research.hunter_cex_scan',
           'availability': 'research.hunter_bybit_availability'}


def configure(job, env, platform=sys.platform):
    route = env.get('HUNTER_BYBIT_SERVER_ROUTE', 'DEFAULT')
    if route == 'DEFAULT':
        if env.get('HUNTER_BYBIT_SPOT_SOURCE', 'WORKER') != 'WORKER':
            raise ValueError('DEFAULT_ROUTE_REJECTS_DIRECT_OR_UNKNOWN_SPOT_SELECTOR')
        if env.get('HUNTER_BYBIT_KLINE_SOURCE', 'WORKER') != 'WORKER':
            raise ValueError('DEFAULT_ROUTE_REJECTS_CACHE_OR_UNKNOWN_KLINE_SELECTOR')
        if env.get('HUNTER_BYBIT_DIRECT_DISABLED', '1') != '1':
            raise ValueError('DEFAULT_ROUTE_REQUIRES_DIRECT_VETO')
        if job == 'discovery':
            # Preserve the original hosted-runner direct veto.
            env['HUNTER_BYBIT_DIRECT_DISABLED'] = '1'
        return route
    if route != 'SERVER_DIRECT':
        raise ValueError('SERVER_SPOT_ROUTE_UNKNOWN')
    if (platform != 'linux' or env.get('GITHUB_ACTIONS') == 'true'
            or env.get('HUNTER_RESEARCH_ISOLATED_CHECKOUT') != '1'):
        raise ValueError('SERVER_SPOT_REQUIRES_ISOLATED_LINUX_JOB')
    expected = {'HUNTER_BYBIT_SPOT_SOURCE': 'SERVER_DIRECT',
                'HUNTER_BYBIT_KLINE_SOURCE': 'SPOT_CACHE',
                'HUNTER_BYBIT_DIRECT_DISABLED': '0'}
    if any(env.get(key) != value for key, value in expected.items()):
        raise ValueError('SERVER_SPOT_EXPLICIT_SELECTORS_REQUIRED')
    return route


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('job', choices=MODULES)
    args = parser.parse_args()
    route = configure(args.job, os.environ)
    print('HUNTER_PUBLIC_SPOT_ROUTE '+json.dumps(dict(job=args.job, route=route)), flush=True)
    sys.argv = [MODULES[args.job]]
    runpy.run_module(MODULES[args.job], run_name='__main__')


if __name__ == '__main__':
    main()
