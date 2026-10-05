// Infrastructure only: dispatch existing workflows; never read credentials for trading.
export const REPO = 'leo14881-eng/btc-grid-state';
export const MONITOR = 'stock-shadow-position-monitor.yml';
export const MAIN = 'stock-shadow.yml';
const MINUTE = 60_000;
const ACTIVE = ['queued', 'in_progress', 'waiting', 'pending', 'requested'];

export function nyClock(ms) {
  const p = Object.fromEntries(new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  }).formatToParts(new Date(ms)).map(x => [x.type, x.value]));
  const date = `${p.year}-${p.month}-${p.day}`;
  return { date, minute: Number(p.hour) * 60 + Number(p.minute),
    weekday: new Date(`${date}T12:00:00Z`).getUTCDay() };
}

export function marketStatus(ms, calendar) {
  const ny = nyClock(ms);
  if ([0, 6].includes(ny.weekday)) return 'CLOSED';
  const verified = Date.parse(calendar?.verified_at);
  const minute = value => {
    if (typeof value !== 'string' || !/^\d{2}:\d{2}$/.test(value)) return NaN;
    const [h, m] = value.split(':').map(Number);
    return h < 24 && m < 60 ? h * 60 + m : NaN;
  };
  const open = minute(calendar?.open), close = minute(calendar?.close);
  if (calendar?.date !== ny.date || calendar?.source !== 'ALPACA_EXCHANGE_CALENDAR' ||
      !Number.isFinite(verified) || verified > ms + MINUTE || ms - verified > 48 * 60 * MINUTE ||
      !Number.isFinite(open) || !Number.isFinite(close) || close <= open) return 'UNKNOWN';
  return ny.minute >= open && ny.minute < close ? 'OPEN' : 'CLOSED';
}

export function age(ms, timestamp) {
  const t = Date.parse(timestamp);
  return Number.isFinite(t) && t <= ms + MINUTE ? Math.max(0, ms - t) : Infinity;
}

export function monitorAge(ms, health) {
  // A failed or partial update is not a successful heartbeat.
  if (health?.status !== 'OK' || (health.errors ?? []).length ||
      (health.missing_symbols ?? []).length ||
      !Number.isInteger(health.positions_before) || !Number.isInteger(health.positions_updated) ||
      health.positions_before !== health.positions_updated) return Infinity;
  return age(ms, health.updated_at);
}

export function decide(ms, snapshot, lease = {}) {
  const market = marketStatus(ms, snapshot.calendar);
  const monitorMs = monitorAge(ms, snapshot.monitor);
  const mainMs = age(ms, snapshot.summary?.updated_at);
  const stale = market === 'OPEN' && monitorMs >= 10 * MINUTE;
  const base = { market, stale, monitor_age_seconds: Number.isFinite(monitorMs) ? Math.round(monitorMs / 1000) : null };
  if (snapshot.active) return { ...base, action: 'WAIT_ACTIVE_RUN' };
  if (ms - (lease.at ?? 0) < 5 * MINUTE) return { ...base, action: 'DISPATCH_COOLDOWN' };
  // Give the full scan a turn once per hour without letting stale monitoring wait behind it.
  const mainDue = mainMs >= 60 * MINUTE;
  if (mainDue && !stale && ms - (lease.main_at ?? 0) >= 15 * MINUTE)
    return { ...base, action: 'DISPATCH_MAIN', workflow: MAIN };
  if (market !== 'CLOSED' && monitorMs >= 4 * MINUTE)
    return { ...base, action: 'DISPATCH_MONITOR', workflow: MONITOR };
  return { ...base, action: market === 'CLOSED' ? 'MARKET_CLOSED' : 'FRESH' };
}

export class GitHub {
  constructor(token, fetcher = fetch) { this.token = token; this.fetcher = fetcher; }
  async request(path, method = 'GET', body) {
    const response = await this.fetcher(`https://api.github.com/repos/${REPO}/${path}`, {
      method, redirect: 'error', signal: AbortSignal.timeout(10_000),
      headers: { Accept: 'application/vnd.github+json', Authorization: `Bearer ${this.token}`,
        'User-Agent': 'stock-shadow-independent-scheduler', 'X-GitHub-Api-Version': '2022-11-28',
        ...(body ? { 'Content-Type': 'application/json' } : {}) },
      ...(body ? { body: JSON.stringify(body) } : {}),
    });
    // Never log GitHub response bodies or headers: they can contain private metadata.
    if (!response.ok) throw new Error(`GITHUB_HTTP_${response.status}`);
    return response.status === 204 ? null : response.json();
  }
  async file(path, ref) {
    try {
      const d = await this.request(`contents/research/results/stock-shadow/${path}?ref=${ref}`);
      if (d.encoding !== 'base64' || typeof d.content !== 'string') throw new Error('FILE_ENCODING');
      return JSON.parse(new TextDecoder().decode(Uint8Array.from(atob(d.content.replace(/\s/g, '')), c => c.charCodeAt(0))));
    } catch (e) {
      if (e.message === 'GITHUB_HTTP_404') return null;
      throw e;
    }
  }
  async active() {
    const pages = await Promise.all(ACTIVE.map(status => this.request(`actions/runs?status=${status}&per_page=100`)));
    for (const page of pages) {
      if (page.total_count > 100) throw new Error('ACTIVE_RUN_INVENTORY_TRUNCATED');
      if (!Array.isArray(page.workflow_runs)) throw new Error('ACTIVE_RUN_INVENTORY_INVALID');
    }
    return pages.some(page => page.workflow_runs.some(r =>
      r.head_branch === 'main' && [`.github/workflows/${MONITOR}`, `.github/workflows/${MAIN}`].includes(r.path)));
  }
  async snapshot() {
    const head = await this.request('commits/main');
    if (!/^[a-f0-9]{40}$/.test(head.sha)) throw new Error('INVALID_HEAD');
    const [calendar, monitor, summary, active] = await Promise.all([
      this.file('calendar-session-cache-v1.json', head.sha),
      this.file('position-monitor-v1.json', head.sha),
      this.file('summary-v1.json', head.sha), this.active(),
    ]);
    return { calendar, monitor, summary, active };
  }
  async dispatch(workflow) {
    if (![MAIN, MONITOR].includes(workflow)) throw new Error('WORKFLOW_NOT_ALLOWED');
    // Check workflow state explicitly. Do not silently enable a disabled workflow.
    const wf = await this.request(`actions/workflows/${workflow}`);
    if (wf.state !== 'active') throw new Error('WORKFLOW_NOT_ACTIVE');
    return this.request(`actions/workflows/${workflow}/dispatches`, 'POST', { ref: 'main' });
  }
}

export async function tick(storage, github, ms) {
  let result;
  try {
    const snapshot = await github.snapshot();
    let reserved = false;
    // Persistent transaction protects overlapping cron ticks and restarts.
    await storage.transaction(async txn => {
      const lease = await txn.get('lease') ?? {};
      result = decide(ms, snapshot, lease);
      if (result.workflow) {
        await txn.put('lease', { ...lease, at: ms,
          ...(result.workflow === MAIN ? { main_at: ms } : {}) });
        reserved = true;
      }
    });
    if (reserved) {
      // Close the cron/GitHub-cron race as far as the public API allows.
      // Final state mutation remains serialized by the existing Actions concurrency group.
      if (await github.active()) result.action = 'WAIT_ACTIVE_RUN_AFTER_RESERVATION';
      else {
        await github.dispatch(result.workflow);
        result.dispatched = true;
      }
    }
  } catch (e) {
    // Ambiguous POST timeouts keep the reservation: never immediately duplicate a dispatch.
    result = { ...result, action: 'ERROR', error: /^[A-Z0-9_]+$/.test(e.message) ? e.message : 'SCHEDULER_REQUEST_FAILED' };
  }
  result = { ...result, checked_at: new Date(ms).toISOString(), simulation_only: true };
  const history = (await storage.get('history') ?? []).slice(-31);
  history.push(result);
  await storage.put({ health: result, history });
  return result;
}

export class StockScheduler {
  constructor(ctx, env) { this.ctx = ctx; this.env = env; }
  async fetch(request) {
    if (request.method === 'GET') {
      const health = await this.ctx.storage.get('health') ?? { action: 'NOT_STARTED' };
      const seconds = age(Date.now(), health.checked_at) / 1000;
      const healthy = Number.isFinite(seconds) && seconds <= 600 && health.action !== 'ERROR' && !health.stale;
      return Response.json({ ...health, scheduler_healthy: healthy,
        scheduler_age_seconds: Number.isFinite(seconds) ? Math.round(seconds) : null }, { status: healthy ? 200 : 503 });
    }
    if (request.method !== 'POST') return new Response('Method not allowed', { status: 405 });
    if (!this.env.GITHUB_ACTIONS_TOKEN) {
      const result = { action: 'ERROR', error: 'GITHUB_TOKEN_MISSING', checked_at: new Date().toISOString() };
      await this.ctx.storage.put('health', result);
      return Response.json(result, { status: 503 });
    }
    return Response.json(await tick(this.ctx.storage, new GitHub(this.env.GITHUB_ACTIONS_TOKEN), Date.now()));
  }
}

export default {
  async scheduled(event, env, ctx) {
    const stub = env.STOCK_SCHEDULER.get(env.STOCK_SCHEDULER.idFromName('stock-shadow-main'));
    ctx.waitUntil(stub.fetch('https://scheduler.internal/tick', { method: 'POST' }).then(async r => {
      const result = await r.json();
      console.log(JSON.stringify(result));
      if (!r.ok || result.action === 'ERROR') throw new Error('STOCK_SCHEDULER_FAILED');
    }));
  },
  async fetch(request, env) {
    const url = new URL(request.url);
    // Public endpoint is read-only; no HTTP route can trigger or modify anything.
    if (request.method !== 'GET' || url.pathname !== '/health') return new Response('Not found', { status: 404 });
    const stub = env.STOCK_SCHEDULER.get(env.STOCK_SCHEDULER.idFromName('stock-shadow-main'));
    return stub.fetch('https://scheduler.internal/health');
  },
};
