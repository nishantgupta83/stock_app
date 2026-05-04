"""
News agent.

Polls free RSS feeds (CNBC, MarketWatch, Seeking Alpha), dedupes by article id,
classifies by ticker mention + sentiment keywords, and writes:
  - stock_raw_news (raw articles)
  - stock_normalized_events (one event per (article, affected_ticker) pair)

Truth Social is one signal source among several — this agent provides a second
independent source so 8-K + news clusters can satisfy the §15.3 cluster rule
without requiring a Trump post.

Run via .github/workflows/news_agent.yml on */5 cron.
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
import time
from datetime import datetime, timezone

import feedparser
import requests

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

HEADERS_SB = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
    "Prefer":        "resolution=ignore-duplicates,return=minimal",
}

# Free RSS feeds — no API keys, confirmed reachable from GitHub Actions IPs
_FEEDS = [
    ("cnbc_markets",    "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=15839069"),
    ("marketwatch",     "https://feeds.marketwatch.com/marketwatch/topstories/"),
    ("seeking_alpha",   "https://seekingalpha.com/market_currents.xml"),
]

# ============================================================
# Classifier — ticker + sentiment
# ============================================================

# Tickers to scan for directly (word-boundary match, case-insensitive)
_WATCHLIST_TICKERS = [
    "NVDA","AAPL","MSFT","AMZN","AVGO","GOOGL","GOOG","META","TSLA","BRK.B",
    "JPM","LLY","XOM","JNJ","WMT","V","NFLX","COST","MA","AMD","DJT","SPY",
    "QQQ","XLK","XLF","XLE","XLI","TLT","COIN","MSTR","FXI",
]

_COMPANY_MAP = {
    "nvidia":      "NVDA",
    "apple":       "AAPL",
    "microsoft":   "MSFT",
    "amazon":      "AMZN",
    "broadcom":    "AVGO",
    "alphabet":    "GOOGL",
    "google":      "GOOGL",
    "meta":        "META",
    "facebook":    "META",
    "tesla":       "TSLA",
    "berkshire":   "BRK.B",
    "jpmorgan":    "JPM",
    "j.p. morgan": "JPM",
    "eli lilly":   "LLY",
    "exxon":       "XOM",
    "johnson":     "JNJ",
    "walmart":     "WMT",
    "netflix":     "NFLX",
    "costco":      "COST",
    "mastercard":  "MA",
    "coinbase":    "COIN",
    "microstrategy": "MSTR",
}

_BULLISH_RE = re.compile(
    r"\b(beat(s)?|surge(s|d)?|jumps?|rally|raises?\s+guidance|buyback|"
    r"acquisition|upgrade(s|d)?|record\s+(high|profit|revenue)|dividend|"
    r"strong\s+(earnings|results)|outperform)\b", re.I
)
_BEARISH_RE = re.compile(
    r"\b(miss(es|ed)?|fall(s|ing)?|drop(s|ped)?|cut(s)?\s+guidance|"
    r"layoff|investigation|lawsuit|recall|downgrade(s|d)?|"
    r"disappointing|warning|loss(es)?|below\s+expectations)\b", re.I
)

# DB-backed override layer. Loaded by load_rules() at start of main() so the
# user can add tickers + sentiment phrases via INSERTs into stock_keyword_rules
# without redeploying. The hardcoded constants above stay as a safety net for
# Supabase outages — see _RULES_SOURCE for which path was used in any given run.
_RULES_NAME_HITS: list[dict] = []        # ticker rules: {keyword, match_type, tickers, rule_label}
_RULES_BULLISH:   list[dict] = []        # sentiment rules with direction_prior=long
_RULES_BEARISH:   list[dict] = []        # sentiment rules with direction_prior=short
_RULES_SOURCE:    str         = "keyword_fallback"
_REGEX_CACHE:     dict[str, re.Pattern] = {}


def load_rules() -> str:
    """Populate _RULES_NAME_HITS / _RULES_BULLISH / _RULES_BEARISH from Supabase.
    Returns the source tag used ('keyword_db' or 'keyword_fallback')."""
    global _RULES_NAME_HITS, _RULES_BULLISH, _RULES_BEARISH
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/stock_keyword_rules",
            headers=HEADERS_SB,
            params={
                "kind":    "eq.news",
                "enabled": "eq.true",
                "select":  "keyword,match_type,direction_prior,tickers,rule_label",
                "limit":   "500",
            },
            timeout=15,
        )
        if r.status_code == 200:
            rows = r.json() or []
            ticker_rules = [r_ for r_ in rows if r_.get("tickers")]
            sentiment_rules = [r_ for r_ in rows if not r_.get("tickers")]
            if ticker_rules or sentiment_rules:
                _RULES_NAME_HITS = ticker_rules
                _RULES_BULLISH = [r_ for r_ in sentiment_rules if r_.get("direction_prior") == "long"]
                _RULES_BEARISH = [r_ for r_ in sentiment_rules if r_.get("direction_prior") == "short"]
                return "keyword_db"
    except Exception as e:  # noqa: BLE001
        print(f"  load_rules: DB fetch failed, using fallback ({e})", file=sys.stderr)

    # Fallback: derive from the in-process constants so we always classify the
    # core mega-cap names + the existing bullish/bearish regex sentiment.
    _RULES_NAME_HITS = [
        {"keyword": name, "match_type": "icontains", "tickers": [ticker],
         "direction_prior": "neutral", "rule_label": f"name_{ticker}"}
        for name, ticker in _COMPANY_MAP.items()
    ]
    _RULES_BULLISH = [{"keyword": _BULLISH_RE.pattern, "match_type": "regex",
                       "tickers": [], "direction_prior": "long",
                       "rule_label": "sentiment_bullish"}]
    _RULES_BEARISH = [{"keyword": _BEARISH_RE.pattern, "match_type": "regex",
                       "tickers": [], "direction_prior": "short",
                       "rule_label": "sentiment_bearish"}]
    return "keyword_fallback"


def _matches(text: str, rule: dict) -> bool:
    kw = rule["keyword"]
    if rule.get("match_type") == "regex":
        pattern = _REGEX_CACHE.get(kw)
        if pattern is None:
            try:
                pattern = re.compile(kw, re.I)
            except re.error:
                return False
            _REGEX_CACHE[kw] = pattern
        return bool(pattern.search(text))
    return kw.lower() in text.lower()


def _article_id(entry: dict, source: str) -> str:
    raw = entry.get("id") or entry.get("guid") or entry.get("link") or ""
    return hashlib.sha1(f"{source}:{raw}".encode()).hexdigest()[:24]


def classify(text: str) -> list[dict]:
    """Return list of {ticker, direction_prior, sentiment_label, classified_by} for
    tickers mentioned. Uses DB-loaded rules + watchlist symbol scan."""
    if not text:
        return []
    hits: dict[str, dict] = {}

    # 1. Raw ticker symbols still come from the in-process watchlist — these are
    # uppercase tokens, not "rules" worth editing in DB. Symbols are derived from
    # stock_watchlists at deploy time.
    for ticker in _WATCHLIST_TICKERS:
        pattern = _REGEX_CACHE.get(f"sym_{ticker}")
        if pattern is None:
            pattern = re.compile(r"\b" + re.escape(ticker) + r"\b", re.I)
            _REGEX_CACHE[f"sym_{ticker}"] = pattern
        if pattern.search(text):
            hits.setdefault(ticker, {"ticker": ticker})

    # 2. DB-loaded company-name rules (icontains / regex per row).
    for rule in _RULES_NAME_HITS:
        if not _matches(text, rule):
            continue
        for t in rule.get("tickers") or []:
            hits.setdefault(t, {"ticker": t})

    if not hits:
        return []

    # 3. Article-wide sentiment from DB-loaded sentiment rules. First-match wins
    # per direction; conflict (both long + short matched) → neutral.
    bullish = any(_matches(text, r) for r in _RULES_BULLISH)
    bearish = any(_matches(text, r) for r in _RULES_BEARISH)
    if bullish and not bearish:
        direction, label = "long",    "positive"
    elif bearish and not bullish:
        direction, label = "short",   "negative"
    else:
        direction, label = "neutral", "neutral"

    for t in hits:
        hits[t]["direction_prior"]  = direction
        hits[t]["sentiment_label"]  = label
        hits[t]["classified_by"]    = _RULES_SOURCE

    return list(hits.values())


# ============================================================
# Operational logging (mirrors truth_social_agent.py)
# ============================================================

def job_run_start(agent: str) -> int | None:
    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/stock_job_runs",
            headers={**HEADERS_SB, "Prefer": "return=representation"},
            json={"agent": agent}, timeout=10,
        )
        if r.status_code in (200, 201) and r.json():
            return r.json()[0]["id"]
    except Exception as e:  # noqa: BLE001
        print(f"  job_run_start failed: {e}", file=sys.stderr)
    return None


def job_run_finish(run_id: int | None, status: str, rows_in: int, rows_out: int, err: str | None = None) -> None:
    if run_id is None:
        return
    try:
        requests.patch(
            f"{SUPABASE_URL}/rest/v1/stock_job_runs?id=eq.{run_id}",
            headers=HEADERS_SB,
            json={
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "status":      status,
                "rows_in":     rows_in,
                "rows_out":    rows_out,
                "error_text":  err,
            }, timeout=10,
        )
    except Exception as e:  # noqa: BLE001
        print(f"  job_run_finish failed: {e}", file=sys.stderr)


def dead_letter(agent: str, reason: str, detail: str) -> None:
    try:
        requests.post(
            f"{SUPABASE_URL}/rest/v1/stock_dead_letter_events",
            headers=HEADERS_SB,
            json={"agent": agent, "reason": reason, "detail": detail[:2000], "payload": {}},
            timeout=10,
        )
    except Exception as e:  # noqa: BLE001
        print(f"  dead_letter failed: {e}", file=sys.stderr)


# ============================================================
# Ingestion
# ============================================================

def already_seen_dedupe_keys(keys: list[str]) -> set[str]:
    """Check which news dedupe_keys already exist in normalized_events."""
    if not keys:
        return set()
    in_list = ",".join(f'"{k}"' for k in keys)
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/stock_normalized_events"
        f"?dedupe_key=in.({in_list})&select=dedupe_key",
        headers=HEADERS_SB, timeout=15,
    )
    if r.status_code != 200:
        return set()
    return {row["dedupe_key"] for row in r.json()}


def upsert_raw_news(articles: list[dict]) -> dict[tuple[str, str], int]:
    """Insert raw RSS articles first and return {(source, article_id): row_id}."""
    if not articles:
        return {}
    raw_rows = [{
        "source":       a["source"],
        "external_id":  a["article_id"],
        "headline":     a["headline"][:500],
        "url":          a.get("url"),
        "published_at": a["published_at"],
        "raw_payload":  {
            "article_id": a["article_id"],
            "summary":    a.get("summary"),
            "url":        a.get("url"),
        },
    } for a in articles]
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/stock_raw_news?on_conflict=source,external_id",
        headers={**HEADERS_SB, "Prefer": "resolution=merge-duplicates,return=representation"},
        json=raw_rows,
        timeout=30,
    )
    if r.status_code not in (200, 201, 204):
        print(f"  raw news upsert {r.status_code}: {r.text[:300]}", file=sys.stderr)
        return {}
    rows = r.json() if r.text else []
    if not rows:
        ids = ",".join(f'"{a["article_id"]}"' for a in articles)
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/stock_raw_news?external_id=in.({ids})&select=id,source,external_id",
            headers=HEADERS_SB,
            timeout=20,
        )
        rows = r.json() if r.status_code == 200 else []
    return {
        (str(row.get("source")), str(row.get("external_id"))): int(row["id"])
        for row in rows
        if row.get("id") is not None and row.get("source") and row.get("external_id")
    }


def emit_news_events(articles: list[dict], raw_ids: dict[tuple[str, str], int]) -> int:
    """One normalized event per (article, classified ticker)."""
    rows = []
    for a in articles:
        hits = classify(a["headline"] + " " + (a.get("summary") or ""))
        if not hits:
            continue
        for h in hits:
            sev = 3 if h["direction_prior"] != "neutral" else 2
            rows.append({
                "event_type":        "news_article",
                "event_subtype":     h["sentiment_label"],
                "ticker":            h["ticker"],
                "event_at":          a["published_at"],
                "severity":          sev,
                "source_table":      "stock_raw_news",
                "source_id":         raw_ids.get((a["source"], a["article_id"])),
                "parser_confidence": 0.55,
                "dedupe_key":        f"news_{a['article_id']}_{h['ticker']}",
                "payload": {
                    "article_id":      a["article_id"],
                    "headline":        a["headline"][:200],
                    "url":             a.get("url"),
                    "source":          a["source"],
                    "direction_prior": h["direction_prior"],
                    "classified_by":   h.get("classified_by") or _RULES_SOURCE,
                },
            })
    if not rows:
        return 0
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/stock_normalized_events?on_conflict=dedupe_key",
        headers={**HEADERS_SB, "Prefer": "resolution=merge-duplicates,return=minimal"},
        json=rows,
        timeout=20,
    )
    if r.status_code not in (200, 201, 204):
        print(f"  events insert {r.status_code}: {r.text[:300]}", file=sys.stderr)
        return 0
    return len(rows)


def poll_feed(source_name: str, feed_url: str) -> list[dict]:
    """Parse one RSS feed, return normalized article dicts."""
    articles = []
    try:
        feed = feedparser.parse(feed_url, request_headers={"User-Agent": "Hub4Apps Market Intel/1.0"})
        if feed.bozo and not feed.entries:
            print(f"  {source_name}: feed parse error — {feed.bozo_exception}", file=sys.stderr)
            dead_letter("news_agent", "feed_parse_failure", f"{source_name}: {feed.bozo_exception}")
            return []
        for entry in feed.entries:
            art_id = _article_id(entry, source_name)
            published = entry.get("published_parsed") or entry.get("updated_parsed")
            pub_at = (
                datetime(*published[:6], tzinfo=timezone.utc).isoformat()
                if published else datetime.now(timezone.utc).isoformat()
            )
            headline = entry.get("title") or ""
            summary  = entry.get("summary") or ""
            # Strip HTML tags from summary
            summary  = re.sub(r"<[^>]+>", " ", summary).strip()
            articles.append({
                "article_id":   art_id,
                "source":       source_name,
                "published_at": pub_at,
                "headline":     headline[:500],
                "summary":      summary[:1000],
                "url":          entry.get("link"),
            })
    except Exception as e:  # noqa: BLE001
        print(f"  {source_name}: exception — {e}", file=sys.stderr)
        dead_letter("news_agent", "feed_exception", f"{source_name}: {e}")
    return articles


def main() -> int:
    global _RULES_SOURCE
    run_id = job_run_start("news_agent")
    total_in     = 0
    total_events = 0

    # Load DB-backed keyword rules once per run; sets module-level caches.
    _RULES_SOURCE = load_rules()
    print(f"Loaded {len(_RULES_NAME_HITS)} ticker rules + "
          f"{len(_RULES_BULLISH)} bullish + {len(_RULES_BEARISH)} bearish "
          f"sentiment rules from {_RULES_SOURCE}")

    try:
        all_articles: list[dict] = []
        for source_name, feed_url in _FEEDS:
            batch = poll_feed(source_name, feed_url)
            print(f"  {source_name}: {len(batch)} articles fetched")
            all_articles.extend(batch)
            time.sleep(0.3)

        total_in = len(all_articles)
        if not all_articles:
            job_run_finish(run_id, "ok", 0, 0)
            return 0

        raw_ids = upsert_raw_news(all_articles)
        total_events = emit_news_events(all_articles, raw_ids)
        print(f"Fetched {total_in} articles, {total_events} events emitted (dupes ignored by DB)")
        job_run_finish(run_id, "ok", total_in, total_events)
        return 0

    except Exception as e:  # noqa: BLE001
        import traceback
        tb = traceback.format_exc()
        dead_letter("news_agent", "top_level_failure", tb)
        job_run_finish(run_id, "failed", total_in, 0, err=str(e))
        print(f"FATAL: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
