// Replace the complete Cloudflare Worker with this file. No trading routes.
const headers = { "content-type": "application/json;charset=UTF-8", "cache-control": "no-store" };
const reply = (body, status = 200, extra = {}) => Response.json(body, { status, headers: { ...headers, ...extra } });
const symbolPattern = /^[A-Z0-9]{2,30}$/;

async function upstream(path, params, signal) {
  const url = new URL("https://api.bybit.com" + path);
  for (const [key, value] of Object.entries(params)) url.searchParams.set(key, String(value));
  const response = await fetch(url, {
    headers: { "User-Agent": "Hunter-Bybit-Proxy/2.0", "Accept": "application/json" }, signal
  });
  if (!response.ok) throw new Error("BYBIT_HTTP_" + response.status);
  const data = await response.json();
  if (path === "/v5/market/orderbook" && data.retCode === 0 && data.result?.s === params.symbol) data.result.category = "spot";
  if (data.retCode !== 0 || data.result?.category !== "spot") throw new Error("BYBIT_RET_" + data.retCode);
  if (!Number.isFinite(Number(data.time)) || Math.abs(Number(data.time) - Date.now()) > 120000) throw new Error("BYBIT_STALE_TIME");
  return data;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/debug") return reply({
      colo: request.cf?.colo || null, country: request.cf?.country || null,
      city: request.cf?.city || null, region: request.cf?.region || null, timezone: request.cf?.timezone || null
    });
    if (url.pathname === "/health") return reply({ ok: true, service: "hunter-bybit-proxy", version: 3, capabilities: ["tickers", "orderbooks", "klines", "early-klines"] });

    // Historical/management candles: fixed spot endpoint, bounded parameters.
    if (url.pathname === "/bybit/klines") {
      if (request.method !== "GET") return reply({ ok: false, error: "METHOD_NOT_ALLOWED" }, 405, { Allow: "GET" });
      const symbol = url.searchParams.get("symbol") || "";
      const interval = url.searchParams.get("interval") || "";
      const limit = Number(url.searchParams.get("limit") || 1000);
      const params = { category: "spot", symbol, interval, limit };
      if (!symbolPattern.test(symbol) || !symbol.endsWith("USDT") || !["5", "15", "60"].includes(interval) || !Number.isInteger(limit) || limit < 1 || limit > 1000) return reply({ ok: false, error: "INVALID_KLINE_QUERY" }, 400);
      for (const key of ["start", "end"]) {
        if (url.searchParams.has(key)) {
          const value = Number(url.searchParams.get(key));
          if (!Number.isSafeInteger(value) || value <= 0 || value > Date.now()) return reply({ ok: false, error: "INVALID_KLINE_TIME" }, 400);
          params[key] = value;
        }
      }
      if (params.start && params.end && params.start > params.end) return reply({ ok: false, error: "INVALID_KLINE_TIME" }, 400);
      const controller = new AbortController(); const timer = setTimeout(() => controller.abort(), 15000);
      try { return reply(await upstream("/v5/market/kline", params, controller.signal)); }
      catch (error) { return reply({ ok: false, error: "BYBIT_KLINE_FAILED", detail: String(error) }, 502); }
      finally { clearTimeout(timer); }
    }

    // Bounded order-book batch. No private/order-creation endpoints.
    if (url.pathname === "/bybit/orderbooks") {
      if (request.method !== "POST") return reply({ ok: false, error: "METHOD_NOT_ALLOWED" }, 405, { Allow: "POST" });
      let symbols;
      try {
        const raw = await request.text(); if (raw.length > 2048) throw new Error("size");
        const body = JSON.parse(raw); symbols = body.symbols;
        if (Object.keys(body).some(k => k !== "symbols") || !Array.isArray(symbols) || symbols.length < 1 || symbols.length > 20 || new Set(symbols).size !== symbols.length || symbols.some(s => typeof s !== "string" || !symbolPattern.test(s) || !s.endsWith("USDT"))) throw new Error("symbols");
      } catch { return reply({ ok: false, error: "INVALID_BOOK_BATCH" }, 400); }
      const books = {}; const failures = {}; let index = 0;
      const controller = new AbortController(); const timer = setTimeout(() => controller.abort(), 45000);
      const one = async () => {
        while (index < symbols.length) {
          const symbol = symbols[index++];
          try { books[symbol] = await upstream("/v5/market/orderbook", { category: "spot", symbol, limit: 200 }, controller.signal); }
          catch (error) { failures[symbol] = String(error); }
        }
      };
      try { await Promise.all(Array.from({ length: 4 }, one)); }
      finally { clearTimeout(timer); }
      return reply({ retCode: 0, time: Date.now(), result: { category: "spot", books, failures } });
    }

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
        return reply(await upstream(path, params, controller.signal));
      } catch (error) {
        return reply({ ok: false, error: "BYBIT_FETCH_FAILED", detail: String(error) }, 502);
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
      const klines = {}; const failures = {}; let index = 0;
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 45000);
      const one = async () => {
        while (index < jobs.length) {
          const job = jobs[index++];
          try {
            const data = await upstream("/v5/market/kline", { category: "spot", ...job, end: body.end }, controller.signal);
            if (data.result.symbol !== job.symbol || !Array.isArray(data.result.list) || data.result.list.length !== job.limit) throw new Error("INCOMPLETE_KLINE");
            (klines[job.symbol] ||= {})[job.interval] = data;
          } catch (error) { (failures[job.symbol] ||= {})[job.interval] = String(error); }
        }
      };
      try { await Promise.all(Array.from({ length: 4 }, one)); }
      finally { clearTimeout(timer); }
      return reply({ retCode: 0, retMsg: "OK", time: Date.now(), result: { category: "spot", end: body.end, klines, failures } });
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
