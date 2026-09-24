// Advanced-mode Cloudflare Pages Worker for ai-humanoid-screen.
//
// Serves GET /api/quote?symbol=XXX and hands everything else to the static assets.
//
// WHY _worker.js AND NOT functions/api/quote.js: `wrangler pages deploy <dir>` resolves a
// `functions/` directory relative to the CURRENT WORKING DIRECTORY, not inside <dir>. A
// functions/ folder placed inside the assets directory is uploaded as a static file and
// never routed — verified 2026-09-24: /api/quote returned index.html with
// content-type text/html. agents/site_generator.py:1747 already uses this _worker.js shape.
//
// The page is static and Yahoo sends no Access-Control-Allow-Origin, so the browser cannot
// call it directly. This runs server-side, where there is no CORS to satisfy and no key to
// leak. Cached 10 minutes at the edge: a refresh is a human clicking, and Yahoo rate-limits
// hard (a bare request from a datacentre IP already returns 429).

const TTL = 600;

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname === "/api/quote") {
      if (request.method !== "GET") return json({ error: "GET only" }, 405);
      return quote(url, ctx);
    }
    return env.ASSETS.fetch(request);
  },
};

async function quote(url, ctx) {
  const sym = (url.searchParams.get("symbol") || "").trim().toUpperCase();
  // Whitelist the shape rather than escaping it: this is interpolated into an upstream URL,
  // and tickers are a small, well-defined alphabet.
  if (!/^[A-Z0-9.\-^=]{1,12}$/.test(sym)) return json({ error: "bad symbol" }, 400);

  const cache = caches.default;
  const key = new Request("https://ai-humanoid-screen.internal/q/" + sym);
  const hit = await cache.match(key);
  if (hit) return hit;

  let upstream;
  try {
    upstream = await fetch(
      "https://query1.finance.yahoo.com/v8/finance/chart/" +
        encodeURIComponent(sym) + "?range=2y&interval=1d",
      {
        headers: { "User-Agent": "Mozilla/5.0", Accept: "application/json" },
        cf: { cacheTtl: TTL, cacheEverything: true },
      }
    );
  } catch (e) {
    return json({ error: "upstream unreachable" }, 502);
  }

  if (!upstream.ok) {
    // Name the rate limit rather than reporting a generic failure — it is the common one
    // and it tells the reader to wait rather than to doubt the symbol.
    return json(
      { error: upstream.status === 429
          ? "rate limited by Yahoo — try again in a minute"
          : "upstream " + upstream.status },
      502
    );
  }

  let body;
  try {
    body = await upstream.json();
  } catch (e) {
    return json({ error: "bad upstream json" }, 502);
  }

  const res = body && body.chart && body.chart.result && body.chart.result[0];
  const ts = res && res.timestamp;
  const q = res && res.indicators && res.indicators.quote && res.indicators.quote[0];
  if (!ts || !q) return json({ error: "no data for " + sym }, 404);

  // Drop any bar with a null close. Yahoo returns nulls for sessions it has not finished
  // publishing — the same gap that made the nightly screen 67% "stale" before it started
  // anchoring to a well-covered session.
  const bars = [];
  for (let i = 0; i < ts.length; i++) {
    const c = q.close && q.close[i];
    if (c === null || c === undefined || !isFinite(c)) continue;
    bars.push({
      d: new Date(ts[i] * 1000).toISOString().slice(0, 10),
      o: num(q.open && q.open[i]),
      h: num(q.high && q.high[i]),
      l: num(q.low && q.low[i]),
      c: +c,
      v: num(q.volume && q.volume[i]),
    });
  }
  if (bars.length < 25) return json({ error: "only " + bars.length + " bars for " + sym }, 404);

  const meta = res.meta || {};
  const out = json(
    {
      symbol: sym,
      name: meta.longName || meta.shortName || sym,
      currency: meta.currency || "USD",
      as_of: bars[bars.length - 1].d,
      fetched_at: new Date().toISOString(),
      bars: bars,
    },
    200,
    TTL
  );
  ctx.waitUntil(cache.put(key, out.clone()));
  return out;
}

function num(x) {
  return x === null || x === undefined || !isFinite(x) ? null : +x;
}

function json(obj, status, ttl) {
  return new Response(JSON.stringify(obj), {
    status: status || 200,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": ttl ? "public, max-age=" + ttl : "no-store",
      "access-control-allow-origin": "*",
    },
  });
}
