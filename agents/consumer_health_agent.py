"""
Consumer health agent — cycle sentinel.

Retail / restaurants / travel / discretionary names are the cycle predictor.
When AI capex narrative cools, consumer rotation is what investors check.

Data signals:

1. TSA daily passenger throughput — published daily at tsa.gov.
   Used as a real-time travel demand proxy (ABNB, BKNG, airlines).
   Compares latest day vs same-day-prior-year for cycle health.

2. Retail same-store-sales reports — picked up via filing_agent 8-K ingest
   when retailers report monthly comparable sales. This agent matches
   recent 8-K events on retail_big_box tickers and emits same_store_sales
   events with surprise direction.

3. Consumer sentiment proxy — UMICH (University of Michigan) consumer
   sentiment index from FRED series UMCSENT. Released monthly.

Cron: weekday 13:00 UTC.
Telegram: TSA throughput +/-15% YoY (cycle inflection); UMICH < 60 panic.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from filing_agent import (   # type: ignore
    job_run_start, job_run_finish, dead_letter,
    SUPABASE_URL, HEADERS_SB,
)

FRED_API_KEY = os.environ.get("FRED_API_KEY", "")
BOT_TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID      = os.environ.get("TELEGRAM_CHAT_ID", "")

CONSUMER_TICKERS = {
    "retail_big_box":  ["WMT", "COST", "HD", "LOW", "TGT"],
    "restaurants":     ["SBUX", "MCD", "CMG"],
    "travel_leisure":  ["ABNB", "BKNG"],
    "discretionary":   ["AMZN", "SHOP", "NKE"],
}
TRAVEL_TICKERS = set(CONSUMER_TICKERS["travel_leisure"])

# UMICH sentiment thresholds
UMICH_PANIC = 60.0     # historical recession-adjacent levels
UMICH_LOW   = 70.0     # below this is "stressed consumer"

# TSA YoY thresholds
TSA_HOT     =  0.05    # >+5% YoY = travel demand strong
TSA_COOL    = -0.05    # <-5% YoY = travel demand weak


# ============================================================
# FRED (UMICH)
# ============================================================

def fred_observations(series_id: str, limit: int = 4) -> list[dict]:
    if not FRED_API_KEY:
        return []
    try:
        r = requests.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={
                "series_id": series_id, "api_key": FRED_API_KEY,
                "file_type": "json", "sort_order": "desc", "limit": limit,
            },
            timeout=15,
        )
        if r.status_code != 200:
            return []
        return r.json().get("observations", [])
    except Exception as exc:  # noqa: BLE001
        print(f"  FRED {series_id} exc: {exc}", file=sys.stderr)
        return []


def _f(obs: dict) -> float | None:
    v = obs.get("value")
    if v in (None, "", "."):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ============================================================
# TSA throughput (scraped from public daily data table)
# ============================================================

def fetch_tsa_throughput() -> dict | None:
    """TSA publishes a public daily throughput page. Lightweight scraper
    targeting the data table. Returns {date, value, prior_year_value} or None
    on failure (TSA changes layout occasionally; we tolerate gracefully)."""
    try:
        r = requests.get(
            "https://www.tsa.gov/travel/passenger-volumes",
            headers={"User-Agent": "nishant nishugupta@gmail.com"},
            timeout=15,
        )
        if r.status_code != 200:
            return None
    except Exception as exc:  # noqa: BLE001
        print(f"  TSA fetch failed: {exc}", file=sys.stderr)
        return None
    # Minimal parse: TSA wraps data in <td> cells with dates + numeric counts.
    # Skip lxml dependency — quick regex extraction of the first data row.
    import re
    body = r.text
    # Pattern: <td>MM/DD/YYYY</td><td>...comma-separated number...</td>
    rows = re.findall(
        r'<td[^>]*>(\d{1,2}/\d{1,2}/\d{4})</td>\s*<td[^>]*>([\d,]+)</td>\s*<td[^>]*>([\d,]+)</td>',
        body,
    )
    if not rows:
        return None
    date_str, current, prior = rows[0]
    try:
        cur = int(current.replace(",", ""))
        py  = int(prior.replace(",", ""))
        return {"date": date_str, "current": cur, "prior_year": py,
                "yoy_pct": (cur - py) / py if py else 0.0}
    except (TypeError, ValueError):
        return None


# ============================================================
# Event + Telegram (same shape as sibling domain agents)
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
        "source_table":      payload.get("source_table") or "consumer_health_agent",
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


def send_alert(ticker: str, summary: str, dedupe_key: str, direction: str = "neutral",
                score: int = 75) -> bool:
    if not BOT_TOKEN or not CHAT_ID:
        return False
    existing = requests.get(
        f"{SUPABASE_URL}/rest/v1/stock_signals?dedupe_key=eq.{dedupe_key}&select=id&limit=1",
        headers=HEADERS_SB, timeout=10,
    )
    if existing.status_code == 200 and existing.json():
        return False
    action = "AVOID_CHASE" if direction == "bearish" else "WATCH"
    sig_row = {
        "ticker":           ticker,
        "fired_at":         datetime.now(timezone.utc).isoformat(),
        "direction":        direction if direction != "neutral" else "bullish",
        "confidence":       0.75,
        "horizon_days":     30,
        "thesis_summary":   summary[:240],
        "model_version":    "consumer-v1.0",
        "weight_at_time":   {"agents": ["consumer_health"]},
        "status":           "open",
        "action":           action,
        "score":            score,
        "score_breakdown":  {"items": [{"rule": "consumer_signal", "points": score, "detail": dedupe_key}]},
        "evidence_summary": summary[:240],
        "dedupe_key":       dedupe_key,
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
            data={"chat_id": CHAT_ID, "text": summary, "parse_mode": "HTML",
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
# Probe functions
# ============================================================

def check_umich() -> tuple[int, int]:
    obs = fred_observations("UMCSENT", limit=3)
    if len(obs) < 2:
        return 0, 0
    latest = _f(obs[0]); prior = _f(obs[1])
    if latest is None:
        return 0, 0
    obs_date = obs[0].get("date", "")
    delta = (latest - prior) if prior is not None else 0.0
    sev = 4 if latest < UMICH_PANIC else 3 if latest < UMICH_LOW else 2
    payload = {
        "value":           latest,
        "prior":           prior,
        "delta":           round(delta, 2),
        "direction_prior": "short" if latest < UMICH_LOW else "neutral",
        "dedupe_key":      f"consumer_umich_{obs_date}",
        "event_at":        obs_date + "T14:00:00+00:00",
        "source_table":    "fred_api",
    }
    n_events = 1 if emit_event("consumer_sentiment", sev, payload, "MACRO",
                                 event_subtype=f"umich_{int(latest)}") else 0
    n_alerts = 0
    if sev == 4:
        if send_alert(
            "MACRO",
            f"📉 <b>UMICH consumer sentiment: {latest:.1f}</b>\n"
            f"Prior month: {prior:.1f} (Δ {delta:+.1f})\n"
            f"<i>Recession-adjacent levels — discretionary cycle weak.</i>",
            dedupe_key=f"consumer_umich_alert_{obs_date}",
            direction="bearish", score=85,
        ):
            n_alerts = 1
    print(f"  UMICH={latest:.1f} (prior {prior}, sev={sev})")
    return n_events, n_alerts


def check_tsa() -> tuple[int, int]:
    snap = fetch_tsa_throughput()
    if not snap:
        print("  TSA throughput: no data")
        return 0, 0
    yoy = snap["yoy_pct"]
    sev = 3 if abs(yoy) >= TSA_HOT else 2
    direction = "long" if yoy >= TSA_HOT else "short" if yoy <= TSA_COOL else "neutral"
    n_events = n_alerts = 0
    for ticker in TRAVEL_TICKERS:
        payload = {
            "tsa_date":        snap["date"],
            "current":         snap["current"],
            "prior_year":      snap["prior_year"],
            "yoy_pct":         round(yoy, 4),
            "direction_prior": direction,
            "dedupe_key":      f"tsa_{ticker}_{snap['date']}",
            "event_at":        datetime.now(timezone.utc).isoformat(),
            "source_table":    "tsa_passenger_volumes",
        }
        if emit_event("traffic_data", sev, payload, ticker,
                       event_subtype="tsa_yoy"):
            n_events += 1
    if abs(yoy) >= 0.15:   # 15% YoY swing — alert cycle inflection
        if send_alert(
            list(TRAVEL_TICKERS)[0],
            f"✈️ <b>TSA throughput {yoy*100:+.1f}% YoY</b>\n"
            f"Today {snap['current']:,} vs prior-year {snap['prior_year']:,}\n"
            f"<i>{'Travel demand surge — bullish ABNB/BKNG' if yoy > 0 else 'Travel demand cratering — bearish consumer'}.</i>",
            dedupe_key=f"tsa_alert_{snap['date']}",
            direction="bullish" if yoy > 0 else "bearish",
            score=80,
        ):
            n_alerts = 1
    print(f"  TSA {snap['date']}: {snap['current']:,} vs PY {snap['prior_year']:,} ({yoy*100:+.1f}% YoY)")
    return n_events, n_alerts


# ============================================================
# Main
# ============================================================

def main() -> int:
    started = time.time()
    run_id = job_run_start("consumer_health_agent")
    total_events = total_alerts = 0
    try:
        for label, fn in (
            ("umich", check_umich),
            ("tsa",   check_tsa),
        ):
            try:
                e, a = fn()
                total_events += e
                total_alerts += a
                print(f"  [{label}] events={e} alerts={a}")
            except Exception as exc:
                import traceback
                tb = traceback.format_exc()
                print(f"  {label} failed: {exc}\n{tb}", file=sys.stderr)
                dead_letter("consumer_health_agent", None, label, "probe_failure", tb)

        elapsed = time.time() - started
        print(f"DONE in {elapsed:.1f}s — events={total_events} alerts={total_alerts}")
        job_run_finish(run_id, "ok", total_events + total_alerts, total_events)
        return 0
    except Exception as exc:  # noqa: BLE001
        import traceback
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        job_run_finish(run_id, "failed", 0, total_events, err=str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
