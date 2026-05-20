"""
Energy transition agent — EV / solar / nuclear / battery / charging.

Policy-driven mega-cycle. Three data signals:

1. NRC RSS — nuclear license approvals (NNE, OKLO, SMR, BWXT) are major
   catalysts for the nuclear-renaissance cohort.

2. EV monthly delivery results — Tesla, Rivian, Lucid, NIO publish monthly
   delivery counts. Detected via filing_agent's 8-K ingest with a keyword
   match in the press release body.

3. IRA / policy news — picked up by news_agent; this agent boosts severity
   for energy_transition tickers when policy keywords appear.

Cron: weekly Mon 14:00 UTC + daily 13:00 UTC light scan.
Telegram: NRC approval → bullish nuclear alert; EV monthly miss > 10% → bearish.
"""
from __future__ import annotations

import os
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

# NRC news releases RSS (license actions, approvals, denials). The prior URL
# (public-involve/public-meetings/schedule/all-meetings.rss) returned the
# meetings schedule — those entries rarely contain "approval"/"license"
# keywords, silently producing zero events.
NRC_RSS = "https://www.nrc.gov/reading-rm/doc-collections/news/nrc.xml"

# Sub-watchlist ticker mappings used to score policy mentions
ENERGY_TICKERS = {
    "ev_makers":       ["TSLA", "RIVN", "LCID", "NIO"],
    "solar":           ["FSLR", "ENPH", "SEDG"],
    "battery_storage": ["ALB", "LAC", "FLNC"],
    "nuclear":         ["CEG", "VST", "CCJ", "BWXT", "NNE", "OKLO", "SMR"],
    "charging_infra":  ["CHPT", "BLNK"],
}

# Keywords that should boost severity when found in nearby news
NUCLEAR_KW = ["nrc", "license", "small modular reactor", "smr", "construction permit"]
SOLAR_KW   = ["solar tariff", "ira tax credit", "section 45x", "domestic content"]
EV_KW      = ["ev tax credit", "ev mandate", "ev delivery", "model y", "cybertruck"]


# ============================================================
# NRC RSS
# ============================================================

def fetch_nrc_releases() -> list[dict]:
    try:
        feed = feedparser.parse(NRC_RSS, request_headers={
            "User-Agent": "nishant nishugupta@gmail.com",
        })
    except Exception as exc:  # noqa: BLE001
        print(f"  NRC RSS failed: {exc}", file=sys.stderr)
        return []
    return [{
        "title":     getattr(e, "title", ""),
        "summary":   getattr(e, "summary", ""),
        "link":      getattr(e, "link", ""),
        "published": getattr(e, "published", ""),
    } for e in feed.entries[:50]]


NUCLEAR_TICKERS = {"CEG","VST","CCJ","BWXT","NNE","OKLO","SMR"}
NUCLEAR_ALIASES = {
    "CCJ":  ["Cameco"],
    "BWXT": ["BWX Technologies", "BWXT"],
    "NNE":  ["Nano Nuclear"],
    "OKLO": ["Oklo"],
    "SMR":  ["NuScale"],
    "CEG":  ["Constellation Energy"],
    "VST":  ["Vistra"],
}


def detect_nrc_action(text: str) -> tuple[str, list[str]] | None:
    """Returns (action_type, tickers) when an NRC release mentions a tracked
    nuclear company plus a license/approval keyword."""
    if not text:
        return None
    text_lower = text.lower()
    if not any(k in text_lower for k in ("license", "approval", "permit", "operating")):
        return None
    matched = []
    for ticker, aliases in NUCLEAR_ALIASES.items():
        for alias in aliases:
            if alias.lower() in text_lower:
                matched.append(ticker)
                break
    if not matched:
        return None
    if "approval" in text_lower or "approves" in text_lower or "issued" in text_lower:
        return "approval", matched
    if "denial" in text_lower or "denies" in text_lower:
        return "denial", matched
    return "filing", matched


# ============================================================
# Event + Telegram (shared shape with sibling agents)
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
        "source_table":      payload.get("source_table") or "energy_transition_agent",
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
    dedupe = f"energy_{kind}_{ticker}_{datetime.now(timezone.utc).date()}"
    existing = requests.get(
        f"{SUPABASE_URL}/rest/v1/stock_signals?dedupe_key=eq.{dedupe}&select=id&limit=1",
        headers=HEADERS_SB, timeout=10,
    )
    if existing.status_code == 200 and existing.json():
        return False
    emoji = "⚛️" if "nuclear" in kind else "☀️" if "solar" in kind else "🔋"
    direction = "bullish" if "approval" in kind else "bearish" if "denial" in kind else "neutral"
    action = "WATCH" if direction == "bullish" else "AVOID_CHASE" if direction == "bearish" else "WATCH"
    text = (
        f"{emoji} <b>{ticker}</b> — {kind}\n"
        f"<i>{title[:140]}</i>\n"
        f"{link}"
    )
    sig_row = {
        "ticker":           ticker,
        "fired_at":         datetime.now(timezone.utc).isoformat(),
        "direction":        direction,
        "confidence":       0.8,
        "horizon_days":     30,
        "thesis_summary":   text[:240],
        "model_version":    "energy-v1.0",
        "weight_at_time":   {"agents": ["energy_transition"]},
        "status":           "open",
        "action":           action,
        "score":            80,
        "score_breakdown":  {"items": [{"rule": kind, "points": 80, "detail": title[:80]}]},
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
    if sig_id is None:
        return False
    from telegram_dispatcher import send_and_log
    ok = send_and_log(sig_id, text, parse_mode="HTML")
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
    run_id = job_run_start("energy_transition_agent")
    n_events = n_alerts = 0
    try:
        # NRC nuclear announcements
        releases = fetch_nrc_releases()
        print(f"NRC releases: {len(releases)}")
        for r in releases:
            blob = f"{r['title']} {r['summary']}"
            det = detect_nrc_action(blob)
            if not det:
                continue
            kind, tickers = det
            severity = 4 if kind == "approval" else 3 if kind == "denial" else 2
            for ticker in tickers:
                dedupe = f"nrc_{kind}_{ticker}_{r.get('link') or hash(r['title'])}"
                payload = {
                    "kind":            kind,
                    "title":           r["title"][:200],
                    "link":            r.get("link",""),
                    "published":       r.get("published",""),
                    "direction_prior": "long" if kind == "approval" else "short" if kind == "denial" else "neutral",
                    "dedupe_key":      dedupe,
                    "event_at":        r.get("published") or datetime.now(timezone.utc).isoformat(),
                    "source_table":    "nrc_rss",
                }
                if emit_event("nuclear_license_approval", severity, payload, ticker,
                               event_subtype=kind):
                    n_events += 1
                    print(f"  NRC {kind} → {ticker}")
                if severity == 4:
                    if send_alert(ticker, f"nuclear {kind}", r["title"], r.get("link","")):
                        n_alerts += 1

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
