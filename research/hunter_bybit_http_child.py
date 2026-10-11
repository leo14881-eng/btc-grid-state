"""Disposable public HTTP worker. Its supervisor owns deadlines and all state.

No credential environment, SQLite, retry, strategy or independent budget here.
Linux parent-death signalling prevents an orphan HTTP socket after a crash.
"""
import base64
import ctypes
import http.client
import json
import os
import signal
import sys
import urllib.error
import urllib.parse
import urllib.request

HOST = 'api.bybit.com'
PATHS = {'/v5/market/instruments-info', '/v5/market/tickers', '/v5/market/kline'}
MAX_BODY = 8 * 1024 * 1024


def allowed(url):
    value = urllib.parse.urlsplit(url)
    if (value.scheme != 'https' or value.netloc != HOST or value.path not in PATHS
            or value.fragment or not value.query):
        raise ValueError('PUBLIC_ENDPOINT_REQUIRED')
    pairs = urllib.parse.parse_qsl(value.query, strict_parsing=True)
    params = dict(pairs)
    if len(params) != len(pairs) or params.get('category') != 'spot':
        raise ValueError('PUBLIC_SPOT_REQUIRED')
    permitted = {'category'} if value.path != '/v5/market/kline' else {'category','symbol','interval','limit','end'}
    if set(params) != permitted:
        raise ValueError('PUBLIC_REQUEST_SHAPE_INVALID')
    if value.path.endswith('/kline'):
        import re
        if (not re.fullmatch(r'[A-Z0-9]{1,30}USDT', params['symbol'])
                or params['interval'] not in ('15','60')
                or params['limit'] != {'15':'25','60':'5'}[params['interval']]
                or not params['end'].isdigit()):
            raise ValueError('PUBLIC_KLINE_SCOPE_INVALID')
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('REDIRECT_FORBIDDEN')


def emit(value):
    sys.stdout.write(json.dumps(value, separators=(',',':'))+'\n')
    sys.stdout.flush()


def exchange(url, send=emit):
    allowed(url)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(url, headers={'Accept':'application/json','User-Agent':'Hunter-Public-Spot/1'})
    try:
        response = opener.open(request, timeout=10)
    except urllib.error.HTTPError as error:
        response = error
    status = response.code
    send(dict(kind='headers',status=status))  # Known denial precedes body parsing.
    try:
        with response:
            payload = response.read(MAX_BODY+1)
            if len(payload) > MAX_BODY:
                raise ValueError('RESPONSE_TOO_LARGE')
            raw = response.headers.get('Retry-After','1')
            retry = min(600, int(raw)) if raw.isdigit() and len(raw) <= 6 else 1
    except (ValueError, TimeoutError, OSError, http.client.HTTPException):
        if status != 403:
            raise
        payload, retry = b'', 1
    send(dict(kind='response',status=status,payload=base64.b64encode(payload).decode('ascii'),retry=retry))


def main():
    url, parent = sys.argv[1], int(sys.argv[2])
    allowed(url)
    if sys.platform != 'linux':
        raise RuntimeError('LINUX_SUPERVISOR_REQUIRED')
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent:
        raise RuntimeError('PARENT_DEATH_GUARD_UNAVAILABLE')
    emit(dict(kind='ready',pid=os.getpid(),parent=parent))
    if sys.stdin.buffer.readline(4) != b'GO\n' or os.getppid() != parent:
        return 2  # No DNS or socket is created before the explicit GO.
    try:
        exchange(url)
    except BaseException:
        emit(dict(kind='error',code='PUBLIC_HTTP_FAILED'))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
