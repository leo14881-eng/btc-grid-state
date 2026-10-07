"""Offline regressions for the stock-only, single-mutation cron cutover helper.

All provider requests, runtime authority reads, and subprocesses are fakes. These
checks must never use live Cloudflare credentials or modify a provider resource.
"""
import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
import urllib.error

import pytest

from research.stock_shadow.scheduler import cron_control as control


VERSION = '12345678-1234-1234-1234-123456789abc'
OTHER_VERSION = '87654321-4321-4321-4321-cba987654321'
SOURCE = 'a' * 40
FRESH_SOURCE = 'b' * 40
ACCOUNT = 'c' * 32
TOKEN = 'offline-token-never-print'
SECRET_DETAIL = 'offline-private-exception-never-print'
PAUSED = {
    'schema': 'stock_shadow_runtime_v1', 'owner': 'paused', 'epoch': 2,
    'jobs': ['main', 'monitor', 'replay'], 'shadow_only': True,
    'automatic_failover': False,
}
SETTINGS = {'bindings': [
    {'name': 'STOCK_SCHEDULER', 'type': 'durable_object_namespace',
     'class_name': 'StockScheduler', 'namespace_id': 'namespace-do-not-log'},
    {'name': 'GITHUB_ACTIONS_TOKEN', 'type': 'secret_text'},
]}
DEPLOYMENTS = {'deployments': [{
    'id': 'd' * 36, 'strategy': 'percentage',
    'versions': [{'version_id': VERSION, 'percentage': 100}],
}]}


def schedules(*crons):
    return {'schedules': [{'cron': cron} for cron in crons]}


@pytest.fixture(autouse=True)
def forbid_live_network_and_processes(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('offline cron-control test attempted live I/O')
    monkeypatch.setattr(control.urllib.request, 'urlopen', forbidden)
    monkeypatch.setattr(control.urllib.request, 'build_opener', forbidden)
    monkeypatch.setattr(control.subprocess, 'run', forbidden)


class ScriptedAPI:
    def __init__(self, *, before=None, after=None, events=None):
        before = schedules(control.ORIGINAL_CRON) if before is None else before
        after = schedules() if after is None else after
        self.events = events if events is not None else []
        self.responses = {
            ('GET', 'settings'): [copy.deepcopy(SETTINGS) for _ in range(3)],
            ('GET', 'deployments'): [copy.deepcopy(DEPLOYMENTS) for _ in range(3)],
            ('GET', 'schedules'): [copy.deepcopy(before), copy.deepcopy(before),
                                   copy.deepcopy(after)],
            ('PUT', 'schedules'): [schedules()],
        }

    def call(self, method, resource, payload=None):
        self.events.append((method, resource, copy.deepcopy(payload)))
        assert ((method == 'GET' and resource in ('settings', 'deployments', 'schedules')
                 and payload is None) or
                (method == 'PUT' and resource == 'schedules' and payload == []))
        queue = self.responses[(method, resource)]
        assert queue, 'unexpected repeat provider request'
        value = queue.pop(0)
        if isinstance(value, BaseException):
            raise value
        return copy.deepcopy(value)

    @property
    def puts(self):
        return [event for event in self.events if event[0] == 'PUT']


class Authority:
    def __init__(self, events=None, fail_on=None):
        self.events = events if events is not None else []
        self.calls = 0
        self.fail_on = fail_on

    def __call__(self):
        self.calls += 1
        self.events.append(('AUTHORITY', self.calls, None))
        if self.calls == self.fail_on:
            raise control.ControlError('CUTOVER_NOT_PAUSED_EPOCH_2')
        return {'main_sha': str(self.calls) * 40, 'runtime_sha256': control.digest(PAUSED),
                'owner': 'paused', 'epoch': 2}


def operate(api=None, *, mutate=True, authority=None, version=VERSION):
    api = api or ScriptedAPI()
    authority = authority or Authority(api.events)
    return control.control_operation(api, authority, version, mutate=mutate)


def test_removes_only_exact_stock_cron_once_and_refreshes_authority():
    api = ScriptedAPI()
    authority = Authority(api.events)
    result = operate(api, authority=authority)
    assert api.puts == [('PUT', 'schedules', [])]
    put_index = api.events.index(api.puts[0])
    assert api.events[put_index - 1] == ('AUTHORITY', 2, None)
    assert api.events[0] == ('AUTHORITY', 1, None)
    assert api.events[-1] == ('AUTHORITY', 3, None)
    assert result['worker'] == 'stock-shadow-scheduler'
    assert result['mode'] == 'disable'
    assert result['before_crons'] == [control.ORIGINAL_CRON]
    assert result['after_crons'] == []
    assert result['put_attempted'] is True
    assert result['put_response_uncertain'] is False
    assert result['stored_configuration_empty'] is True
    assert result['propagation_verified'] is False
    assert result['propagation_wait_seconds'] == 900
    assert result['authority_before']['main_sha'] != result['authority_after']['main_sha']
    assert result['settings_sha256'] == control.digest(SETTINGS)
    assert result['deployments_sha256'] == control.digest(DEPLOYMENTS)
    serialized = json.dumps(result)
    for private in (ACCOUNT, TOKEN, 'namespace-do-not-log', 'GITHUB_ACTIONS_TOKEN'):
        assert private not in serialized
    assert all(not queue for queue in api.responses.values())


def test_already_empty_is_idempotent_and_still_rechecks_metadata_and_authority():
    api = ScriptedAPI(before=schedules())
    authority = Authority(api.events)
    result = operate(api, authority=authority)
    assert api.puts == []
    assert result['put_attempted'] is False
    assert result['stored_configuration_empty'] is True
    assert authority.calls == 3
    assert len([event for event in api.events if event[:2] == ('GET', 'settings')]) == 3
    assert len([event for event in api.events if event[:2] == ('GET', 'deployments')]) == 3


@pytest.mark.parametrize('crons', [[], [control.ORIGINAL_CRON]])
def test_inspect_never_mutates(crons):
    api = ScriptedAPI(before=schedules(*crons), after=schedules(*crons))
    result = operate(api, mutate=False)
    assert api.puts == []
    assert result['mode'] == 'inspect'
    assert result['before_crons'] == result['after_crons'] == crons
    assert result['stored_configuration_empty'] is (crons == [])


@pytest.mark.parametrize('value', [None, '', OTHER_VERSION + '/schedules', VERSION.upper(),
                                    '../stock-shadow-scheduler', 'a' * 36])
def test_invalid_expected_version_blocks_all_reads(value):
    api = ScriptedAPI()
    with pytest.raises(control.ControlError, match='INVALID_EXPECTED_VERSION'):
        operate(api, version=value)
    assert api.events == []


@pytest.mark.parametrize('value', [1, 0, 'true', None, []])
def test_mutation_mode_requires_actual_boolean(value):
    api = ScriptedAPI()
    with pytest.raises(control.ControlError, match='INVALID_MUTATION_MODE'):
        operate(api, mutate=value)
    assert api.events == []


@pytest.mark.parametrize('value', [None, [], {}, {'schedules': None},
    {'schedules': [None]}, {'schedules': [{}]}, {'schedules': [{'cron': 5}]},
    schedules('* * * * *'), schedules(control.ORIGINAL_CRON, control.ORIGINAL_CRON),
    schedules(control.ORIGINAL_CRON, '0 1 * * *')])
def test_bad_initial_schedules_fail_without_mutation(value):
    api = ScriptedAPI()
    api.responses[('GET', 'schedules')][0] = value
    with pytest.raises(control.ControlError):
        operate(api)
    assert api.puts == []


def test_schedule_change_between_preflight_reads_blocks_put():
    api = ScriptedAPI()
    api.responses[('GET', 'schedules')][1] = schedules()
    with pytest.raises(control.ControlError, match='STOCK_SCHEDULE_DRIFT'):
        operate(api)
    assert api.puts == []


@pytest.mark.parametrize('resource', ['settings', 'deployments'])
@pytest.mark.parametrize('read_index', [1, 2])
def test_metadata_drift_before_or_after_put_fails_closed(resource, read_index):
    api = ScriptedAPI()
    api.responses[('GET', resource)][read_index]['unexpected_change'] = 'do-not-log'
    with pytest.raises(control.ControlError, match='STOCK_METADATA_DRIFT'):
        operate(api)
    assert len(api.puts) == (0 if read_index == 1 else 1)


@pytest.mark.parametrize('read_index', [0, 1, 2])
def test_current_version_change_never_passes(read_index):
    api = ScriptedAPI()
    row = api.responses[('GET', 'deployments')][read_index]['deployments'][0]
    row['versions'][0]['version_id'] = OTHER_VERSION
    with pytest.raises(control.ControlError, match='STOCK_VERSION_CHANGED'):
        operate(api)
    assert len(api.puts) == (1 if read_index == 2 else 0)


@pytest.mark.parametrize('versions', [[], [{'version_id': VERSION, 'percentage': 99}],
    [{'version_id': VERSION, 'percentage': 50},
     {'version_id': OTHER_VERSION, 'percentage': 50}],
    [{'version_id': VERSION, 'percentage': 100, 'extra': True}],
    [{'version_id': OTHER_VERSION, 'percentage': 100}]])
def test_mixed_or_unexpected_deployment_cannot_authorize_removal(versions):
    api = ScriptedAPI()
    api.responses[('GET', 'deployments')][0]['deployments'][0]['versions'] = versions
    with pytest.raises(control.ControlError, match='STOCK_VERSION_CHANGED'):
        operate(api)
    assert api.puts == []


@pytest.mark.parametrize('resource,value', [
    ('settings', None), ('settings', {}), ('settings', {'bindings': {}}),
    ('deployments', None), ('deployments', {}),
    ('deployments', {'deployments': []}), ('deployments', {'deployments': [None]}),
])
def test_malformed_metadata_fails_before_mutation(resource, value):
    api = ScriptedAPI()
    api.responses[('GET', resource)][0] = value
    with pytest.raises(control.ControlError):
        operate(api)
    assert api.puts == []


@pytest.mark.parametrize('fail_on', [1, 2, 3])
def test_authority_drift_at_any_read_cannot_report_success(fail_on):
    api = ScriptedAPI()
    authority = Authority(api.events, fail_on=fail_on)
    with pytest.raises(control.ControlError, match='CUTOVER_NOT_PAUSED_EPOCH_2'):
        operate(api, authority=authority)
    assert len(api.puts) == (1 if fail_on == 3 else 0)
    assert authority.calls == fail_on


@pytest.mark.parametrize('put_reply', [
    control.APIError('CF_TRANSPORT_UNKNOWN', uncertain=True),
    control.APIError('CF_HTTP_503', uncertain=True),
    control.APIError('CF_RESPONSE_UNVERIFIED', uncertain=True),
    None, {}, {'schedules': 'invalid'},
])
def test_uncertain_put_reconciles_with_gets_without_retry(put_reply):
    api = ScriptedAPI()
    api.responses[('PUT', 'schedules')] = [put_reply]
    result = operate(api)
    assert len(api.puts) == 1
    assert result['put_response_uncertain'] is True
    assert result['after_crons'] == []
    put_index = next(i for i, event in enumerate(api.events) if event[0] == 'PUT')
    assert all(event[0] in ('GET', 'AUTHORITY') for event in api.events[put_index + 1:])


@pytest.mark.parametrize('uncertain', [False, True])
def test_unremoved_cron_after_put_never_succeeds_or_retries(uncertain):
    api = ScriptedAPI(after=schedules(control.ORIGINAL_CRON))
    if uncertain:
        api.responses[('PUT', 'schedules')] = [control.APIError('CF_TRANSPORT_UNKNOWN', True)]
    with pytest.raises(control.ControlError, match='STOCK_CRON_REMOVAL_NOT_VERIFIED'):
        operate(api)
    assert len(api.puts) == 1


def test_definite_put_rejection_is_not_retried():
    api = ScriptedAPI()
    api.responses[('PUT', 'schedules')] = [control.APIError('CF_HTTP_403')]
    with pytest.raises(control.APIError, match='CF_HTTP_403'):
        operate(api)
    assert len(api.puts) == 1


def test_failed_reconciliation_never_retries_mutation():
    api = ScriptedAPI()
    api.responses[('PUT', 'schedules')] = [control.APIError('CF_TRANSPORT_UNKNOWN', True)]
    api.responses[('GET', 'schedules')][2] = control.APIError('CF_HTTP_503')
    with pytest.raises(control.APIError, match='CF_HTTP_503'):
        operate(api)
    assert len(api.puts) == 1


def test_inspect_detects_schedule_drift_in_final_read():
    api = ScriptedAPI()
    with pytest.raises(control.ControlError, match='STOCK_SCHEDULE_DRIFT'):
        operate(api, mutate=False)
    assert api.puts == []


def fake_git_authority(monkeypatch, runtime=None, *, failure=None, raw=None):
    monkeypatch.setenv('GITHUB_REPOSITORY', control.REPOSITORY)
    monkeypatch.setenv('GITHUB_REF', 'refs/heads/main')
    calls = []
    def fake_git(*args):
        calls.append(args)
        if args[0] == failure:
            raise control.ControlError('GIT_AUTHORITY_READ_FAILED')
        if args[0] == 'rev-parse':
            return FRESH_SOURCE
        if args[0] == 'show':
            return raw if raw is not None else json.dumps(PAUSED if runtime is None else runtime)
        return ''
    monkeypatch.setattr(control, 'git', fake_git)
    return calls


def test_git_authority_fetches_main_and_checks_protected_content_every_time(monkeypatch):
    calls = fake_git_authority(monkeypatch)
    for _ in range(2):
        receipt = control.git_authority(SOURCE)
        assert receipt['main_sha'] == FRESH_SOURCE
        assert receipt['owner'] == 'paused' and receipt['epoch'] == 2
    expected = [
        ('diff', '--quiet', SOURCE, 'HEAD', '--', *control.PROTECTED),
        ('fetch', '--quiet', '--no-tags', 'origin', 'main'),
        ('rev-parse', 'origin/main'),
        ('diff', '--quiet', SOURCE, FRESH_SOURCE, '--', *control.PROTECTED),
        ('show', FRESH_SOURCE + ':' + control.CONTROL),
    ]
    assert calls == expected * 2


@pytest.mark.parametrize('field,value', [
    ('owner', 'github'), ('owner', 'server'), ('epoch', 1), ('epoch', 3),
    ('epoch', 2.0), ('epoch', '2'), ('shadow_only', False), ('shadow_only', 1),
    ('automatic_failover', True), ('automatic_failover', 0),
    ('schema', 'unknown'), ('jobs', ['main', 'monitor']),
])
def test_git_authority_requires_strict_paused_epoch_2_schema(monkeypatch, field, value):
    runtime = copy.deepcopy(PAUSED)
    runtime[field] = value
    fake_git_authority(monkeypatch, runtime)
    with pytest.raises(control.ControlError, match='CUTOVER_NOT_PAUSED_EPOCH_2'):
        control.git_authority(SOURCE)


@pytest.mark.parametrize('raw', ['not-json', 'null', '[]', '{}'])
def test_git_authority_rejects_malformed_runtime(monkeypatch, raw):
    fake_git_authority(monkeypatch, raw=raw)
    with pytest.raises(control.ControlError):
        control.git_authority(SOURCE)


@pytest.mark.parametrize('failure', ['fetch', 'rev-parse', 'diff', 'show'])
def test_git_authority_read_or_protected_source_failure_blocks(monkeypatch, failure):
    fake_git_authority(monkeypatch, failure=failure)
    with pytest.raises(control.ControlError):
        control.git_authority(SOURCE)


@pytest.mark.parametrize('env,value', [('GITHUB_REPOSITORY', 'different/repository'),
                                     ('GITHUB_REF', 'refs/heads/feature')])
def test_git_authority_rejects_wrong_repository_or_ref_before_fetch(monkeypatch, env, value):
    calls = fake_git_authority(monkeypatch)
    monkeypatch.setenv(env, value)
    with pytest.raises(control.ControlError):
        control.git_authority(SOURCE)
    assert calls == []


@pytest.mark.parametrize('source', ['', 'not-a-sha', SOURCE + '/main', SOURCE.upper()])
def test_git_authority_rejects_invalid_expected_source_before_fetch(monkeypatch, source):
    calls = fake_git_authority(monkeypatch)
    with pytest.raises(control.ControlError, match='INVALID_EXPECTED_SOURCE'):
        control.git_authority(source)
    assert calls == []


class Response(io.BytesIO):
    def __init__(self, body, status=200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = headers or {}


def install_transport(monkeypatch, *, result=None, raw=None, error=None, status=200,
                      headers=None):
    calls = []
    built_handlers = []
    def open_request(request, timeout):
        calls.append(request)
        assert timeout > 0
        if error is not None:
            raise error
        body = raw if raw is not None else json.dumps({'success': True,
            'result': schedules() if result is None else result}).encode()
        return Response(body, status, headers)
    class Opener:
        open = staticmethod(open_request)
    def build(*handlers):
        built_handlers.extend(handlers)
        return Opener()
    monkeypatch.setattr(control.urllib.request, 'build_opener', build)
    return calls, built_handlers


def test_cf_transport_is_hardcoded_to_stock_and_sends_only_empty_json_put(monkeypatch):
    requests, _ = install_transport(monkeypatch, headers={'CF-Ray': 'abc123-LHR'})
    api = control.Cloudflare(ACCOUNT, TOKEN)
    assert api.call('PUT', 'schedules', []) == schedules()
    assert len(requests) == 1
    request = requests[0]
    assert request.full_url == (
        f'https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}'
        '/workers/scripts/stock-shadow-scheduler/schedules')
    assert request.get_method() == 'PUT' and request.data == b'[]'
    assert request.get_header('Authorization') == 'Bearer ' + TOKEN
    assert request.get_header('Content-type') == 'application/json'
    assert api.evidence == [{'method': 'PUT', 'resource': 'schedules',
                             'http_status': 200, 'cf_ray': 'abc123-LHR'}]
    assert TOKEN not in json.dumps(api.evidence) and ACCOUNT not in json.dumps(api.evidence)


@pytest.mark.parametrize('method,resource,payload', [
    ('POST', 'schedules', []), ('DELETE', 'schedules', None),
    ('PUT', 'settings', []), ('PUT', 'deployments', []),
    ('PUT', 'schedules', [{'cron': control.ORIGINAL_CRON}]),
    ('PUT', 'schedules', None), ('GET', 'secrets', None),
    ('GET', '../hunter-scheduler/settings', None),
    ('GET', 'schedules?per_page=100', None), ('GET', 'schedules', []),
])
def test_cf_rejects_every_other_provider_operation_before_transport(method, resource, payload):
    api = control.Cloudflare(ACCOUNT, TOKEN)
    with pytest.raises(control.ControlError, match='FORBIDDEN_PROVIDER_OPERATION'):
        api.call(method, resource, payload)


@pytest.mark.parametrize('account', [None, '', '../other-account', 'x' * 32,
                                     ACCOUNT + '/workers'])
def test_cf_rejects_invalid_account_before_transport(account):
    with pytest.raises(control.ControlError, match='INVALID_ACCOUNT_ID'):
        control.Cloudflare(account, TOKEN)


@pytest.mark.parametrize('token', [None, '', 1])
def test_cf_requires_token_without_echoing_it(token):
    with pytest.raises(control.ControlError, match='MISSING_STOCK_CF_TOKEN'):
        control.Cloudflare(ACCOUNT, token)


@pytest.mark.parametrize('method,payload,uncertain', [('GET', None, False), ('PUT', [], True)])
@pytest.mark.parametrize('raw', [b'not-json', b'[]', b'{"success":true}',
    b'{"success":1,"result":{}}', b' ' * 1048577])
def test_cf_bad_responses_are_safe_and_put_is_uncertain(monkeypatch, method, payload, uncertain, raw):
    install_transport(monkeypatch, raw=raw)
    api = control.Cloudflare(ACCOUNT, TOKEN)
    with pytest.raises(control.APIError) as raised:
        api.call(method, 'schedules', payload)
    assert raised.value.uncertain is uncertain
    assert SECRET_DETAIL not in str(raised.value)
    assert ACCOUNT not in str(raised.value) and TOKEN not in str(raised.value)


@pytest.mark.parametrize('method,payload,uncertain', [('GET', None, False), ('PUT', [], True)])
def test_cf_transport_errors_never_leak_exception_details(monkeypatch, method, payload, uncertain):
    install_transport(monkeypatch, error=RuntimeError(TOKEN + ACCOUNT + SECRET_DETAIL))
    api = control.Cloudflare(ACCOUNT, TOKEN)
    with pytest.raises(control.APIError, match='CF_TRANSPORT_UNKNOWN') as raised:
        api.call(method, 'schedules', payload)
    assert raised.value.uncertain is uncertain
    assert str(raised.value) == 'CF_TRANSPORT_UNKNOWN'
    assert api.evidence == []


@pytest.mark.parametrize('status,uncertain', [(400, False), (401, False), (403, False),
                                           (500, True), (502, True), (503, True), (504, True)])
def test_cf_http_error_classifies_put_uncertainty_without_raw_body(monkeypatch, status, uncertain):
    error = urllib.error.HTTPError('https://example.invalid/' + ACCOUNT, status,
                                  SECRET_DETAIL + TOKEN, {}, io.BytesIO(TOKEN.encode()))
    requests, _ = install_transport(monkeypatch, error=error)
    api = control.Cloudflare(ACCOUNT, TOKEN)
    with pytest.raises(control.APIError, match=f'CF_HTTP_{status}') as raised:
        api.call('PUT', 'schedules', [])
    assert raised.value.uncertain is uncertain
    assert str(raised.value) == f'CF_HTTP_{status}'
    assert len(requests) == 1


def test_cf_does_not_echo_unsafe_ray_header(monkeypatch):
    install_transport(monkeypatch, headers={'CF-Ray': TOKEN + '\n' + ACCOUNT})
    api = control.Cloudflare(ACCOUNT, TOKEN)
    api.call('GET', 'schedules')
    assert api.evidence[0]['cf_ray'] is None


def test_cf_transport_disables_redirect_following(monkeypatch):
    requests, handlers = install_transport(monkeypatch)
    api = control.Cloudflare(ACCOUNT, TOKEN)
    api.call('PUT', 'schedules', [])
    redirect_handlers = [handler for handler in handlers
                         if isinstance(handler, control.urllib.request.HTTPRedirectHandler)]
    assert redirect_handlers, 'provider opener must explicitly prevent token-bearing redirects'
    request = requests[0]
    for handler in redirect_handlers:
        for code in (301, 302, 303, 307, 308):
            assert handler.redirect_request(request, None, code, 'redirect', {},
                'https://attacker.invalid/') is None


def test_cli_failure_receipt_does_not_leak_generic_exception(monkeypatch, capsys):
    monkeypatch.setenv('CLOUDFLARE_ACCOUNT_ID', ACCOUNT)
    monkeypatch.setenv('CLOUDFLARE_API_TOKEN', TOKEN)
    monkeypatch.setattr(sys, 'argv', ['cron_control.py', 'disable',
        '--expected-source', SOURCE, '--expected-version', VERSION])
    def fail(*args, **kwargs):
        raise RuntimeError(TOKEN + ACCOUNT + SECRET_DETAIL)
    monkeypatch.setattr(control, 'control_operation', fail)
    with pytest.raises(SystemExit) as raised:
        control.main()
    assert raised.value.code == 1
    output = capsys.readouterr()
    assert output.err == ''
    assert json.loads(output.out) == {
        'status': 'FAILED_OR_UNVERIFIED', 'error': 'CONTROL_FAILED',
        'automatic_retry': False, 'keep_authority_paused': True,
    }


def test_import_has_no_io_side_effects(monkeypatch, capsys):
    script = Path(control.__file__)
    spec = importlib.util.spec_from_file_location('isolated_stock_cron_control', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = capsys.readouterr()
    assert output.out == output.err == ''


@pytest.mark.parametrize('method,payload', [('GET', None), ('PUT', [])])
def test_cf_explicit_api_rejection_is_safe_and_definite(monkeypatch, method, payload):
    raw = json.dumps({'success': False, 'result': None,
                      'errors': [{'message': TOKEN + ACCOUNT + SECRET_DETAIL}]}).encode()
    requests, _ = install_transport(monkeypatch, raw=raw)
    api = control.Cloudflare(ACCOUNT, TOKEN)
    with pytest.raises(control.APIError, match='CF_API_REJECTED') as raised:
        api.call(method, 'schedules', payload)
    assert str(raised.value) == 'CF_API_REJECTED'
    assert raised.value.uncertain is False
    assert len(requests) == 1


@pytest.mark.parametrize('status', [199, 301, 302, 307, 308, 403, 500])
def test_cf_does_not_accept_non_2xx_response_with_success_body(monkeypatch, status):
    install_transport(monkeypatch, status=status)
    api = control.Cloudflare(ACCOUNT, TOKEN)
    with pytest.raises(control.APIError, match='CF_RESPONSE_UNVERIFIED') as raised:
        api.call('PUT', 'schedules', [])
    assert raised.value.uncertain is True


@pytest.mark.parametrize('mode,mutate', [('inspect', False), ('disable', True)])
def test_cli_success_returns_safe_receipt_and_requested_mode(monkeypatch, capsys, mode, mutate):
    monkeypatch.setenv('CLOUDFLARE_ACCOUNT_ID', ACCOUNT)
    monkeypatch.setenv('CLOUDFLARE_API_TOKEN', TOKEN)
    monkeypatch.setattr(sys, 'argv', ['cron_control.py', mode,
        '--expected-source', SOURCE, '--expected-version', VERSION])
    source_calls = []
    def authority(expected_source):
        source_calls.append(expected_source)
        return {'owner': 'paused', 'epoch': 2}
    monkeypatch.setattr(control, 'git_authority', authority)
    def success(api, authority, expected_version, *, mutate):
        assert isinstance(api, control.Cloudflare)
        assert api.token == TOKEN
        assert expected_version == VERSION
        assert mutate is (mode == 'disable')
        assert authority() == {'owner': 'paused', 'epoch': 2}
        api.evidence.append({'method': 'GET', 'resource': 'schedules', 'http_status': 200})
        return {'worker': control.WORKER, 'mode': mode,
                'stored_configuration_empty': True, 'propagation_verified': False}
    monkeypatch.setattr(control, 'control_operation', success)
    control.main()
    output = capsys.readouterr()
    assert output.err == ''
    receipt = json.loads(output.out)
    assert receipt['worker'] == 'stock-shadow-scheduler'
    assert receipt['mode'] == mode
    assert receipt['propagation_verified'] is False
    assert receipt['requests'] == [{'method': 'GET', 'resource': 'schedules', 'http_status': 200}]
    assert source_calls == [SOURCE]
    assert ACCOUNT not in output.out and TOKEN not in output.out


def test_cli_known_failure_preserves_only_fixed_error_code(monkeypatch, capsys):
    monkeypatch.setenv('CLOUDFLARE_ACCOUNT_ID', ACCOUNT)
    monkeypatch.setenv('CLOUDFLARE_API_TOKEN', TOKEN)
    monkeypatch.setattr(sys, 'argv', ['cron_control.py', 'disable',
        '--expected-source', SOURCE, '--expected-version', VERSION])
    def fail(*args, **kwargs):
        raise control.ControlError('STOCK_METADATA_DRIFT')
    monkeypatch.setattr(control, 'control_operation', fail)
    with pytest.raises(SystemExit) as raised:
        control.main()
    assert raised.value.code == 1
    output = capsys.readouterr()
    assert output.err == ''
    assert json.loads(output.out) == {
        'status': 'FAILED_OR_UNVERIFIED', 'error': 'STOCK_METADATA_DRIFT',
        'automatic_retry': False, 'keep_authority_paused': True,
    }
