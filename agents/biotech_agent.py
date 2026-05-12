"""
Biotech agent — pure event-driven FDA + clinical catalysts.

Two data sources, both free, no API key:

1. clinicaltrials.gov API — query for Phase 3 trials with recent status changes
   on tickers in the biotech watchlists. Pulls completion/results-posted updates
   which often precede major moves.

2. FDA approval calendar — scrape FDA.gov pressannouncements RSS to detect
   PDUFA decisions on tickers we track (matched via sponsor name in releases).

Telegram triggers:
  - FDA approval / rejection on a watchlist ticker (severity 4)
  - Phase 3 readout posted (severity 4)
  - M&A target announcement (handled by news_agent + filing_agent; this agent
    only consumes their events for biotech-specific scoring)
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
    job_run_start, job_run_finish, dead_letter,
    SUPABASE_URL, HEADERS_SB,
)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

FDA_PRESS_RSS = "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml"
CTGOV_API     = "https://clinicaltrials.gov/api/v2/studies"

BIOTECH_ALIASES = {
    "LLY":  ["Eli Lilly", "Lilly"],
    "NVO":  ["Novo Nordisk"],
    "MRK":  ["Merck"],
    "PFE":  ["Pfizer"],
    "JNJ":  ["Johnson & Johnson", "Janssen"],
    "ABBV": ["AbbVie"],
    "AMGN": ["Amgen"],
    "VKTX": ["Viking Therapeutics"],
    "REGN": ["Regeneron"],
    "VRTX": ["Vertex Pharmaceuticals", "Vertex"],
    "ALNY": ["Alnylam"],
    "MRNA": ["Moderna"],
    "ISRG": ["Intuitive Surgical", "da Vinci"],
    "BSX":  ["Boston Scientific"],
    "MDT":  ["Medtronic"],
}

# Keywords that indicate a market-moving FDA announcement
FDA_APPROVAL_KW   = ["approves", "approval", "approved", "authorizes", "authorization"]
FDA_REJECTION_KW  = ["complete response letter", "crl", "rejects", "declines", "warning"]
FDA_PHASE3_KW     = ["phase 3", "pivotal", "topline results"]


# ============================================================
# FDA press releases
# ============================================================

def fetch_fda_releases() -> list[dict]:
    try:
        feed = feedparser.parse(FDA_PRESS_RSS, request_headers={
            "User-Agent": "nishant nishugupta@gmail.com",
        })
    except Exception as exc:  # noqa: BLE001
        print(f"  FDA RSS failed: {exc}", file=sys.stderr)
        return []
    return [{
        "title":     getattr(e, "title", ""),
        "summary":   getattr(e, "summary", ""),
        "link":      getattr(e, "link", ""),
        "published": getattr(e, "published", ""),
    } for e in feed.entries[:50]]


def classify_fda(text: str) -> str | None:
    """Returns 'approval', 'rejection', 'phase3', or None."""
    text_lower = text.lower()
    if any(k in text_lower for k in FDA_APPROVAL_KW):
        return "approval"
    if any(k in text_lower for k in FDA_REJECTION_KW):
        return "rejection"
    if any(k in text_lower for k in FDA_PHASE3_KW):
        return "phase3"
    return None


def match_tickers(text: str) -> list[str]:
    if not text:
        return []
    text_lower = text.lower()
    matched = []
    for ticker, aliases in BIOTECH_ALIASES.items():
        for alias in aliases:
            if alias.lower() in text_lower:
                matched.append(ticker)
                break
    return matched


# ============================================================
# clinicaltrials.gov Phase 3 readouts
# ============================================================

def fetch_recent_phase3_readouts(sponsor_name: str) -> list[dict]:
    """Query ctgov for Phase 3 trials sponsored by a given company with
    recent status updates (results posted, completed, terminated). Returns
    list of trial dicts."""
    try:
        r = requests.get(
            CTGOV_API,
            params={
                "query.term":  f"{sponsor_name} AND Phase 3",
                "filter.advanced": "AREA[LastUpdatePostDate]RANGE[2026-04-01,2026-12-31]",
                "fields":      "NCTId,BriefTitle,OverallStatus,LastUpdatePostDate,LeadSponsorName,Phase",
                "pageSize":    "10",
            },
            timeout=15,
            headers={"User-Agent": "nishant nishugupta@gmail.com"},
        )
        if r.status_code != 200:
            return []
        return r.json().get("studies", [])
    except Exception as exc:  # noqa: BLE001
        print(f"  ctgov {sponsor_name}: {exc}", file=sys.stderr)
        return []


# ============================================================
# Event + Telegram
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
        "source_table":      payload.get("source_table") or "biotech_agent",
        "parser_confidence": 0.8,
        "dedupe_key":        dedupe_key,
        "payload":           {k: v for k, v in payload.items()
                              if k not in ("dedupe_key", "event_at", "source_table")},
    }
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/stock_normalized_events?on_conflict=dedupe_key",
        headers={**HEADERS_SB, "Prefer": "resolution=ignore-duplicates,return=representation"},
        json=[row], timeout=15,
    )
    if r.status_code in (200, 201) and r.json():
        return r.json()[0]["id"]
    return None


def send_alert(ticker: str, kind: str, title: str, link: str) -> bool:
    if not BOT_TOKEN or not CHAT_ID:
        return False
    dedupe = f"biotech_{kind}_{ticker}_{datetime.now(timezone.utc).date()}"
    existing = requests.get(
        f"{SUPABASE_URL}/rest/v1/stock_signals?dedupe_key=eq.{dedupe}&select=id&limit=1",
        headers=HEADERS_SB, timeout=10,
    )
    if existing.status_code == 200 and existing.json():
        return False

    emoji = "💊" if kind == "approval" else "⛔" if kind == "rejection" else "🧪"
    direction = "bullish" if kind == "approval" else "bearish" if kind == "rejection" else "neutral"
    action    = "WATCH"   if direction == "bullish" else "AVOID_CHASE" if direction == "bearish" else "WATCH"

    text = (
        f"{emoji} <b>{ticker}</b> — FDA {kind}\n"
        f"<i>{title[:140]}</i>\n"
        f"{link}"
    )
    sig_row = {
        "ticker":           ticker,
        "fired_at":         datetime.now(timezone.utc).isoformat(),
        "direction":        direction,
        "confidence":       0.9,
        "horizon_days":     30,
        "thesis_summary":   text[:240],
        "model_version":    "biotech-v1.0",
        "weight_at_time":   {"agents": ["biotech"]},
        "status":           "open",
        "action":           action,
        "score":            90,
        "score_breakdown":  {"items": [{"rule": f"fda_{kind}", "points": 90, "detail": title[:80]}]},
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
    except Exception:
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
    run_id = job_run_start("biotech_agent")
    n_events = n_alerts = 0
    try:
        releases = fetch_fda_releases()
        print(f"FDA press releases: {len(releases)}")
        for r in releases:
            blob = f"{r['title']} {r['summary']}"
            kind = classify_fda(blob)
            if not kind:
                continue
            tickers = match_tickers(blob)
            if not tickers:
                continue
            severity = 4 if kind in ("approval","rejection") else 3
            for ticker in tickers:
                dedupe = f"fda_{kind}_{ticker}_{r.get('link') or hash(r['title'])}"
                payload = {
                    "title":           r["title"][:200],
                    "link":            r.get("link",""),
                    "published":       r.get("published",""),
                    "direction_prior": "long" if kind == "approval" else "short" if kind == "rejection" else "neutral",
                    "dedupe_key":      dedupe,
                    "event_at":        r.get("published") or datetime.now(timezone.utc).isoformat(),
                    "source_table":    "fda_press_rss",
                }
                if emit_event("fda_pdufa_decision", severity, payload, ticker,
                               event_subtype=kind):
                    n_events += 1
                    print(f"  FDA {kind} → {ticker}")
                if severity == 4:
                    if send_alert(ticker, kind, r["title"], r.get("link","")):
                        n_alerts += 1

        # Phase 3 trial readouts via ctgov — sample 3 GLP-1 sponsors per run
        # (limit ctgov hits to stay polite). Future: round-robin all sponsors.
        for sponsor_ticker, names in list(BIOTECH_ALIASES.items())[:5]:
            sponsor = names[0]
            studies = fetch_recent_phase3_readouts(sponsor)
            for s in studies:
                prot = s.get("protocolSection", {}) or {}
                nct = prot.get("identificationModule", {}).get("nctId") or ""
                title = prot.get("identificationModule", {}).get("briefTitle", "")[:120]
                status = prot.get("statusModule", {}).get("overallStatus", "")
                last_update = prot.get("statusModule", {}).get("lastUpdatePostDate", "")
                if status not in ("COMPLETED", "TERMINATED", "ACTIVE_NOT_RECRUITING"):
                    continue
                dedupe = f"phase3_{sponsor_ticker}_{nct}_{last_update}"
                payload = {
                    "nct_id":          nct,
                    "title":           title,
                    "status":          status,
                    "last_update":     last_update,
                    "sponsor":         sponsor,
                    "direction_prior": "neutral",
                    "dedupe_key":      dedupe,
                    "source_table":    "clinicaltrials_gov",
                }
                if emit_event("clinical_readout", 3, payload, sponsor_ticker,
                               event_subtype=status.lower()):
                    n_events += 1

        elapsed = time.time() - started
        print(f"DONE in {elapsed:.1f}s — events={n_events} alerts={n_alerts}")
        job_run_finish(run_id, "ok", len(releases), n_events)
        return 0
    except Exception as exc:  # noqa: BLE001
        import traceback
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        job_run_finish(run_id, "failed", 0, n_events, err=str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
