// Real Worker code, synthetic upstream only. JSON goes to stdout for offline CI.
import fs from 'node:fs';
import assert from 'node:assert/strict';
const now = Date.parse('2026-10-05T18:07:00Z');
Date.now = () => now;
Object.defineProperty(globalThis, 'performance', {value: {now: () => 0}});
let id = 0;
Object.defineProperty(globalThis, 'crypto', {value: {randomUUID: () => `00000000-0000-4000-8000-${String(++id).padStart(12, '0')}`}});
console.log = () => {}; // Worker console is not the server journal under test.
const source = fs.readFileSync(new URL('../workers/hunter-bybit-proxy.js', import.meta.url), 'utf8');
const {default: worker} = await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
const bases = Array.from({length: 141}, (_, i) => `ASSET${String(i).padStart(3, '0')}`);
const symbols = [...bases.map(base => base+'USDT'), 'BTCUSDT'].sort();
// Maximal accepted header lengths conservatively exercise diagnostic size.
const headers = {'cf-ray': 'a'.repeat(32)+'-SIN', traceid: 'b'.repeat(64),
  'x-amz-cf-id': 'c'.repeat(128), 'retry-after': '9'.repeat(6),
  'x-bapi-limit': '9'.repeat(10), 'x-bapi-limit-status': '9'.repeat(10),
  'x-bapi-limit-reset-timestamp': '9'.repeat(16)};
const output = {fixture_only: true, end: now, bases};
for (const mode of ['success', 'partial', 'all_failed']) {
  let calls = 0;
  globalThis.fetch = async address => {
    calls++;
    const url = new URL(address);
    if (url.pathname === '/v5/market/tickers') return Response.json({retCode: 0, time: now,
      result: {category: 'spot', list: [...bases, 'BTC'].map(base => ({symbol: base+'USDT',
        lastPrice: '10', turnover24h: '100000', price24hPcnt: '.1'}))}}, {headers});
    const symbol = url.searchParams.get('symbol'), interval = url.searchParams.get('interval');
    if (mode === 'all_failed' || mode === 'partial' && symbols.indexOf(symbol) >= 80 && symbols.indexOf(symbol) < 120)
      return new Response('We block access from your country.', {status: 403, headers});
    const limit = Number(url.searchParams.get('limit')), step = Number(interval)*60000;
    return Response.json({retCode: 0, time: now, result: {category: 'spot', symbol,
      list: Array.from({length: limit}, (_, i) => [String(Math.floor(now/step)*step-(limit-1-i)*step), '10', '11', '9', '10', '1', '10'])}}, {headers});
  };
  const rows = [];
  const ticker = await worker.fetch(new Request('https://worker.invalid/bybit/tickers'), {});
  rows.push({path: '/bybit/tickers', sent: null, headers: Object.fromEntries(ticker.headers), body: await ticker.json()});
  for (let offset = 0; offset < symbols.length; offset += 20) {
    const sent = {symbols: symbols.slice(offset, offset+20), end: now};
    const response = await worker.fetch(new Request('https://worker.invalid/bybit/early-klines',
      {method: 'POST', body: JSON.stringify(sent)}), {});
    rows.push({path: '/bybit/early-klines', sent, headers: Object.fromEntries(response.headers), body: await response.json()});
  }
  assert.equal(calls, 285);
  output[mode] = {upstream_calls: calls, rows};
}
process.stdout.write(JSON.stringify(output));
