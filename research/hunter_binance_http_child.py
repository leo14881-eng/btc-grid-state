"""Disposable public HTTP worker. Its supervisor owns deadlines and all state.

No credential environment, SQLite, retry, strategy or independent budget here.
Linux parent-death signalling prevents an orphan HTTP socket after a crash.
"""
import base64
import ctypes
import email.utils
import http.client
import json
import os
import signal
import sys
import urllib.error
import urllib.parse
import urllib.request

HOST = 'data-api.binance.vision'
MAX_BODY = 1024 * 1024


def allowed(url):
    value = urllib.parse.urlsplit(url)
    if (value.scheme != 'https' or value.netloc != HOST or value.path not in ('/api/v3/klines','/api/v3/exchangeInfo')
            or value.fragment or not value.query):
        raise ValueError('BINANCE_PUBLIC_ENDPOINT_REQUIRED')
    pairs = urllib.parse.parse_qsl(value.query, strict_parsing=True)
    params = dict(pairs)
    import re
    if value.path=='/api/v3/exchangeInfo':
        if (len(params)!=len(pairs) or set(params)!={'symbols','showPermissionSets'}
                or params['showPermissionSets']!='false'):
            raise ValueError('BINANCE_PUBLIC_DIRECTORY_SCOPE_INVALID')
        symbols=json.loads(params['symbols'])
        if (not isinstance(symbols,list) or not 1<=len(symbols)<=20
                or len(symbols)!=len(set(symbols)) or any(not isinstance(s,str)
                or not s.endswith('USDT') or not 1<=len(s[:-4])<=30 or not s.isalnum()
                or s.upper()!=s or s.lower().upper()!=s for s in symbols)):
            raise ValueError('BINANCE_PUBLIC_DIRECTORY_SCOPE_INVALID')
        return value
    limits = {'5m':16, '15m':25, '1h':5, '1d':35}
    if (len(params) != len(pairs) or set(params) != {'symbol','interval','limit','endTime'}
            or not params['symbol'].endswith('USDT') or not 1 <= len(params['symbol'][:-4]) <= 30
            or not params['symbol'].isalnum() or params['symbol'].upper()!=params['symbol']
            or params['symbol'].lower().upper()!=params['symbol']
            or params['interval'] not in limits
            or params['limit'] != str(limits[params['interval']])
            or not params['endTime'].isdigit()):
        raise ValueError('BINANCE_PUBLIC_KLINE_SCOPE_INVALID')
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
    header=dict(kind='headers',status=status)
    used=response.headers.get('X-MBX-USED-WEIGHT-1M')
    date=response.headers.get('Date')
    # Only a complete, bounded, server-window attribution can calibrate the
    # governor. Missing or malformed optional headers never invent a window.
    if used and used.isdigit() and len(used)<=9 and date:
        try:
            stamp=email.utils.parsedate_to_datetime(date)
            if stamp.tzinfo is not None:
                header.update(weight_used=int(used),weight_window_ms=int(stamp.timestamp())//60*60000)
        except (ValueError,TypeError,OverflowError):
            pass
    send(header)  # Known denial precedes body parsing.
    try:
        with response:
            payload = response.read(MAX_BODY+1)
            if len(payload) > MAX_BODY:
                raise ValueError('RESPONSE_TOO_LARGE')
            raw = response.headers.get('Retry-After','1')
            retry = min(600, int(raw)) if raw.isdigit() and len(raw) <= 6 else 1
    except (ValueError, TimeoutError, OSError, http.client.HTTPException):
        if status not in (403,418):
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
