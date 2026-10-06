import test from 'node:test';
import assert from 'node:assert/strict';
import worker, { age, marketStatus, monitorAge, decide, tick, GitHub, MAIN, MONITOR, StockScheduler } from './worker.mjs';

const ms = Date.parse('2026-10-05T18:00:00Z');
const minutes = n => n * 60_000;
const stamp = n => new Date(ms - minutes(n)).toISOString();
const calendar = { date: '2026-10-05', open: '09:30', close: '16:00',
  source: 'ALPACA_EXCHANGE_CALENDAR', verified_at: '2026-10-05T12:00:00Z' };
const health = n => ({ status: 'OK', positions_before: 327, positions_updated: 327,
  missing_symbols: [], errors: [], updated_at: stamp(n) });
const snapshot = (n = 2, mainAge = 10) => ({ calendar, monitor: health(n), summary: { updated_at: stamp(mainAge) }, active: false });

class Storage {
  constructor() { this.data = new Map(); this.tail = Promise.resolve(); }
  async get(k) { return structuredClone(this.data.get(k)); }
  async put(k, v) { if (typeof k === 'object') for (const [key, value] of Object.entries(k)) this.data.set(key, structuredClone(value));
    else this.data.set(k, structuredClone(v)); }
  transaction(fn) { const next = this.tail.then(() => fn(this)); this.tail = next.catch(() => {}); return next; }
}
const github = (s = snapshot(15)) => ({ calls: [], snapshot: async () => s,
  active: async () => false, async dispatch(w) { this.calls.push(w); } });

test('regular session, NY weekend and early-close boundary', () => {
  assert.equal(marketStatus(ms, calendar), 'OPEN');
  assert.equal(marketStatus(Date.parse('2026-10-04T18:00:00Z'), calendar), 'CLOSED');
  assert.equal(marketStatus(ms, { ...calendar, close: '13:00' }), 'CLOSED');
  assert.equal(marketStatus(Date.parse('2026-10-05T13:30:00Z'), calendar), 'OPEN');
  assert.equal(marketStatus(Date.parse('2026-10-05T20:00:00Z'), calendar), 'CLOSED');
});
test('DST conversion uses NY zone, not a hardcoded UTC offset', () => {
  const winter = { ...calendar, date: '2026-12-01', verified_at: '2026-12-01T12:00:00Z' };
  assert.equal(marketStatus(Date.parse('2026-12-01T14:00:00Z'), winter), 'CLOSED');
  assert.equal(marketStatus(Date.parse('2026-12-01T14:30:00Z'), winter), 'OPEN');
});
for (const [name, c] of Object.entries({ missing: null, wrongDate: { ...calendar, date: '2026-10-02' },
  wrongSource: { ...calendar, source: 'GUESS' }, malformed: { ...calendar, close: '25:00' },
  reversed: { ...calendar, close: '08:00' }, old: { ...calendar, verified_at: stamp(3000) },
  future: { ...calendar, verified_at: stamp(-10) } })) {
  test(`unverified calendar ${name} is UNKNOWN`, () => assert.equal(marketStatus(ms, c), 'UNKNOWN'));
}
test('missing, failed, partial and future timestamps are not successful heartbeats', () => {
  assert.equal(monitorAge(ms, null), Infinity);
  for (const h of [{ ...health(1), status: 'ERROR' }, { ...health(1), errors: ['error'] },
    { ...health(1), missing_symbols: ['AAPL'] }, { ...health(1), positions_updated: 326 }, health(-10)])
    assert.equal(monitorAge(ms, h), Infinity);
  assert.equal(monitorAge(ms, health(5)), minutes(5));
  assert.equal(age(ms, 'bad'), Infinity);
});
test('stale monitor is recovered without changing strategy parameters', () => {
  assert.deepEqual(decide(ms, snapshot(15)).workflow, MONITOR);
  assert.equal(decide(ms, snapshot(15)).stale, true);
  assert.equal(decide(ms, snapshot(2)).action, 'FRESH');
});
test('hourly scan gets a turn, but cannot jump ahead of severely stale monitor', () => {
  assert.equal(decide(ms, snapshot(5, 70)).workflow, MAIN);
  assert.equal(decide(ms, snapshot(15, 70)).workflow, MONITOR);
});
test('persistent partial monitoring gets two recovery turns then an overdue scan', async () => {
  const partial = { ...snapshot(1, 90), monitor: { ...health(1), status: 'PARTIAL',
    positions_updated: 326, missing_symbols: ['QRVO'] } };
  const storage = new Storage(), gh = github(partial);
  for (let n = 0; n < 4; n++) {
    const result = await tick(storage, gh, ms + minutes(n * 5));
    assert.equal(result.stale, true);
    if (n === 2) assert.equal(result.reason, 'BOUNDED_MONITOR_RECOVERY');
  }
  assert.deepEqual(gh.calls, [MONITOR, MONITOR, MAIN, MONITOR]);
  assert.equal(monitorAge(ms, partial.monitor), Infinity);
});
test('active recheck and dispatch failures do not count as recovery turns', async () => {
  const storage = new Storage(), gh = github(snapshot(15, 90));
  gh.active = async () => true;
  await tick(storage, gh, ms);
  assert.equal((await storage.get('lease')).monitor_recovery_turns, undefined);
  gh.active = async () => false;
  gh.dispatch = async () => { throw new Error('GITHUB_HTTP_429'); };
  await tick(storage, gh, ms + minutes(5));
  assert.equal((await storage.get('lease')).monitor_recovery_turns, undefined);
  assert.equal(decide(ms + minutes(10), snapshot(15, 90), await storage.get('lease')).workflow, MONITOR);
});
test('active runs and persistent cooldown suppress duplicate recovery', () => {
  assert.equal(decide(ms, { ...snapshot(15), active: true }).action, 'WAIT_ACTIVE_RUN');
  assert.equal(decide(ms, snapshot(15), { at: ms - minutes(2) }).action, 'DISPATCH_COOLDOWN');
});
test('weekend skips monitor but retains hourly research', () => {
  const sunday = Date.parse('2026-10-04T18:00:00Z');
  assert.equal(decide(sunday, { ...snapshot(), summary: null }).workflow, MAIN);
  assert.equal(decide(sunday, { ...snapshot(), summary: { updated_at: new Date(sunday).toISOString() } }).action, 'MARKET_CLOSED');
});
test('unknown calendar can dispatch monitor; existing engine remains final trading authority', () => {
  assert.equal(decide(ms, { ...snapshot(15), calendar: null }).workflow, MONITOR);
});
test('overlapping external ticks dispatch once using persistent storage', async () => {
  const storage = new Storage(), gh = github();
  await Promise.all([tick(storage, gh, ms), tick(storage, gh, ms)]);
  assert.equal(gh.calls.length, 1);
  assert.equal((await tick(storage, gh, ms + minutes(1))).action, 'DISPATCH_COOLDOWN');
});
test('rechecks native GitHub cron activity after reservation', async () => {
  const storage = new Storage(), gh = github(); gh.active = async () => true;
  assert.equal((await tick(storage, gh, ms)).action, 'WAIT_ACTIVE_RUN_AFTER_RESERVATION');
  assert.equal(gh.calls.length, 0);
});
test('dispatch timeout is reserved, redacted and retried only on a later tick', async () => {
  const storage = new Storage(), gh = github();
  gh.dispatch = async () => { throw new Error('https://secret.invalid?token=hidden'); };
  const result = await tick(storage, gh, ms);
  assert.equal(result.error, 'SCHEDULER_REQUEST_FAILED');
  assert.equal((await tick(storage, gh, ms + minutes(1))).action, 'DISPATCH_COOLDOWN');
  assert.equal((await tick(storage, gh, ms + minutes(5))).action, 'ERROR');
});
test('API snapshot failures do not dispatch or manufacture healthy state', async () => {
  const gh = github(); gh.snapshot = async () => { throw new Error('GITHUB_HTTP_429'); };
  assert.equal((await tick(new Storage(), gh, ms)).error, 'GITHUB_HTTP_429');
  assert.equal(gh.calls.length, 0);
});
test('run history is bounded', async () => {
  const storage = new Storage(), gh = github(snapshot());
  for (let i = 0; i < 40; i++) await tick(storage, gh, ms + i);
  assert.equal((await storage.get('history')).length, 32);
});
test('GitHub token only goes to fixed API host and only allowed workflows can dispatch', async () => {
  const calls = [];
  const api = new GitHub('test-secret', async (url, options) => { calls.push({ url, options });
    return new Response(options.method === 'POST' ? null : JSON.stringify({ state: 'active' }), { status: options.method === 'POST' ? 204 : 200 }); });
  await api.dispatch(MONITOR);
  assert.equal(calls.length, 2);
  assert(calls.every(x => x.url.startsWith('https://api.github.com/repos/leo14881-eng/btc-grid-state/')));
  assert.equal(calls[1].options.body, '{"ref":"main"}');
  await assert.rejects(() => api.dispatch('hunter-position-monitor.yml'), /WORKFLOW_NOT_ALLOWED/);
  assert.equal(calls.length, 2);
});
test('disabled workflow never dispatches', async () => {
  const api = new GitHub('test-secret', async () => Response.json({ state: 'disabled_manually' }));
  await assert.rejects(() => api.dispatch(MONITOR), /WORKFLOW_NOT_ACTIVE/);
});
test('oversized active inventory fails closed instead of missing old queued runs', async () => {
  const api = new GitHub('test-secret', async () => Response.json({ total_count: 101, workflow_runs: [] }));
  await assert.rejects(() => api.active(), /ACTIVE_RUN_INVENTORY_TRUNCATED/);
});
test('active inventory only blocks stock main and monitor on main', async () => {
  const api = new GitHub('test-secret', async () => Response.json({ total_count: 1,
    workflow_runs: [{ path: '.github/workflows/hunter-position-monitor.yml', head_branch: 'main' }] }));
  assert.equal(await api.active(), false);
});
test('public routes cannot start a tick, even with arbitrary path or body', async () => {
  for (const path of ['/tick', '/health']) {
    assert.equal((await worker.fetch(new Request(`https://worker.invalid${path}`, { method: 'POST' }), {})).status, 404);
  }
});
test('missing token is visible as unhealthy; no calls are made', async () => {
  const obj = new StockScheduler({ storage: new Storage() }, {});
  assert.equal((await obj.fetch(new Request('https://internal/tick', { method: 'POST' }))).status, 503);
  const status = await obj.fetch(new Request('https://internal/health'));
  assert.equal(status.status, 503);
  assert.equal((await status.json()).error, 'GITHUB_TOKEN_MISSING');
});
test('scheduler heartbeat expiration is visible even if last run had been healthy', async () => {
  const storage = new Storage(); await storage.put('health', { action: 'FRESH', checked_at: new Date(Date.now() - minutes(15)).toISOString() });
  const obj = new StockScheduler({ storage }, {});
  assert.equal((await obj.fetch(new Request('https://internal/health'))).status, 503);
});
