"""
Truth Social agent.

Polls the trumpstruth.org RSS mirror, dedupes by post id, classifies posts via
a deterministic keyword router (§7.9), and writes:
  - stock_raw_truth_posts (raw)
  - stock_normalized_events (one event per (post, affected_ticker) pair)

Run via .github/workflows/truth_social_agent.yml on */5 cron.
"""
from __future__ import annotations

import os
import re
import sys
import time
from datetime import datetime, timezone

import feedparser
import requests

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

# Public third-party RSS mirror. Override via env var if it changes.
FEED_URL = os.environ.get("TRUTH_SOCIAL_FEED", "https://trumpstruth.org/feed")

HEADERS_SB = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
    "Prefer":        "resolution=ignore-duplicates,return=minimal",
}

# ============================================================
# Classifier — deterministic keyword router (§7.9 of design doc)
# Returns list of (ticker, direction_prior, rule_label) tuples.
# direction_prior ∈ {"long","short"}
# ============================================================

# Pre-compile patterns once at import for speed
_RULES = [
    # (regex, [tickers], direction_prior, label)
    (re.compile(r"\btariff(s)?\b", re.I),                   ["XLI", "XLB", "XLY"],          "short", "tariff_general"),
    (re.compile(r"\bchina|xi\s+jinping|ccp\b", re.I),       ["AAPL", "NVDA", "TSLA", "FXI"], "short", "china"),
    (re.compile(r"\b(fed|powell|interest\s+rate)\b", re.I), ["TLT", "XLF"],                  "long",  "rates_dovish_or_hawkish"),
    (re.compile(r"\b(oil|drill(ing)?|opec)\b", re.I),       ["XLE", "XOM"],                  "long",  "oil"),
    (re.compile(r"\b(crypto|bitcoin|btc)\b", re.I),         ["COIN", "MSTR"],                "long",  "crypto"),
    (re.compile(r"\b(djt|truth\s+social)\b", re.I),         ["DJT"],                         "long",  "djt_self"),
]

# Explicit S&P 500 company-name → ticker map for direct mentions.
# Conservative: only the names a Trump post is plausibly going to use.
_COMPANY_MAP = {
    "apple":      "AAPL",
    "nvidia":     "NVDA",
    "microsoft":  "MSFT",
    "amazon":     "AMZN",
    "google":     "GOOGL",
    "alphabet":   "GOOGL",
    "meta":       "META",
    "facebook":   "META",
    "tesla":      "TSLA",
    "berkshire":  "BRK.B",
    "jpmorgan":   "JPM",
    "exxon":      "XOM",
    "walmart":    "WMT",
    "netflix":    "NFLX",
    "costco":     "COST",
    "visa":       "V",
    "mastercard": "MA",
    "amd":        "AMD",
    "coinbase":   "COIN",
}


def classify(text: str) -> list[dict]:
    """Return list of {ticker, direction_prior, rule_label} per matched rule."""
    if not text:
        return []
    hits: dict[str, dict] = {}
    text_low = text.lower()
    # Keyword rules
    for pattern, tickers, direction, label in _RULES:
        if pattern.search(text):
            for t in tickers:
                hits.setdefault(t, {"ticker": t, "direction_prior": direction, "rule_label": label})
    # Direct company name mentions (override direction = "neutral" until sentiment added)
    for name, ticker in _COMPANY_MAP.items():
        if name in text_low:
            # If a keyword rule also matched, keep it; otherwise add neutral entry.
            hits.setdefault(ticker, {"ticker": ticker, "direction_prior": "neutral", "rule_label": f"direct_mention_{name}"})
    return list(hits.values())


# ============================================================
# Operational logging (mirrors filing_agent.py — kept minimal here)
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


def dead_letter(agent: str, reason: str, detail: str, payload: dict | None = None) -> None:
    try:
        requests.post(
            f"{SUPABASE_URL}/rest/v1/stock_dead_letter_events",
            headers=HEADERS_SB,
            json={"agent": agent, "reason": reason, "detail": detail[:2000], "payload": payload or {}},
            timeout=10,
        )
    except Exception as e:  # noqa: BLE001
        print(f"  dead_letter failed: {e}", file=sys.stderr)


# ============================================================
# Ingestion
# ============================================================

def already_seen_post_ids(ids: list[str]) -> set[str]:
    if not ids:
        return set()
    in_list = ",".join(f'"{i}"' for i in ids)
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/stock_raw_truth_posts?post_id=in.({in_list})&select=post_id",
        headers=HEADERS_SB, timeout=15,
    )
    if r.status_code != 200:
        return set()
    return {row["post_id"] for row in r.json()}


def upsert_posts(posts: list[dict]) -> int:
    if not posts:
        return 0
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/stock_raw_truth_posts",
        headers=HEADERS_SB, json=posts, timeout=20,
    )
    if r.status_code not in (200, 201, 204):
        print(f"  posts insert {r.status_code}: {r.text}", file=sys.stderr)
        return 0
    return len(posts)


def emit_truth_events(posts: list[dict]) -> int:
    """One normalized event per (post, classified ticker)."""
    rows = []
    for p in posts:
        hits = classify(p["content"])
        if not hits:
            continue
        for h in hits:
            rows.append({
                "event_type":     "truth_social_post",
                "event_subtype":  h["rule_label"],
                "ticker":         h["ticker"],
                "event_at":       p["posted_at"],
                "severity":       2,                 # treat as medium by default
                "source_table":   "stock_raw_truth_posts",
                "parser_confidence": 0.7,            # rule-based, mid confidence
                "dedupe_key":     f"truth_{p['post_id']}_{h['ticker']}",
                "payload": {
                    "post_id":         p["post_id"],
                    "rule_label":      h["rule_label"],
                    "direction_prior": h["direction_prior"],
                    "post_excerpt":    p["content"][:200],
                    "url":             p.get("url"),
                },
            })
    if not rows:
        return 0
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/stock_normalized_events",
        headers=HEADERS_SB, json=rows, timeout=20,
    )
    if r.status_code not in (200, 201, 204):
        print(f"  events insert {r.status_code}: {r.text}", file=sys.stderr)
        return 0
    return len(rows)


def main() -> int:
    started = time.time()
    run_id = job_run_start("truth_social_agent")
    n_posts_in = 0
    n_posts_new = 0
    n_events = 0

    try:
        feed = feedparser.parse(FEED_URL, request_headers={"User-Agent": "Hub4Apps Market Intel/1.0"})
        if feed.bozo and not feed.entries:
            err = f"feed parse failed: {feed.bozo_exception}"
            dead_letter("truth_social_agent", "feed_parse_failure", err, {"url": FEED_URL})
            job_run_finish(run_id, "failed", 0, 0, err=err)
            print(err, file=sys.stderr)
            return 1

        n_posts_in = len(feed.entries)
        print(f"Feed: {n_posts_in} entries from {FEED_URL}")

        if n_posts_in == 0:
            job_run_finish(run_id, "ok", 0, 0)
            return 0

        # Normalize entries
        candidates = []
        for e in feed.entries:
            post_id = e.get("id") or e.get("guid") or e.get("link")
            if not post_id:
                continue
            posted = e.get("published_parsed") or e.get("updated_parsed")
            posted_at = (
                datetime(*posted[:6], tzinfo=timezone.utc).isoformat()
                if posted else datetime.now(timezone.utc).isoformat()
            )
            content = e.get("summary") or e.get("title") or ""
            candidates.append({
                "post_id":    str(post_id),
                "posted_at":  posted_at,
                "content":    content,
                "url":        e.get("link"),
                "source":     "trumpstruth_rss",
            })

        ids = [c["post_id"] for c in candidates]
        seen = already_seen_post_ids(ids)
        new_posts = [c for c in candidates if c["post_id"] not in seen]
        n_posts_new = upsert_posts(new_posts)
        n_events = emit_truth_events(new_posts)
        print(f"New posts: {n_posts_new}, classified events: {n_events}")
        job_run_finish(run_id, "ok", n_posts_in, n_posts_new + n_events)
        return 0

    except Exception as e:  # noqa: BLE001
        import traceback
        tb = traceback.format_exc()
        dead_letter("truth_social_agent", "top_level_failure", tb)
        job_run_finish(run_id, "failed", n_posts_in, 0, err=str(e))
        print(f"FATAL: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
