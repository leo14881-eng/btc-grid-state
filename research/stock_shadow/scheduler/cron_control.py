"""Operator-only stock cron removal. No Worker deploy, credentials or ledger writes.

Cloudflare schedules have no cross-provider compare-and-swap. This command needs
the sole cutover operator to hold paused/epoch 2 throughout, plus the existing
Actions deployment concurrency group. Any observed drift fails closed. A fresh
empty GET proves stored configuration, not completion of the 15m propagation.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone

WORKER = 'stock-shadow-scheduler'
REPOSITORY = 'leo14881-eng/btc-grid-state'
ORIGINAL_CRON = '3,8,13,18,23,28,33,38,43,48,53,58 * * * *'
CONTROL = '.github/stock-runtime.json'
PROTECTED = (CONTROL, 'research/stock_shadow/scheduler',
             '.github/workflows/deploy-stock-shadow-scheduler.yml')
UUID = r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}'


class ControlError(RuntimeError):
    """Only fixed, non-secret error codes may leave this module."""


class APIError(ControlError):
    def __init__(self, code, uncertain=False):
        super().__init__(code)
        self.uncertain = uncertain


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def require(condition, code):
    if not condition:
        raise ControlError(code)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    allow_nan=False).encode()).hexdigest()


class Cloudflare:
    def __init__(self, account, token):
        require(isinstance(account, str) and re.fullmatch(r'[a-fA-F0-9]{32}', account),
                'INVALID_ACCOUNT_ID')
        require(isinstance(token, str) and bool(token), 'MISSING_STOCK_CF_TOKEN')
        self.base = ('https://api.cloudflare.com/client/v4/accounts/' + account +
                     '/workers/scripts/' + WORKER + '/')
        self.token = token
        self.evidence = []

    def call(self, method, resource, payload=None):
        require((method == 'GET' and resource in ('schedules', 'settings', 'deployments')
                 and payload is None) or
                (method == 'PUT' and resource == 'schedules' and payload == []),
                'FORBIDDEN_PROVIDER_OPERATION')
        headers = {'Authorization': 'Bearer ' + self.token,
                   'User-Agent': 'stock-shadow-cron-cutover/1.0'}
        data = None
        if method == 'PUT':
            headers['Content-Type'] = 'application/json'
            data = b'[]'
        request = urllib.request.Request(self.base + resource, data=data,
                                         headers=headers, method=method)
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=20) as response:
                status = response.status
                raw = response.read(1048577)
                ray = response.headers.get('CF-Ray', '')
        except urllib.error.HTTPError as exc:
            # No raw exception/body/URL/headers: they may contain credentials.
            raise APIError('CF_HTTP_' + str(exc.code),
                           uncertain=method == 'PUT' and exc.code >= 500) from None
        except Exception:
            raise APIError('CF_TRANSPORT_UNKNOWN', uncertain=method == 'PUT') from None
        self.evidence.append({'method': method, 'resource': resource, 'http_status': status,
                              'cf_ray': ray if re.fullmatch(r'[A-Za-z0-9-]{1,100}', ray) else None})
        try:
            require(200 <= status < 300 and len(raw) <= 1048576, 'CF_RESPONSE_INVALID')
            body = json.loads(raw)
            require(isinstance(body, dict), 'CF_RESPONSE_INVALID')
        except Exception:
            raise APIError('CF_RESPONSE_UNVERIFIED', uncertain=method == 'PUT') from None
        if body.get('success') is False:
            raise APIError('CF_API_REJECTED', uncertain=False)
        if body.get('success') is not True or 'result' not in body:
            raise APIError('CF_RESPONSE_UNVERIFIED', uncertain=method == 'PUT')
        return body['result']


def cron_list(value):
    require(isinstance(value, dict) and isinstance(value.get('schedules'), list),
            'SCHEDULE_RESPONSE_INVALID')
    rows = value['schedules']
    require(all(isinstance(row, dict) and isinstance(row.get('cron'), str) for row in rows),
            'SCHEDULE_RESPONSE_INVALID')
    crons = [row['cron'] for row in rows]
    require(crons in ([], [ORIGINAL_CRON]), 'UNEXPECTED_STOCK_SCHEDULES')
    return crons


def metadata(api, expected_version):
    settings = api.call('GET', 'settings')
    deployments = api.call('GET', 'deployments')
    require(isinstance(settings, dict) and isinstance(settings.get('bindings'), list),
            'STOCK_SETTINGS_INVALID')
    require(isinstance(deployments, dict) and isinstance(deployments.get('deployments'), list)
            and deployments['deployments'], 'STOCK_DEPLOYMENTS_INVALID')
    current = deployments['deployments'][0]
    require(isinstance(current, dict) and current.get('versions') ==
            [{'version_id': expected_version, 'percentage': 100}], 'STOCK_VERSION_CHANGED')
    return {'settings_sha256': digest(settings), 'deployments_sha256': digest(deployments)}


def control_operation(api, authority, expected_version, *, mutate=False):
    """Fakeable state machine. authority() freshly reads Git on every invocation."""
    require(isinstance(expected_version, str) and re.fullmatch(UUID, expected_version),
            'INVALID_EXPECTED_VERSION')
    require(type(mutate) is bool, 'INVALID_MUTATION_MODE')
    authority_before = authority()
    before_metadata = metadata(api, expected_version)
    before = cron_list(api.call('GET', 'schedules'))
    # Recheck both stores immediately before the only possible mutation.
    require(metadata(api, expected_version) == before_metadata, 'STOCK_METADATA_DRIFT')
    require(cron_list(api.call('GET', 'schedules')) == before, 'STOCK_SCHEDULE_DRIFT')
    authority_pre_put = authority()
    put_attempted = False
    put_uncertain = False
    if mutate and before:
        put_attempted = True
        try:
            require(cron_list(api.call('PUT', 'schedules', [])) == [],
                    'PUT_RESPONSE_NOT_EMPTY')
        except APIError as exc:
            if not exc.uncertain:
                raise
            put_uncertain = True
        except ControlError:
            # A malformed PUT response may still have applied. Do not retry it.
            put_uncertain = True
    after = cron_list(api.call('GET', 'schedules'))
    if mutate:
        require(after == [], 'STOCK_CRON_REMOVAL_NOT_VERIFIED')
    else:
        require(after == before, 'STOCK_SCHEDULE_DRIFT')
    require(metadata(api, expected_version) == before_metadata, 'STOCK_METADATA_DRIFT')
    authority_after = authority()
    return {'schema': 'stock_cron_control_v1', 'checked_at': datetime.now(timezone.utc).isoformat(),
            'worker': WORKER, 'mode': 'disable' if mutate else 'inspect',
            'before_crons': before, 'after_crons': after,
            'put_attempted': put_attempted, 'put_response_uncertain': put_uncertain,
            'stored_configuration_empty': after == [], 'propagation_verified': False,
            'propagation_wait_seconds': 900, 'expected_version': expected_version,
            'authority_before': authority_before, 'authority_pre_put': authority_pre_put,
            'authority_after': authority_after, **before_metadata}


def git(*args):
    try:
        return subprocess.run(['git', *args], text=True, capture_output=True,
                              timeout=60, check=True).stdout.strip()
    except Exception:
        raise ControlError('GIT_AUTHORITY_READ_FAILED') from None


def git_authority(expected_source):
    require(re.fullmatch(r'[a-f0-9]{40}', expected_source) is not None, 'INVALID_EXPECTED_SOURCE')
    require(os.environ.get('GITHUB_REPOSITORY') == REPOSITORY, 'UNEXPECTED_REPOSITORY')
    require(os.environ.get('GITHUB_REF') == 'refs/heads/main', 'MAIN_ONLY')
    git('diff', '--quiet', expected_source, 'HEAD', '--', *PROTECTED)
    git('fetch', '--quiet', '--no-tags', 'origin', 'main')
    source = git('rev-parse', 'origin/main')
    git('diff', '--quiet', expected_source, source, '--', *PROTECTED)
    try:
        control = json.loads(git('show', source + ':' + CONTROL))
    except Exception:
        raise ControlError('RUNTIME_AUTHORITY_INVALID') from None
    require(isinstance(control, dict) and type(control.get('epoch')) is int and
            control.get('shadow_only') is True and control.get('automatic_failover') is False and
            control == {'schema': 'stock_shadow_runtime_v1', 'owner': 'paused', 'epoch': 2,
                        'jobs': ['main', 'monitor', 'replay'], 'shadow_only': True,
                        'automatic_failover': False}, 'CUTOVER_NOT_PAUSED_EPOCH_2')
    return {'main_sha': source, 'runtime_sha256': digest(control), 'owner': 'paused', 'epoch': 2}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('inspect', 'disable'))
    parser.add_argument('--expected-source', required=True)
    parser.add_argument('--expected-version', required=True)
    args = parser.parse_args()
    try:
        api = Cloudflare(os.environ.get('CLOUDFLARE_ACCOUNT_ID'),
                         os.environ.get('CLOUDFLARE_API_TOKEN'))
        receipt = control_operation(api, lambda: git_authority(args.expected_source),
                                    args.expected_version, mutate=args.mode == 'disable')
        receipt['requests'] = api.evidence
        print(json.dumps(receipt, sort_keys=True))
    except ControlError as exc:
        print(json.dumps({'status': 'FAILED_OR_UNVERIFIED', 'error': str(exc),
                          'automatic_retry': False, 'keep_authority_paused': True}))
        raise SystemExit(1) from None
    except Exception:
        print(json.dumps({'status': 'FAILED_OR_UNVERIFIED', 'error': 'CONTROL_FAILED',
                          'automatic_retry': False, 'keep_authority_paused': True}))
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
