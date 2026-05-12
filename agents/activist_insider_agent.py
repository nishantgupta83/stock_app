"""
Activist & insider agent — highest signal-to-noise alpha.

Reuses filing_agent's existing 13D and Form 4 ingest (no new data source).
Two detection paths:

1. Activist 13D — when one of TRACKED_ACTIVISTS files an SC 13D, fire an
   alert immediately. Empirical 10-30% rally over 60 days on the underlying.

2. Insider cluster buys — when 3+ Form 4 BUY transactions hit the same
   ticker within 7 days from different filers (typically multiple officers
   buying in concert), fire an alert. Cleanest fundamental signal in equities.

Outputs:
  - activist_initial_position events (severity 4)
  - insider_cluster_buy events (severity 3)
  - Telegram immediately on either type
"""
from __future__ import annotations

import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from filing_agent import (   # type: ignore
    job_run_start, job_run_finish, dead_letter,
    SUPABASE_URL, HEADERS_SB,
)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

# Activists tracked. Names match filer/issuer text in 13D filings.
# Aliases capture variations (Pershing Square Capital Management, etc.)
TRACKED_ACTIVISTS = {
    "PSHSQR":   ["Pershing Square", "Ackman"],
    "ICAHN":    ["Icahn Capital", "Carl Icahn", "Icahn Enterprises"],
    "ELLIOTT":  ["Elliott Management", "Elliott Investment", "Paul Singer"],
    "VALUEACT": ["ValueAct Capital"],
    "TRIAN":    ["Trian Fund", "Nelson Peltz"],
    "STARBOARD":["Starboard Value", "Jeff Smith"],
    "THIRDPT":  ["Third Point", "Daniel Loeb"],
    "SCION":    ["Scion Asset", "Michael Burry"],
    "BERKSHIRE":["Berkshire Hathaway", "Warren Buffett"],
    "BRIDGEW":  ["Bridgewater Associates", "Ray Dalio"],
}

INSIDER_CLUSTER_MIN_FILERS = 3      # need 3+ distinct insiders
INSIDER_CLUSTER_WINDOW_DAYS = 7

LOOKBACK_HOURS = 24


# ============================================================
# Activist detection — 13D filings text-match
# ============================================================

def fetch_recent_13d_filings(hours: int) -> list[dict]:
    """13D events created in last N hours from existing filing_agent ingest."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/stock_normalized_events",
        headers=HEADERS_SB,
        params=[
            ("event_type", "in.(filing_13d,filing_13d/a)"),
            ("created_at", f"gte.{cutoff}"),
            ("select",     "id,ticker,event_at,severity,payload,event_subtype"),
            ("order",      "event_at.desc"),
            ("limit",      "200"),
        ],
        timeout=15,
    )
    return r.json() if r.status_code == 200 else []


def match_activist(filing: dict) -> tuple[str, str] | None:
    """Returns (activist_key, alias_matched) if the filing text mentions a tracked activist."""
    payload = filing.get("payload") or {}
    haystack = " ".join(str(payload.get(k) or "") for k in
                        ("primary_doc_desc","filer_name","filer","reporting_owner","title"))
    if not haystack:
        return None
    haystack_lower = haystack.lower()
    for activist_key, aliases in TRACKED_ACTIVISTS.items():
        for alias in aliases:
            if alias.lower() in haystack_lower:
                return activist_key, alias
    return None


# ============================================================
# Insider cluster — Form 4 within 7 days same ticker
# ============================================================

def fetch_recent_form4(days: int) -> list[dict]:
    """Form 4 events in last N days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/stock_normalized_events",
        headers=HEADERS_SB,
        params=[
            ("event_type", "eq.filing_4"),
            ("event_at",   f"gte.{cutoff}"),
            ("select",     "id,ticker,event_at,severity,payload"),
            ("order",      "event_at.desc"),
            ("limit",      "500"),
        ],
        timeout=15,
    )
    return r.json() if r.status_code == 200 else []


def detect_clusters(form4s: list[dict]) -> list[dict]:
    """{ticker, filer_count, filings, filers} where filer_count >=
    INSIDER_CLUSTER_MIN_FILERS within INSIDER_CLUSTER_WINDOW_DAYS.

    Form 4 transaction_code: P=purchase, S=sale, A=grant/award, M=option
    exercise. We only count P/A (insider buying or being granted shares —
    bullish signal); sells flip the signal direction so they must NOT
    increment the buy-cluster counter. Filings without a transaction_code
    in payload are dropped (cannot determine direction)."""
    by_ticker: dict[str, list[dict]] = defaultdict(list)
    for f in form4s:
        p = f.get("payload") or {}
        code = (p.get("transaction_code") or "").upper()
        # Only purchases + grants. Treat unknown/missing transaction_code as
        # ambiguous and skip — better to under-fire than to flip a sell into
        # a bullish alert.
        if code not in ("P", "A"):
            continue
        if f.get("ticker"):
            by_ticker[f["ticker"]].append(f)

    clusters: list[dict] = []
    for ticker, filings in by_ticker.items():
        filers = set()
        for f in filings:
            p = f.get("payload") or {}
            owner = p.get("reporting_owner") or p.get("filer") or p.get("filer_name")
            if owner:
                filers.add(owner)
        if len(filers) >= INSIDER_CLUSTER_MIN_FILERS:
            clusters.append({
                "ticker":      ticker,
                "filer_count": len(filers),
                "filings":     filings,
                "filers":      sorted(filers)[:10],
            })
    return clusters


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
        "event_at":          when,
        "severity":          severity,
        "source_table":      "filing_agent_derived",
        "parser_confidence": 0.85,
        "dedupe_key":        dedupe_key,
        "payload":           {k: v for k, v in payload.items() if k != "dedupe_key"},
    }
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/stock_normalized_events?on_conflict=dedupe_key",
        headers={**HEADERS_SB, "Prefer": "resolution=ignore-duplicates,return=representation"},
        json=[row], timeout=15,
    )
    if r.status_code in (200, 201) and r.json():
        return r.json()[0]["id"]
    if r.status_code in (200, 201, 204):
        return None  # dup
    print(f"  emit {event_type} {r.status_code}: {r.text[:200]}", file=sys.stderr)
    return None


def send_alert(text: str, dedupe_key: str, ticker: str, score: int = 75) -> bool:
    if not BOT_TOKEN or not CHAT_ID:
        return False
    existing = requests.get(
        f"{SUPABASE_URL}/rest/v1/stock_signals?dedupe_key=eq.{dedupe_key}&select=id&limit=1",
        headers=HEADERS_SB, timeout=10,
    )
    if existing.status_code == 200 and existing.json():
        return False

    sig_row = {
        "ticker":           ticker,
        "fired_at":         datetime.now(timezone.utc).isoformat(),
        "direction":        "bullish",
        "confidence":       0.85,
        "horizon_days":     30,
        "thesis_summary":   text[:240],
        "model_version":    "activist-v1.0",
        "weight_at_time":   {"agents": ["activist_insider"]},
        "status":           "open",
        "action":           "WATCH",
        "score":            score,
        "score_breakdown":  {"items": [{"rule": "activist_alert", "points": score, "detail": dedupe_key}]},
        "evidence_summary": text[:240],
        "dedupe_key":       dedupe_key,
        "status_v2":        "candidate",
    }
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/stock_signals",
        headers={**HEADERS_SB, "Prefer": "resolution=ignore-duplicates,return=representation"},
        json=sig_row, timeout=15,
    )
    sig_id = r.json()[0]["id"] if r.status_code in (200,201) and r.json() else None

    try:
        tr = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": "true"},
            timeout=15,
        )
        ok = tr.status_code == 200 and tr.json().get("ok", False)
    except Exception as exc:  # noqa: BLE001
        print(f"  Telegram send: {exc}", file=sys.stderr)
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
    run_id = job_run_start("activist_insider_agent")
    n_events = n_alerts = 0
    try:
        # Activist 13Ds
        filings = fetch_recent_13d_filings(LOOKBACK_HOURS)
        print(f"Recent 13D filings: {len(filings)}")
        for f in filings:
            match = match_activist(f)
            if not match:
                continue
            activist_key, alias = match
            ticker = f.get("ticker", "")
            if not ticker:
                continue
            dedupe = f"activist_{activist_key}_{ticker}_{(f.get('event_at') or '')[:10]}"
            payload = {
                "activist":       activist_key,
                "alias_matched":  alias,
                "filing_id":      f.get("id"),
                "filing_at":      f.get("event_at"),
                "direction_prior":"long",
                "dedupe_key":     dedupe,
            }
            if emit_event("activist_initial_position", 4, payload, ticker,
                           event_subtype=activist_key):
                n_events += 1
            if send_alert(
                f"🎯 <b>{activist_key} → {ticker}</b>\n"
                f"New SC 13D filing detected — {alias} took an initial position.\n"
                f"<i>Historical: 10-30% rally over 60 days on activist 13D initial filings.</i>",
                dedupe_key=dedupe + "_alert", ticker=ticker, score=90,
            ):
                n_alerts += 1

        # Insider clusters
        form4s = fetch_recent_form4(INSIDER_CLUSTER_WINDOW_DAYS)
        print(f"Recent Form 4 filings (last {INSIDER_CLUSTER_WINDOW_DAYS}d): {len(form4s)}")
        clusters = detect_clusters(form4s)
        for c in clusters:
            ticker = c["ticker"]
            today = datetime.now(timezone.utc).date().isoformat()
            dedupe = f"insider_cluster_{ticker}_{today}"
            payload = {
                "filer_count":    c["filer_count"],
                "filers":         c["filers"],
                "window_days":    INSIDER_CLUSTER_WINDOW_DAYS,
                "direction_prior":"long",
                "dedupe_key":     dedupe,
            }
            if emit_event("insider_cluster_buy", 3, payload, ticker):
                n_events += 1
            if send_alert(
                f"📊 <b>Insider cluster — {ticker}</b>\n"
                f"<b>{c['filer_count']}</b> insiders filed Form 4 within {INSIDER_CLUSTER_WINDOW_DAYS} days.\n"
                f"<i>Clean fundamental signal — track for follow-through.</i>",
                dedupe_key=dedupe + "_alert", ticker=ticker, score=70,
            ):
                n_alerts += 1

        elapsed = time.time() - started
        print(f"DONE in {elapsed:.1f}s — events={n_events} alerts={n_alerts}")
        job_run_finish(run_id, "ok", len(filings) + len(form4s), n_events)
        return 0
    except Exception as exc:  # noqa: BLE001
        import traceback
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        job_run_finish(run_id, "failed", 0, n_events, err=str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
