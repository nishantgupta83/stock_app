"""
News agent.

Polls free RSS feeds (CNBC, MarketWatch, AP Business), dedupes by article id,
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


def _article_id(entry: dict, source: str) -> str:
    raw = entry.get("id") or entry.get("guid") or entry.get("link") or ""
    return hashlib.sha1(f"{source}:{raw}".encode()).hexdigest()[:24]


def classify(text: str) -> list[dict]:
    """Return list of {ticker, direction_prior, sentiment_label} for tickers mentioned."""
    if not text:
        return []
    text_low = text.lower()
    hits: dict[str, dict] = {}

    # Direct ticker symbol match (e.g. "NVDA", "BRK.B")
    for ticker in _WATCHLIST_TICKERS:
        pattern = re.compile(r"\b" + re.escape(ticker) + r"\b", re.I)
        if pattern.search(text):
            hits.setdefault(ticker, {"ticker": ticker})

    # Company name match
    for name, ticker in _COMPANY_MAP.items():
        if name in text_low:
            hits.setdefault(ticker, {"ticker": ticker})

    if not hits:
        return []

    # Determine sentiment once for the article, apply to all matched tickers
    bullish = bool(_BULLISH_RE.search(text))
    bearish = bool(_BEARISH_RE.search(text))
    if bullish and not bearish:
        direction, label = "long",    "positive"
    elif bearish and not bullish:
        direction, label = "short",   "negative"
    else:
        direction, label = "neutral", "neutral"

    for t in hits:
        hits[t]["direction_prior"]  = direction
        hits[t]["sentiment_label"]  = label

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


def emit_news_events(articles: list[dict]) -> int:
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
                "parser_confidence": 0.55,
                "dedupe_key":        f"news_{a['article_id']}_{h['ticker']}",
                "payload": {
                    "article_id":      a["article_id"],
                    "headline":        a["headline"][:200],
                    "url":             a.get("url"),
                    "source":          a["source"],
                    "direction_prior": h["direction_prior"],
                },
            })
    if not rows:
        return 0
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/stock_normalized_events?on_conflict=dedupe_key",
        headers=HEADERS_SB, json=rows, timeout=20,
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
    return articles


def main() -> int:
    run_id = job_run_start("news_agent")
    total_in     = 0
    total_events = 0

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

        # DB handles dedup via resolution=ignore-duplicates on dedupe_key unique index
        total_events = emit_news_events(all_articles)
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
