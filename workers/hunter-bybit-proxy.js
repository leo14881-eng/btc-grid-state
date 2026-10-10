// Replace the complete Cloudflare Worker with this file. No trading routes.
const headers = { "content-type": "application/json;charset=UTF-8", "cache-control": "no-store" };
const reply = (body, status = 200, extra = {}) => Response.json(body, { status, headers: { ...headers, ...extra } });
const symbolPattern = /^[A-Z0-9]{2,30}$/;
const BUILD = "hunter-bybit-httpdiag-v1";
const BODY_LIMIT = 4096;
const headerRules = {
  "cf-ray": /^[0-9a-fA-F]{16,32}(?:-[A-Z]{3})?$/,
  "traceid": /^[0-9a-fA-F]{16,64}$/,
  "x-amz-cf-id": /^[A-Za-z0-9_=-]{16,128}$/,
  "retry-after": /^[0-9]{1,6}$/,
};

async function httpEvidence(response, requestId, failureKind = "http") {
  const safeHeaders = { "x-hunter-worker-build": BUILD, "x-hunter-request-id": requestId };
  for (const [key, pattern] of Object.entries(headerRules)) {
    const value = response?.headers.get(key);
    if (value && pattern.test(value)) safeHeaders[key] = value;
  }
  const bytes = new Uint8Array(BODY_LIMIT + 1);
  let size = 0; let readFailed = false;
  // Read only a prefix of failed responses; never retain raw bodies in output.
  if (response?.body && failureKind === "http") {
    const reader = response.body.getReader();
    try {
      while (size < bytes.length) {
        const { done, value } = await reader.read();
        if (done) break;
        const used = Math.min(value.length, bytes.length - size);
        bytes.set(value.subarray(0, used), size); size += used;
      }
    } catch { readFailed = true; }
    finally { try { await reader.cancel(); } catch { readFailed = true; } }
  }
  const truncated = size > BODY_LIMIT;
  const sampled = Math.min(size, BODY_LIMIT);
  const text = new TextDecoder().decode(bytes.subarray(0, sampled));
  let kind = !response || failureKind !== "http" || readFailed ? "unavailable" : sampled ? "text" : "empty";
  let retCode = null;
  if (sampled && !truncated) {
    try {
      const body = JSON.parse(text); kind = "json";
      if (Number.isSafeInteger(body?.retCode) && Math.abs(body.retCode) <= 9999999999) retCode = body.retCode;
    } catch { if (/<!doctype\s+html|<html(?:\s|>)/i.test(text)) kind = "html"; }
  }
  const markers = [];
  for (const [marker, pattern] of [["ACCESS_DENIED_TEXT", /access denied/i],
    ["COUNTRY_BLOCK_TEXT", /block access from your country/i], ["RATE_LIMIT_TEXT", /too many requests|rate limit|access too frequent/i]]) {
    if (pattern.test(text)) markers.push(marker);
  }
  return { schema: "hunter_http_evidence_v1", layer: "bybit_upstream", http_status: response?.status || 0,
    failure_kind: failureKind, headers: safeHeaders, body_kind: kind, body_bytes_sampled: sampled,
    body_truncated: truncated, body_read_failed: readFailed, body_markers: markers, ret_code: retCode };
}

function evidenceError(code, evidence) {
  const error = new Error(code); error.httpEvidence = evidence; return error;
}

async function upstream(path, params, signal, requestId) {
  const started = Date.now(); const monotonic = performance.now(); const observed = {};
  try { return await upstreamRequest(path, params, signal, requestId, observed); }
  finally {
    // One bounded line per existing public request, including successes. Never
    // log URLs, raw bodies, caller headers, IPs, or the protected Alpha route.
    // These are observations, not a decision about the cause of HTTP 403.
    try {
      const safeHeaders = {};
      const rules = { ...headerRules, "x-bapi-limit": /^[0-9]{1,10}$/,
        "x-bapi-limit-status": /^[0-9]{1,10}$/, "x-bapi-limit-reset-timestamp": /^[0-9]{1,16}$/ };
      for (const [key, pattern] of Object.entries(rules)) {
        const value = observed.response?.headers.get(key);
        if (value && pattern.test(value)) safeHeaders[key] = value;
      }
      console.log("HUNTER_BYBIT_REQUEST_OBSERVATION " + JSON.stringify({
        schema: "hunter_bybit_request_observation_v1", layer: "bybit_upstream",
        job: path === "/v5/market/kline" ? "early_klines" : path === "/v5/market/tickers" ? "tickers" : "spot",
        symbol: symbolPattern.test(params.symbol || "") ? params.symbol : null,
        interval: ["15", "60"].includes(params.interval) ? params.interval : null,
        started_at_utc: new Date(started).toISOString(), completed_at_utc: new Date(Date.now()).toISOString(),
        duration_ms: Math.max(0, Math.round(performance.now() - monotonic)),
        http_status: observed.response?.status || 0, ret_code: observed.retCode ?? null,
        worker_request_id: requestId, worker_build: BUILD, headers: safeHeaders, root_cause: "UNKNOWN"
      }));
    } catch { /* Observation failures must not change market results. */ }
  }
}

async function upstreamRequest(path, params, signal, requestId, observed) {
  const url = new URL("https://api.bybit.com" + path);
  for (const [key, value] of Object.entries(params)) url.searchParams.set(key, String(value));
  let response;
  try {
    response = await fetch(url, {
      headers: { "User-Agent": "Hunter-Bybit-Proxy/2.0", "Accept": "application/json" }, signal
    });
    observed.response = response;
  } catch (error) {
    const timeout = error?.name === "AbortError";
    const wrapped = evidenceError(timeout ? "upstream aborted" : "WORKER_KLINE_FAILURE",
      await httpEvidence(null, requestId, timeout ? "timeout" : "transport"));
    if (timeout) wrapped.name = "AbortError";
    throw wrapped;
  }
  if (!response.ok) {
    const evidence = await httpEvidence(response, requestId);
    observed.retCode = evidence.ret_code;
    throw evidenceError("BYBIT_HTTP_" + response.status, evidence);
  }
  let data;
  try { data = await response.json(); }
  catch (error) {
    const kind = error?.name === "AbortError" ? "timeout" : error instanceof SyntaxError ? "invalid_json" : "transport";
    const wrapped = evidenceError(kind === "timeout" ? "upstream aborted" : "WORKER_KLINE_FAILURE",
      await httpEvidence(response, requestId, kind));
    // Legacy Python classifies AbortError by name; keep that canonical contract.
    if (kind === "timeout") wrapped.name = "AbortError";
    throw wrapped;
  }
  observed.retCode = Number.isSafeInteger(data?.retCode) && Math.abs(data.retCode) <= 9999999999 ? data.retCode : null;
  if (data?.retCode !== 0 || data.result?.category !== "spot") {
    const code = Number.isSafeInteger(data?.retCode) && Math.abs(data.retCode) <= 9999999999 ? data.retCode : "INVALID";
    throw new Error("BYBIT_RET_" + code);
  }
  if (!Number.isFinite(Number(data.time)) || Math.abs(Number(data.time) - Date.now()) > 120000) throw new Error("BYBIT_STALE_TIME");
  return data;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const requestId = crypto.randomUUID();
    const diagnosticHeaders = { "x-hunter-worker-build": BUILD, "x-hunter-request-id": requestId };
    if (url.pathname === "/debug") return reply({
      colo: request.cf?.colo || null, country: request.cf?.country || null,
      city: request.cf?.city || null, region: request.cf?.region || null, timezone: request.cf?.timezone || null
    });
    if (url.pathname === "/health") return reply({ ok: true, service: "hunter-bybit-proxy", version: 2, build: BUILD }, 200, diagnosticHeaders);

    // One complete instruments or ticker list. Only listings are cached by GitHub.
    if (url.pathname === "/bybit/spot" || url.pathname === "/bybit/tickers") {
      if (request.method !== "GET") return reply({ ok: false, error: "METHOD_NOT_ALLOWED" }, 405, { Allow: "GET" });
      const raw = url.searchParams.get("symbol");
      const symbol = raw === null ? null : raw.toUpperCase().trim();
      if (symbol !== null && !symbolPattern.test(symbol)) return reply({ ok: false, error: "INVALID_SYMBOL" }, 400);
      const params = { category: "spot" };
      if (symbol !== null) params.symbol = symbol;
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 15000);
      try {
        const path = url.pathname === "/bybit/spot" ? "/v5/market/instruments-info" : "/v5/market/tickers";
        return reply(await upstream(path, params, controller.signal, requestId), 200, diagnosticHeaders);
      } catch (error) {
        return reply({ ok: false, error: "BYBIT_FETCH_FAILED", detail: String(error),
          ...(error.httpEvidence ? { diagnostics: error.httpEvidence } : {}) }, 502, diagnosticHeaders);
      } finally { clearTimeout(timer); }
    }

    // Fixed, bounded batch: up to 20 symbols, exactly two EARLY candle windows.
    // At most 40 upstream requests, four in flight. Partial failures stay explicit.
    if (url.pathname === "/bybit/early-klines") {
      if (request.method !== "POST") return reply({ ok: false, error: "METHOD_NOT_ALLOWED" }, 405, { Allow: "POST" });
      let body;
      try {
        const raw = await request.text();
        if (raw.length > 2048) throw new Error("too large");
        body = JSON.parse(raw);
        if (!body || typeof body !== "object" || Array.isArray(body) || Object.keys(body).some(k => !["symbols", "end"].includes(k))) throw new Error("shape");
        if (!Array.isArray(body.symbols) || body.symbols.length < 1 || body.symbols.length > 20 || new Set(body.symbols).size !== body.symbols.length) throw new Error("symbols");
        if (body.symbols.some(s => typeof s !== "string" || !symbolPattern.test(s) || !s.endsWith("USDT"))) throw new Error("symbol");
        if (!Number.isSafeInteger(body.end) || body.end > Date.now() || body.end < Date.now() - 600000) throw new Error("end");
      } catch { return reply({ ok: false, error: "INVALID_KLINE_BATCH" }, 400); }
      const jobs = body.symbols.flatMap(symbol => [
        { symbol, interval: "60", limit: 5 }, { symbol, interval: "15", limit: 25 }
      ]);
      const klines = {}; const failures = {}; const failureDiagnostics = {}; let index = 0;
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 45000);
      const one = async () => {
        while (index < jobs.length) {
          const job = jobs[index++];
          try {
            const data = await upstream("/v5/market/kline", { category: "spot", ...job, end: body.end }, controller.signal, requestId);
            if (data.result.symbol !== job.symbol || !Array.isArray(data.result.list) || data.result.list.length !== job.limit) throw new Error("INCOMPLETE_KLINE");
            (klines[job.symbol] ||= {})[job.interval] = data;
          } catch (error) {
            (failures[job.symbol] ||= {})[job.interval] = String(error);
            if (error.httpEvidence) (failureDiagnostics[job.symbol] ||= {})[job.interval] = error.httpEvidence;
          }
        }
      };
      try { await Promise.all(Array.from({ length: 4 }, one)); }
      finally { clearTimeout(timer); }
      return reply({ retCode: 0, retMsg: "OK", time: Date.now(), result: { category: "spot", end: body.end, klines, failures,
        ...(Object.keys(failureDiagnostics).length ? { failure_diagnostics: failureDiagnostics } : {}) } }, 200, diagnosticHeaders);
    }

    // Preserve the protected Alpha endpoint and the exact signed request bytes.
    if (url.pathname === "/bybit/alpha/token-list") {
      if (request.method !== "POST") return reply({ ok: false, error: "METHOD_NOT_ALLOWED" }, 405, { Allow: "POST" });
      const proxyToken = request.headers.get("X-Hunter-Proxy-Token") || "";
      if (!env.HUNTER_PROXY_TOKEN || proxyToken !== env.HUNTER_PROXY_TOKEN) return reply({ ok: false, error: "UNAUTHORIZED" }, 401);
      const names = ["X-BAPI-API-KEY", "X-BAPI-TIMESTAMP", "X-BAPI-RECV-WINDOW", "X-BAPI-SIGN"];
      if (names.some(name => !request.headers.get(name))) return reply({ ok: false, error: "MISSING_BYBIT_AUTH_HEADERS" }, 400);
      let body;
      try {
        body = await request.text(); const parsed = JSON.parse(body);
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed) || !Object.hasOwn(parsed, "tokenTag") || Object.keys(parsed).some(key => key !== "tokenTag")) throw new Error("body");
      } catch { return reply({ ok: false, error: "INVALID_ALPHA_BODY" }, 400); }
      try {
        const auth = Object.fromEntries(names.map(name => [name, request.headers.get(name)]));
        const response = await fetch("https://api.bybit.com/v5/alpha/trade/biz-token-list", {
          method: "POST", headers: { "User-Agent": "Hunter-Bybit-Proxy/2.0", "Accept": "application/json", "Content-Type": "application/json", ...auth }, body
        });
        return new Response(response.body, { status: response.status, headers });
      } catch (error) { return reply({ ok: false, error: "BYBIT_ALPHA_FETCH_FAILED", detail: String(error) }, 502); }
    }
    if (url.pathname === "/") return reply({ ok: true, service: "hunter-bybit-proxy", usage: {
      spot: "GET /bybit/spot", single: "GET /bybit/spot?symbol=BTCUSDT",
      tickers: "GET /bybit/tickers", early: "POST /bybit/early-klines", alpha: "POST /bybit/alpha/token-list"
    } });
    return reply({ ok: false, error: "NOT_FOUND" }, 404);
  }
};
