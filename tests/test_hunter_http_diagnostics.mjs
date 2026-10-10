import fs from 'node:fs';
import test from 'node:test';
import assert from 'node:assert/strict';
import './helpers/cloudflare-webcrypto.mjs';
const source = fs.readFileSync(new URL('../workers/hunter-bybit-proxy.js', import.meta.url), 'utf8');
const {default: worker} = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
const blocked = async () => { throw new Error('REAL_NETWORK_DISABLED'); };
globalThis.fetch = blocked;
const send = (symbols = ['BTCUSDT']) => worker.fetch(new Request('https://worker.invalid/bybit/early-klines', {
  method: 'POST', body: JSON.stringify({symbols, end: Date.now()}),
}), {});
const safeHeaders = {'cf-ray': '0123456789abcdef-SIN', 'retry-after': '37',
  authorization: 'Bearer SECRET_DO_NOT_STORE', 'set-cookie': 'SECRET_DO_NOT_STORE',
  location: 'https://example.invalid/?key=SECRET_DO_NOT_STORE'};

test('official access-too-frequent wording is a marker, never a root-cause verdict', async t => {
  const logs = []; const original = console.log;
  console.log = line => logs.push(line);
  t.after(() => { console.log = original; globalThis.fetch = blocked; });
  for (const text of ['access too frequent', 'ACCESS TOO FREQUENT']) {
    globalThis.fetch = async () => new Response(text, {status: 403});
    const body = await (await send()).json();
    assert.deepEqual(body.result.failure_diagnostics.BTCUSDT['60'].body_markers, ['RATE_LIMIT_TEXT']);
    assert.equal(body.result.failures.BTCUSDT['60'], 'Error: BYBIT_HTTP_403');
  }
  assert.equal(logs.length, 4);
  assert(logs.every(line => JSON.parse(line.split(' ').slice(1).join(' ')).root_cause === 'UNKNOWN'));
});

test('all existing subrequests are bounded observations; 142 symbols remain 284 GETs plus ticker', async t => {
  const logs = []; const original = console.log; console.log = line => logs.push(line);
  t.after(() => { console.log = original; globalThis.fetch = blocked; });
  const end = Date.now(); let calls = 0; let active = 0; let peak = 0;
  const upstreamHeaders = {...safeHeaders, 'x-bapi-limit': '600', 'x-bapi-limit-status': '599',
    'x-bapi-limit-reset-timestamp': '1791660000000', 'x-forwarded-for': '192.0.2.1',
    traceid: '0123456789abcdef', 'x-amz-cf-id': '0123456789abcdef'};
  globalThis.fetch = async (address, options) => {
    calls++; active++; peak = Math.max(peak, active);
    await new Promise(resolve => setTimeout(resolve, 1)); active--;
    const url = new URL(address);
    assert.equal(url.origin, 'https://api.bybit.com');
    assert.equal(options.method, undefined); assert.equal(options.body, undefined);
    assert.deepEqual(options.headers, {'User-Agent': 'Hunter-Bybit-Proxy/2.0', Accept: 'application/json'});
    if (url.pathname === '/v5/market/tickers') {
      assert.equal(url.search, '?category=spot');
      return Response.json({retCode: 0, time: end, result: {category: 'spot', list: []}}, {headers: upstreamHeaders});
    }
    assert.equal(url.pathname, '/v5/market/kline');
    assert.deepEqual([...url.searchParams.keys()].sort(), ['category', 'end', 'interval', 'limit', 'symbol']);
    assert.equal(url.searchParams.get('category'), 'spot'); assert.equal(url.searchParams.get('end'), String(end));
    const symbol = url.searchParams.get('symbol'), interval = url.searchParams.get('interval');
    const limit = interval === '60' ? 5 : 25; assert.equal(url.searchParams.get('limit'), String(limit));
    if (symbol === 'ALT0USDT' && interval === '15') return new Response('access too frequent SECRET_DO_NOT_STORE', {status: 403, headers: upstreamHeaders});
    return Response.json({retCode: 0, time: end, result: {category: 'spot', symbol, list: Array(limit).fill([])}}, {headers: upstreamHeaders});
  };
  await worker.fetch(new Request('https://worker.invalid/bybit/tickers'), {});
  const symbols = Array.from({length: 142}, (_, i) => `ALT${i}USDT`);
  const batches = Array.from({length: 8}, (_, i) => symbols.slice(i*20, i*20+20));
  let next = 0; const results = [];
  // Mirror the unchanged Python two-batch ceiling without running Python/network.
  await Promise.all(Array.from({length: 2}, async () => {
    while (next < batches.length) {
      const batch = batches[next++];
      results.push(await (await worker.fetch(new Request('https://worker.invalid/bybit/early-klines', {
        method: 'POST', body: JSON.stringify({symbols: batch, end})}), {})).json());
    }
  }));
  assert.equal(calls, 285); assert.equal(peak, 8); assert.equal(logs.length, 285);
  const partial = results.find(r => r.result.failures.ALT0USDT);
  assert.deepEqual(partial.result.failures, {ALT0USDT: {'15': 'Error: BYBIT_HTTP_403'}});
  assert.deepEqual(Object.keys(partial.result.klines.ALT0USDT), ['60']);
  assert.equal(results.reduce((n, r) => n + Object.values(r.result.klines).reduce((v, x) => v + Object.keys(x).length, 0), 0), 283);
  const parsed = logs.map(line => {
    assert(line.length < 1400); assert(!/SECRET_DO_NOT_STORE|192\.0\.2\.1|authorization|set-cookie|https:/.test(line));
    const record = JSON.parse(line.slice('HUNTER_BYBIT_REQUEST_OBSERVATION '.length));
    assert.equal(record.layer, 'bybit_upstream'); assert.equal(record.root_cause, 'UNKNOWN');
    assert(Number.isInteger(record.duration_ms) && record.duration_ms >= 0);
    assert(Date.parse(record.completed_at_utc) >= Date.parse(record.started_at_utc));
    assert.match(record.worker_request_id, /^[0-9a-f-]{36}$/);
    assert.equal(record.headers['x-bapi-limit-status'], '599');
    return record;
  });
  assert.equal(parsed.filter(r => r.http_status === 403).length, 1);
  assert.equal(parsed.filter(r => r.http_status === 200 && r.ret_code === 0).length, 284);
  assert.equal(new Set(parsed.map(r => r.worker_request_id)).size, 9);
  for (const result of results) {
    assert.equal(result.request_observations.length, Object.keys(result.result.klines).length * 2);
    for (const record of result.request_observations) assert(parsed.some(row => JSON.stringify(row) === JSON.stringify(record)));
    assert(!('request_observations' in result.result));
    assert(JSON.stringify(result.request_observations).length < 56000);
  }
});

test('business errors, transport, invalid headers and logger failures preserve responses', async t => {
  const logs = []; const original = console.log; console.log = line => logs.push(line);
  t.after(() => { console.log = original; globalThis.fetch = blocked; });
  globalThis.fetch = async () => Response.json({retCode: 10006, retMsg: 'SECRET_DO_NOT_STORE', result: {category: 'spot'}},
    {headers: {'x-bapi-limit': '192.0.2.1', 'x-bapi-limit-status': 'SECRET_DO_NOT_STORE', 'x-bapi-limit-reset-timestamp': '9'.repeat(17)}});
  let body = await (await send()).json();
  assert.equal(body.result.failures.BTCUSDT['60'], 'Error: BYBIT_RET_10006');
  assert(!('request_observations' in body.result));
  assert(body.request_observations.every(row => row.ret_code === 10006));
  assert(logs.every(line => {const r = JSON.parse(line.slice(line.indexOf(' ')+1)); return r.ret_code === 10006 && Object.keys(r.headers).length === 0;}));
  logs.length = 0;
  globalThis.fetch = async () => { throw new Error('SECRET_DO_NOT_STORE'); };
  body = await (await send()).json();
  assert.equal(body.result.failures.BTCUSDT['60'], 'Error: WORKER_KLINE_FAILURE');
  assert(logs.every(line => JSON.parse(line.slice(line.indexOf(' ')+1)).http_status === 0));
  console.log = () => { throw new Error('LOGGER_FAILED'); };
  globalThis.fetch = async address => {const u = new URL(address); return Response.json({retCode: 0, time: Date.now(),
    result: {category: 'spot', symbol: 'BTCUSDT', list: Array(Number(u.searchParams.get('limit'))).fill([])}});};
  body = await (await send()).json();
  assert.deepEqual(Object.keys(body.result.klines.BTCUSDT).sort(), ['15', '60']);
  assert.deepEqual(body.result.failures, {});
});

for (const status of [403, 429, 502]) {
  test(`HTTP ${status}: bounded evidence, exact failed scope, no retries`, async t => {
    t.after(() => { globalThis.fetch = blocked; });
    let calls = 0;
    globalThis.fetch = async () => { calls++; return new Response(
      '<html>Access Denied. SECRET_DO_NOT_STORE</html>', {status, headers: safeHeaders}); };
    const symbols = status === 403 ? Array.from({length: 20}, (_, i) => `ALT${i}USDT`) : ['BTCUSDT'];
    const response = await send(symbols); const body = await response.json();
    assert.equal(calls, symbols.length * 2);
    assert.deepEqual(body.result.klines, {});
    assert.equal(Object.keys(body.result.failures).length, symbols.length);
    const ids = new Set();
    for (const symbol of symbols) for (const interval of ['60', '15']) {
      assert.equal(body.result.failures[symbol][interval], `Error: BYBIT_HTTP_${status}`);
      const d = body.result.failure_diagnostics[symbol][interval];
      assert.equal(d.layer, 'bybit_upstream'); assert.equal(d.http_status, status);
      assert.equal(d.body_kind, 'html'); assert.deepEqual(d.body_markers, ['ACCESS_DENIED_TEXT']);
      assert.equal(d.headers['cf-ray'], safeHeaders['cf-ray']);
      assert.equal(d.headers['retry-after'], '37');
      assert.equal(d.headers['x-hunter-worker-build'], 'hunter-bybit-httpdiag-v1');
      ids.add(d.headers['x-hunter-request-id']);
      assert(!JSON.stringify(d).includes('SECRET_DO_NOT_STORE'));
      assert(!('authorization' in d.headers)); assert(!('location' in d.headers));
    }
    assert.deepEqual([...ids], [response.headers.get('x-hunter-request-id')]);
  });
}

test('oversized sensitive body is cancelled after a bounded prefix', async t => {
  t.after(() => { globalThis.fetch = blocked; });
  let cancelled = 0;
  globalThis.fetch = async () => new Response(new ReadableStream({
    start(controller) { controller.enqueue(new TextEncoder().encode('SECRET_DO_NOT_STORE'.repeat(10000))); },
    cancel() { cancelled++; },
  }), {status: 403});
  const body = await (await send()).json();
  const d = body.result.failure_diagnostics.BTCUSDT['60'];
  assert.equal(d.body_bytes_sampled, 4096); assert.equal(d.body_truncated, true);
  assert.equal(cancelled, 2); assert(!JSON.stringify(body).includes('SECRET_DO_NOT_STORE'));
  assert(JSON.stringify(d).length < 1024);
});

test('JSON errors retain only numeric retCode and fixed text markers', async t => {
  t.after(() => { globalThis.fetch = blocked; });
  globalThis.fetch = async () => Response.json({retCode: 10006, retMsg: 'rate limit SECRET_DO_NOT_STORE',
    api_key: 'SECRET_DO_NOT_STORE'}, {status: 429});
  const body = await (await send()).json();
  const d = body.result.failure_diagnostics.BTCUSDT['60'];
  assert.equal(d.body_kind, 'json'); assert.equal(d.ret_code, 10006);
  assert.deepEqual(d.body_markers, ['RATE_LIMIT_TEXT']);
  assert(!JSON.stringify(body).includes('SECRET_DO_NOT_STORE'));
});

test('timeout and transport errors expose no original exception text', async t => {
  t.after(() => { globalThis.fetch = blocked; });
  for (const name of ['AbortError', 'TypeError']) {
    globalThis.fetch = async () => { const e = new Error('SECRET_DO_NOT_STORE'); e.name = name; throw e; };
    const body = await (await send()).json();
    const d = body.result.failure_diagnostics.BTCUSDT['60'];
    assert.equal(d.http_status, 0); assert.equal(d.body_kind, 'unavailable');
    assert.equal(d.failure_kind, name === 'AbortError' ? 'timeout' : 'transport');
    assert(!JSON.stringify(body).includes('SECRET_DO_NOT_STORE'));
  }
});

test('non-JSON 200 is failure, never a usable candle', async t => {
  t.after(() => { globalThis.fetch = blocked; });
  globalThis.fetch = async () => new Response('SECRET_DO_NOT_STORE not json');
  const body = await (await send()).json();
  assert.deepEqual(body.result.klines, {});
  assert.equal(body.result.failure_diagnostics.BTCUSDT['60'].failure_kind, 'invalid_json');
  assert(!JSON.stringify(body).includes('SECRET_DO_NOT_STORE'));
});

test('partial batch keeps success only for unaffected intervals', async t => {
  t.after(() => { globalThis.fetch = blocked; });
  globalThis.fetch = async address => {
    const url = new URL(address); const interval = url.searchParams.get('interval');
    if (interval === '15') return new Response('denied', {status: 403});
    return Response.json({retCode: 0, time: Date.now(), result: {category: 'spot', symbol: 'BTCUSDT', list: Array(5).fill([])}});
  };
  const body = await (await send()).json();
  assert.deepEqual(Object.keys(body.result.klines.BTCUSDT), ['60']);
  assert.deepEqual(Object.keys(body.result.failures.BTCUSDT), ['15']);
  assert.deepEqual(Object.keys(body.result.failure_diagnostics.BTCUSDT), ['15']);
});

test('outer 502 includes build/request binding and sanitized upstream evidence', async t => {
  t.after(() => { globalThis.fetch = blocked; });
  globalThis.fetch = async () => new Response('Access Denied SECRET_DO_NOT_STORE', {status: 403});
  const response = await worker.fetch(new Request('https://worker.invalid/bybit/tickers'), {});
  const body = await response.json();
  assert.equal(response.status, 502); assert.equal(body.diagnostics.http_status, 403);
  assert.equal(body.diagnostics.headers['x-hunter-request-id'], response.headers.get('x-hunter-request-id'));
  assert(!JSON.stringify(body).includes('SECRET_DO_NOT_STORE'));
});

test('malformed upstream retCode cannot leak arbitrary data', async t => {
  t.after(() => { globalThis.fetch = blocked; });
  globalThis.fetch = async () => Response.json({retCode: 'SECRET_DO_NOT_STORE', result: {category: 'spot'}});
  const body = await (await send()).json();
  assert.equal(body.result.failures.BTCUSDT['60'], 'Error: BYBIT_RET_INVALID');
  assert(!JSON.stringify(body).includes('SECRET_DO_NOT_STORE'));
});

test('body read abort and transport failure are not mislabeled invalid JSON', async t => {
  t.after(() => { globalThis.fetch = blocked; });
  for (const name of ['AbortError', 'TypeError']) {
    globalThis.fetch = async () => new Response(new ReadableStream({
      start(controller) { const error = new Error('SECRET_DO_NOT_STORE'); error.name = name; controller.error(error); },
    }));
    const body = await (await send()).json();
    assert.deepEqual(body.result.klines, {});
    const diagnostic = body.result.failure_diagnostics.BTCUSDT['60'];
    assert.equal(diagnostic.http_status, 200);
    assert.equal(diagnostic.failure_kind, name === 'AbortError' ? 'timeout' : 'transport');
    assert.equal(body.result.failures.BTCUSDT['60'], name === 'AbortError'
      ? 'AbortError: upstream aborted' : 'Error: WORKER_KLINE_FAILURE');
    assert(!JSON.stringify(body).includes('SECRET_DO_NOT_STORE'));
  }
});

test('sampling boundaries count bytes and reject arbitrary header values', async t => {
  t.after(() => { globalThis.fetch = blocked; });
  for (const length of [0, 4095, 4096, 4097]) {
    globalThis.fetch = async () => new Response(new Uint8Array(length).fill(200), {status: 403,
      headers: {'cf-ray': 'SECRET_DO_NOT_STORE', 'retry-after': 'date-or-secret', traceid: '0123456789abcdef'}});
    const body = await (await send()).json();
    const diagnostic = body.result.failure_diagnostics.BTCUSDT['60'];
    assert.equal(diagnostic.body_bytes_sampled, Math.min(length, 4096));
    assert.equal(diagnostic.body_truncated, length > 4096);
    assert(!('cf-ray' in diagnostic.headers)); assert(!('retry-after' in diagnostic.headers));
    assert.equal(diagnostic.headers.traceid, '0123456789abcdef');
  }
});

test('request correlation is generated independently of caller secrets and cf metadata', async t => {
  t.after(() => { globalThis.fetch = blocked; });
  globalThis.fetch = async () => new Response('denied', {status: 403});
  const ids = [];
  for (let index = 0; index < 2; index++) {
    const request = new Request('https://worker.invalid/bybit/tickers', {
      headers: {'x-hunter-request-id': 'SECRET_DO_NOT_STORE', authorization: 'SECRET_DO_NOT_STORE'}});
    Object.defineProperty(request, 'cf', {value: {country: 'SECRET_DO_NOT_STORE', colo: 'SECRET_DO_NOT_STORE'}});
    const response = await worker.fetch(request, {}); const body = await response.json();
    const id = response.headers.get('x-hunter-request-id'); ids.push(id);
    assert.match(id, /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/);
    assert.equal(body.diagnostics.headers['x-hunter-request-id'], id);
    assert(!JSON.stringify(body).includes('SECRET_DO_NOT_STORE'));
  }
  assert.notEqual(ids[0], ids[1]);
});

for (const route of ['spot', 'tickers']) test(`public ${route}: 403/429/502 are bounded upstream failures, one GET`, async t => {
  t.after(() => { globalThis.fetch = blocked; });
  for (const status of [403, 429, 502]) {
    let calls = 0;
    globalThis.fetch = async (address, options) => {
      calls++;
      const url = new URL(address);
      assert.equal(url.pathname, route === 'spot' ? '/v5/market/instruments-info' : '/v5/market/tickers');
      assert.equal(url.search, '?category=spot');
      assert.equal(options.body, undefined); assert.equal(options.method, undefined);
      return new Response('Access Denied SECRET_DO_NOT_STORE', {status, headers: safeHeaders});
    };
    const response = await worker.fetch(new Request('https://worker.invalid/bybit/' + route), {});
    const body = await response.json();
    assert.equal(calls, 1); assert.equal(response.status, 502);
    assert.equal(body.detail, `Error: BYBIT_HTTP_${status}`);
    assert.equal(body.diagnostics.http_status, status);
    assert.equal(body.diagnostics.headers['x-hunter-request-id'], response.headers.get('x-hunter-request-id'));
    assert(!JSON.stringify(body).includes('SECRET_DO_NOT_STORE'));
  }
});

test('spot truncated sensitive error body is cancelled and never returned', async t => {
  t.after(() => { globalThis.fetch = blocked; });
  let cancelled = 0;
  globalThis.fetch = async () => new Response(new ReadableStream({
    start(controller) { controller.enqueue(new TextEncoder().encode('SECRET_DO_NOT_STORE'.repeat(10000))); },
    cancel() { cancelled++; },
  }), {status: 403});
  const body = await (await worker.fetch(new Request('https://worker.invalid/bybit/spot'), {})).json();
  assert.equal(cancelled, 1); assert.equal(body.diagnostics.body_bytes_sampled, 4096);
  assert.equal(body.diagnostics.body_truncated, true); assert(!JSON.stringify(body).includes('SECRET_DO_NOT_STORE'));
});

test('spot non-JSON and timeout never return success or exception secrets', async t => {
  t.after(() => { globalThis.fetch = blocked; });
  for (const mode of ['invalid_json', 'timeout']) {
    globalThis.fetch = async () => {
      if (mode === 'invalid_json') return new Response('SECRET_DO_NOT_STORE not json');
      const error = new Error('SECRET_DO_NOT_STORE'); error.name = 'AbortError'; throw error;
    };
    const response = await worker.fetch(new Request('https://worker.invalid/bybit/spot'), {});
    const body = await response.json();
    assert.equal(response.status, 502); assert.equal(body.ok, false);
    assert.equal(body.diagnostics.failure_kind, mode); assert(!JSON.stringify(body).includes('SECRET_DO_NOT_STORE'));
  }
});

test('spot success preserves the upstream JSON contract and makes one request', async t => {
  t.after(() => { globalThis.fetch = blocked; });
  const expected = {retCode: 0, time: Date.now(), result: {category: 'spot', list: [{symbol: 'BTCUSDT'}]}};
  let calls = 0;
  globalThis.fetch = async () => { calls++; return Response.json(expected); };
  const response = await worker.fetch(new Request('https://worker.invalid/bybit/spot'), {});
  const actual = await response.json();
  assert.equal(actual.request_observations.length, 1);
  delete actual.request_observations;
  assert.equal(response.status, 200); assert.deepEqual(actual, expected); assert.equal(calls, 1);
});

test('HTTP 200 body abort keeps AbortError identity on both public list routes', async t => {
  t.after(() => { globalThis.fetch = blocked; });
  for (const route of ['spot', 'tickers']) {
    globalThis.fetch = async () => new Response(new ReadableStream({start(controller) {
      const error = new Error('SECRET_DO_NOT_STORE'); error.name = 'AbortError'; controller.error(error);
    }}));
    const response = await worker.fetch(new Request('https://worker.invalid/bybit/' + route), {});
    const body = await response.json();
    assert.equal(response.status, 502);
    assert.equal(body.detail, 'AbortError: upstream aborted');
    assert.equal(body.diagnostics.http_status, 200);
    assert.equal(body.diagnostics.failure_kind, 'timeout');
    assert(!JSON.stringify(body).includes('SECRET_DO_NOT_STORE'));
  }
});
