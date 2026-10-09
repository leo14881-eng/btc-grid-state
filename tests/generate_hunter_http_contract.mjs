// Deterministic, offline responses from the real current Worker for Python compatibility tests.
import fs from 'node:fs';
import assert from 'node:assert/strict';
const now = Date.parse('2026-10-05T18:07:00Z');
const originalNow = Date.now;
Date.now = () => now;
globalThis.fetch = async () => { throw new Error('NETWORK_FORBIDDEN'); };
Object.defineProperty(globalThis, 'crypto', {configurable: true,
  value: {randomUUID: () => '00000000-0000-4000-8000-000000000001'}});
const source = fs.readFileSync(new URL('../workers/hunter-bybit-proxy.js', import.meta.url), 'utf8');
const {default: worker} = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
const output = {fixture_only: true, end: now, source: 'real Worker with mocked public upstream'};
for (const mode of ['success', 'partial', 'all_failed']) {
  let count = 0;
  globalThis.fetch = async address => {
    const url = new URL(address); count++;
    assert.equal(url.hostname, 'api.bybit.com');
    const symbol = url.searchParams.get('symbol'); const interval = url.searchParams.get('interval');
    if (mode === 'all_failed' || mode === 'partial' && symbol === 'FLUIDUSDT' && interval === '15') {
      return new Response('Access Denied SECRET_DO_NOT_STORE', {status: 403,
        headers: {'cf-ray': '0123456789abcdef-SIN', authorization: 'SECRET_DO_NOT_STORE'}});
    }
    const limit = Number(url.searchParams.get('limit')); const step = Number(interval) * 60000;
    return Response.json({retCode: 0, time: now, result: {category: 'spot', symbol,
      list: Array.from({length: limit}, (_, i) => [String(Math.floor(now / step) * step - (limit - 1 - i) * step),
        '10', '11', '9', '10', '1', '10'])}});
  };
  output[mode] = await (await worker.fetch(new Request('https://worker.invalid/bybit/early-klines', {
    method: 'POST', body: JSON.stringify({symbols: ['BTCUSDT', 'FLUIDUSDT'], end: now}),
  }), {})).json();
  assert.equal(count, 4);
  assert(!JSON.stringify(output[mode]).includes('SECRET_DO_NOT_STORE'));
}
output.spot = {};
for (const mode of ['success', '403', '429', '502', 'non_json']) {
  let count = 0;
  globalThis.fetch = async (address, options) => {
    count++;
    assert.equal(String(address), 'https://api.bybit.com/v5/market/instruments-info?category=spot');
    assert.equal(options.body, undefined);
    if (mode === 'non_json') return new Response('SECRET_DO_NOT_STORE invalid json');
    if (mode !== 'success') return new Response('Access Denied SECRET_DO_NOT_STORE', {status: Number(mode)});
    return Response.json({retCode: 0, time: now, result: {category: 'spot', list: ['BTC', 'ETH'].map(base =>
      ({symbol: base + 'USDT', baseCoin: base, quoteCoin: 'USDT', status: 'Trading'}))}});
  };
  const response = await worker.fetch(new Request('https://worker.invalid/bybit/spot'), {});
  output.spot[mode] = {status: response.status, headers: Object.fromEntries(response.headers), body: await response.json()};
  assert.equal(count, 1);
  assert(!JSON.stringify(output.spot[mode]).includes('SECRET_DO_NOT_STORE'));
}
Date.now = originalNow;
const file = new URL('./fixtures/hunter_http_worker_responses.json', import.meta.url);
const serialized = JSON.stringify(output, null, 2) + '\n';
if (process.argv.includes('--check')) assert.equal(fs.readFileSync(file, 'utf8'), serialized);
else fs.writeFileSync(file, serialized);
console.log('OFFLINE_WORKER_CONTRACT_OK: 3 candle batches, 5 spot cases; exact call counts; no network');
