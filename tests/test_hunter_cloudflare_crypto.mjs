import fs from 'node:fs';
import assert from 'node:assert/strict';
import {webcrypto} from 'node:crypto';

console.log('CRYPTO_RUNTIME', process.version, 'native_global=' + typeof globalThis.crypto);
if (process.version === 'v18.19.1') assert.equal(typeof globalThis.crypto, 'undefined');
globalThis.fetch = async () => { throw new Error('NETWORK_FORBIDDEN'); };
const source = fs.readFileSync(new URL('../workers/hunter-bybit-proxy.js', import.meta.url), 'utf8');
const {default: worker} = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
const health = () => worker.fetch(new Request('https://worker.invalid/health'), {});

// Reproduce the exact missing Cloudflare global before the test-only adapter.
delete globalThis.crypto;
await assert.rejects(health, error => {
  assert.equal(error.name, 'ReferenceError');
  assert.equal(error.message, 'crypto is not defined');
  console.log('BASELINE_ERROR', String(error));
  return true;
});
await import('./helpers/cloudflare-webcrypto.mjs');
assert.equal(globalThis.crypto, webcrypto);
const ids = new Set();
for (let i = 0; i < 2; i++) {
  const response = await health();
  assert.equal(response.status, 200);
  assert.equal((await response.json()).build, 'hunter-bybit-httpdiag-v1');
  const id = response.headers.get('x-hunter-request-id');
  assert.match(id, /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/);
  ids.add(id);
}
assert.equal(ids.size, 2);
// Preserve an existing runtime global or a deterministic fixture implementation.
const existing = {randomUUID: () => 'existing-fixture'};
globalThis.crypto = existing;
await import('./helpers/cloudflare-webcrypto.mjs?preserve-existing');
assert.equal(globalThis.crypto, existing);
console.log('CRYPTO_ADAPTER_REGRESSION_OK');
