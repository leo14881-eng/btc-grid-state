// Synthetic offline wire records from the actual Worker, never a live endpoint.
import fs from 'node:fs';
import assert from 'node:assert/strict';
const now = Date.parse('2026-10-05T18:07:00Z');
Date.now = () => now;
Object.defineProperty(globalThis, 'performance', {value: {now: () => 0}});
Object.defineProperty(globalThis, 'crypto', {value: {randomUUID: () => '00000000-0000-4000-8000-000000000001'}});
const source = fs.readFileSync(new URL('../workers/hunter-bybit-proxy.js', import.meta.url), 'utf8');
const {default: worker} = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
const output = {fixture_only: true, end: now};
const safe = {'cf-ray': '0123456789abcdef-SIN', 'x-bapi-limit': '600', 'x-bapi-limit-status': '599'};
for (const mode of ['success', 'partial', 'spot_failure']) {
  let calls = 0;
  globalThis.fetch = async address => {
    calls++;
    const url = new URL(address), symbol = url.searchParams.get('symbol'), interval = url.searchParams.get('interval');
    if (mode === 'spot_failure' || mode === 'partial' && symbol === 'FLUIDUSDT' && interval === '15')
      return new Response('access too frequent', {status: 403, headers: safe});
    const limit = Number(url.searchParams.get('limit')), step = Number(interval)*60000;
    return Response.json({retCode: 0, time: now, result: {category: 'spot', symbol,
      list: Array.from({length: limit}, (_, i) => [String(Math.floor(now/step)*step-(limit-1-i)*step), '10', '11', '9', '10', '1', '10'])}}, {headers: safe});
  };
  const response = await worker.fetch(new Request('https://worker.invalid/bybit/' + (mode === 'spot_failure' ? 'spot' : 'early-klines'),
    mode === 'spot_failure' ? {} : {method: 'POST', body: JSON.stringify({symbols: ['BTCUSDT', 'FLUIDUSDT'], end: now})}), {});
  const body = await response.json();
  body.request_observations.sort((a, b) => a.upstream_job_index-b.upstream_job_index);
  assert.equal(calls, mode === 'spot_failure' ? 1 : 4);
  assert.equal(body.request_observations.length, calls);
  output[mode] = {status: response.status, headers: Object.fromEntries(response.headers), body};
}
const file = new URL('./fixtures/hunter_observation_responses.json', import.meta.url);
const serialized = JSON.stringify(output, null, 2) + '\n';
if (process.argv.includes('--check')) assert.equal(fs.readFileSync(file, 'utf8'), serialized);
else fs.writeFileSync(file, serialized);
console.log('OFFLINE_OBSERVATION_CONTRACT_OK: 9 mocked upstream requests; no network');
