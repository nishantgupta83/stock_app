"""
Defense agent — geopolitical + government capex signal.

Ingests DoD contract awards from the public RSS feed at
https://www.defense.gov/Contracts/ and matches awards against the
defense watchlist. Emits a dod_contract_award event whenever a tracked
defense prime, drone maker, or cyber-defense ticker is mentioned.

Hot-path:
  Contract > $1B on a tracked ticker → immediate Telegram + event
  Contract $50M-$1B → event only (severity 3)
  Contract < $50M → event (severity 2)

Also detects defense bill / appropriations news through filing_agent's
8-K and news_agent's RSS pipeline (reuses existing event types).
"""
from __future__ import annotations

import os
import re
import sys
import time
from datetime import datetime, timezone

import feedparser
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from filing_agent import (   # type: ignore
    job_run_start, job_run_finish, SUPABASE_URL, HEADERS_SB,
)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

DOD_CONTRACTS_RSS = "https://www.defense.gov/DesktopModules/ArticleCS/RSS.ashx?ContentType=400&Site=945&max=100"

# Ticker → company aliases used in DoD contract text. Awards reference
# legal/operating company names, not exchange tickers.
DEFENSE_ALIASES = {
    "LMT":  ["Lockheed Martin", "Lockheed-Martin"],
    "RTX":  ["RTX Corp", "Raytheon", "Pratt & Whitney", "Collins Aerospace"],
    "NOC":  ["Northrop Grumman", "Northrop"],
    "GD":   ["General Dynamics", "Bath Iron Works", "Electric Boat", "GDIT"],
    "BA":   ["Boeing"],
    "HII":  ["Huntington Ingalls", "Newport News Shipbuilding", "Ingalls Shipbuilding"],
    "LHX":  ["L3Harris", "L3 Technologies", "Harris Corp"],
    "AVAV": ["AeroVironment"],
    "KTOS": ["Kratos"],
    "RKLB": ["Rocket Lab"],
    "PANW": ["Palo Alto Networks"],
    "CRWD": ["CrowdStrike"],
    "FTNT": ["Fortinet"],
    "NET":  ["Cloudflare"],
    "ZS":   ["Zscaler"],
}

CONTRACT_MEGA = 1_000_000_000   # $1B+ → severity 4 + Telegram
CONTRACT_BIG  = 50_000_000      # $50M+ → severity 3
CONTRACT_AMT_RE = re.compile(
    r"\$([0-9,]+(?:\.[0-9]+)?)\s*(million|billion|m|b)?",
    re.IGNORECASE,
)


# ============================================================
# DoD contracts ingest
# ============================================================

def fetch_dod_contracts() -> list[dict]:
    """Parse the DoD contracts RSS. Returns list of {title, summary, link, published}."""
    try:
        feed = feedparser.parse(DOD_CONTRACTS_RSS, request_headers={
            "User-Agent": "nishant nishugupta@gmail.com",
        })
    except Exception as exc:  # noqa: BLE001
        print(f"  DoD RSS fetch failed: {exc}", file=sys.stderr)
        return []
    return [{
        "title":     getattr(e, "title", ""),
        "summary":   getattr(e, "summary", ""),
        "link":      getattr(e, "link", ""),
        "published": getattr(e, "published", ""),
    } for e in feed.entries[:100]]


def parse_contract_amount(text: str) -> int:
    """Best-effort extract of dollar amount. Returns 0 on no match."""
    if not text:
        return 0
    m = CONTRACT_AMT_RE.search(text)
    if not m:
        return 0
    try:
        amt = float(m.group(1).replace(",", ""))
    except ValueError:
        return 0
    unit = (m.group(2) or "").lower()
    if unit in ("billion", "b"):
        return int(amt * 1_000_000_000)
    if unit in ("million", "m"):
        return int(amt * 1_000_000)
    return int(amt)


def match_tickers(text: str) -> list[str]:
    """Return tickers whose company name appears in the contract text."""
    if not text:
        return []
    text_lower = text.lower()
    matched: list[str] = []
    for ticker, aliases in DEFENSE_ALIASES.items():
        for alias in aliases:
            if alias.lower() in text_lower:
                matched.append(ticker)
                break
    return matched


# ============================================================
# Event emission + Telegram
# ============================================================

def emit_event(event_type: str, severity: int, payload: dict, ticker: str,
                event_subtype: str | None = None) -> int | None:
    when = datetime.now(timezone.utc).isoformat()
    dedupe_key = payload.get("dedupe_key") or f"{event_type}_{ticker}_{when[:10]}"
    row = {
        "ticker":            ticker,
        "event_type":        event_type,
        "event_subtype":     event_subtype,
        "event_at":          payload.get("event_at") or when,
        "severity":          severity,
        "source_table":      "dod_contracts_rss",
        "parser_confidence": 0.75,
        "dedupe_key":        dedupe_key,
        "payload":           {k: v for k, v in payload.items()
                              if k not in ("dedupe_key", "event_at")},
    }
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/stock_normalized_events?on_conflict=dedupe_key",
        headers={**HEADERS_SB, "Prefer": "resolution=ignore-duplicates,return=representation"},
        json=[row], timeout=15,
    )
    if r.status_code in (200, 201) and r.json():
        return r.json()[0]["id"]
    return None


def send_alert(ticker: str, amount: int, title: str, link: str) -> bool:
    """Telegram for $1B+ contracts on tracked tickers.

    Dedupe key uses the link (or title hash if link missing) — NOT the parsed
    amount, which can vary between RSS rewordings of the same award and
    silently double-fire alerts.
    """
    if not BOT_TOKEN or not CHAT_ID:
        return False
    stable_id = link or str(hash(title))
    dedupe = f"defense_contract_alert_{ticker}_{stable_id}"
    existing = requests.get(
        f"{SUPABASE_URL}/rest/v1/stock_signals?dedupe_key=eq.{dedupe}&select=id&limit=1",
        headers=HEADERS_SB, timeout=10,
    )
    if existing.status_code == 200 and existing.json():
        return False

    text = (
        f"🛡 <b>{ticker}</b> — DoD contract <b>${amount/1_000_000_000:.2f}B</b>\n"
        f"<i>{title[:120]}</i>\n"
        f"{link}"
    )
    sig_row = {
        "ticker":           ticker,
        "fired_at":         datetime.now(timezone.utc).isoformat(),
        "direction":        "bullish",
        "confidence":       0.8,
        "horizon_days":     30,
        "thesis_summary":   text[:240],
        "model_version":    "defense-v1.0",
        "weight_at_time":   {"agents": ["defense"]},
        "status":           "open",
        "action":           "WATCH",
        "score":            85,
        "score_breakdown":  {"items": [{"rule": "dod_contract_mega", "points": 85, "detail": f"${amount}"}]},
        "evidence_summary": text[:240],
        "dedupe_key":       dedupe,
        "status_v2":        "candidate",
    }
    sr = requests.post(
        f"{SUPABASE_URL}/rest/v1/stock_signals",
        headers={**HEADERS_SB, "Prefer": "resolution=ignore-duplicates,return=representation"},
        json=sig_row, timeout=15,
    )
    sig_id = sr.json()[0]["id"] if sr.status_code in (200,201) and sr.json() else None

    try:
        tr = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": "true"},
            timeout=15,
        )
        ok = tr.status_code == 200 and tr.json().get("ok", False)
    except Exception:  # noqa: BLE001
        ok = False
    if sig_id is not None:
        requests.patch(
            f"{SUPABASE_URL}/rest/v1/stock_signals?id=eq.{sig_id}",
            headers=HEADERS_SB,
            json={"status_v2": "sent" if ok else "dispatch_failed"},
            timeout=10,
        )
    return ok


# ============================================================
# Main
# ============================================================

def main() -> int:
    started = time.time()
    run_id = job_run_start("defense_agent")
    n_events = n_alerts = 0
    try:
        contracts = fetch_dod_contracts()
        print(f"DoD contracts fetched: {len(contracts)}")
        for c in contracts:
            blob = f"{c['title']} {c['summary']}"
            tickers = match_tickers(blob)
            if not tickers:
                continue
            amount = parse_contract_amount(blob)
            if amount < CONTRACT_BIG:
                # Skip tiny awards to avoid noise; could be relaxed later
                continue
            for ticker in tickers:
                severity = 4 if amount >= CONTRACT_MEGA else 3
                dedupe = f"dod_award_{ticker}_{c.get('link') or hash(c['title'])}"
                payload = {
                    "amount":         amount,
                    "title":          c["title"][:200],
                    "link":           c.get("link",""),
                    "published":      c.get("published",""),
                    "direction_prior":"long",
                    "dedupe_key":     dedupe,
                    "event_at":       c.get("published") or datetime.now(timezone.utc).isoformat(),
                }
                if emit_event("dod_contract_award", severity, payload, ticker,
                               event_subtype="mega" if severity == 4 else "large"):
                    n_events += 1
                    print(f"  {ticker}: ${amount/1e9:.2f}B award (sev={severity})")
                if amount >= CONTRACT_MEGA:
                    if send_alert(ticker, amount, c["title"], c.get("link","")):
                        n_alerts += 1

        elapsed = time.time() - started
        print(f"DONE in {elapsed:.1f}s — events={n_events} alerts={n_alerts}")
        job_run_finish(run_id, "ok", len(contracts), n_events)
        return 0
    except Exception as exc:  # noqa: BLE001
        import traceback
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        job_run_finish(run_id, "failed", 0, n_events, err=str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
