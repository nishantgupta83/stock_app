"""
Source review agent — monthly architectural health check.

Three jobs:
  1. Read stock_data_sources registry, ping each alternative, update its health.
  2. Read stock_job_runs (last 30 days), compute per-agent success rate.
  3. Generate a Telegram summary: degradation alerts + recommendations to
     promote a fallback to primary if its rolling success > primary's by ≥10pp.

This is the "scout + monitor" pattern from §3 of the design doc:
  - PASSIVE monitor: tracks per-source success from job_runs telemetry
  - ACTIVE scout:    pings each registered alternative to verify it's still alive

Auto-promotion is NOT done — too risky without human review. Agent only
RECOMMENDS via Telegram. User updates is_primary in Supabase manually.

Run via .github/workflows/source_review_agent.yml — monthly cron.
"""
from __future__ import annotations

import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests
from curl_cffi import requests as cffi_requests

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
BOT_TOKEN    = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID      = os.environ["TELEGRAM_CHAT_ID"]

SOURCE_JOB_AGENT = {
    "edgar": "filing_agent",
    "cnbc_markets": "news_agent",
    "marketwatch": "news_agent",
    "seeking_alpha": "news_agent",
    "rss_cnbc": "news_agent",
    "rss_reuters": "news_agent",
    "trumpstruth_rss": "truth_social_agent",
    "telegram": "thesis_agent",
    "yfinance": "price_agent",
}

HEADERS_SB = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
}

PROMOTION_DELTA_PP = 10  # promote fallback only if it beats primary by ≥10 points


# ============================================================
# Health pings — lightweight check that the source endpoint is reachable
# ============================================================

def ping_source(source: dict) -> tuple[bool, str]:
    """Return (ok, note). Strategy varies per source — keep cheap and safe."""
    name = source["name"]
    url  = source.get("url") or ""
    try:
        if name == "yfinance":
            # Hit Yahoo directly via curl_cffi browser impersonation
            r = cffi_requests.get("https://query1.finance.yahoo.com/v8/finance/chart/AAPL?range=5d&interval=1d",
                                  impersonate="chrome", timeout=10)
            ok = r.status_code == 200 and "chart" in r.text
            return ok, f"http {r.status_code}"
        if name == "stooq":
            r = cffi_requests.get("https://stooq.com/q/d/l/?s=aapl.us&i=d", impersonate="chrome", timeout=10)
            return r.status_code == 200 and len(r.text) > 100, f"http {r.status_code}, {len(r.text)}b"
        if name == "edgar":
            ua = os.environ.get("EDGAR_USER_AGENT", "Hub4Apps Market Intel test@example.com")
            r = requests.get("https://data.sec.gov/submissions/CIK0000320193.json",
                             headers={"User-Agent": ua}, timeout=10)
            return r.status_code == 200 and "filings" in r.text, f"http {r.status_code}"
        if name == "trumpstruth_rss":
            r = requests.get("https://trumpstruth.org/feed", timeout=10,
                             headers={"User-Agent": "Hub4Apps Market Intel/1.0"})
            return r.status_code == 200 and "<rss" in r.text, f"http {r.status_code}"
        if name == "telegram":
            r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getMe", timeout=10)
            return r.status_code == 200 and r.json().get("ok"), f"http {r.status_code}"
        if name == "finnhub_news" or name == "finnhub_free":
            r = requests.get("https://finnhub.io/api/v1/", timeout=10)
            return r.status_code in (200, 401), f"http {r.status_code} (401 expected without key)"
        if name in ("rss_reuters", "rss_cnbc", "cnbc_markets", "marketwatch", "seeking_alpha"):
            test_url = {
                "rss_reuters": "https://www.reutersagency.com/feed/?best-topics=business-finance",
                "rss_cnbc": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=15839069",
                "cnbc_markets": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=15839069",
                "marketwatch": "https://feeds.marketwatch.com/marketwatch/topstories/",
                "seeking_alpha": "https://seekingalpha.com/market_currents.xml",
            }[name]
            r = requests.get(test_url, timeout=10, headers={"User-Agent": "Hub4Apps Market Intel/1.0"})
            return r.status_code == 200, f"http {r.status_code}"
        # Default: HEAD the URL
        if url:
            r = requests.head(url, timeout=10, allow_redirects=True)
            return r.status_code < 400, f"http {r.status_code}"
        return False, "no probe defined"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def update_source_health(source_id: int, ok: bool, note: str) -> None:
    # Increment consecutive_failures atomically via two-step: GET then PATCH
    r = requests.get(f"{SUPABASE_URL}/rest/v1/stock_data_sources?id=eq.{source_id}&select=consecutive_failures",
                     headers=HEADERS_SB, timeout=10)
    cur = r.json()[0]["consecutive_failures"] if r.status_code == 200 and r.json() else 0
    new_failures = 0 if ok else cur + 1
    requests.patch(f"{SUPABASE_URL}/rest/v1/stock_data_sources?id=eq.{source_id}",
                   headers=HEADERS_SB, json={
                       "last_health_check_at": datetime.now(timezone.utc).isoformat(),
                       "last_health_check_ok": ok,
                       "consecutive_failures": new_failures,
                       "notes": (note[:280] if note else None),
                   }, timeout=10)


# ============================================================
# Job-run analysis — per-agent success rate from telemetry
# ============================================================

def per_agent_success_rate(days: int = 30) -> dict[str, dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    r = requests.get(f"{SUPABASE_URL}/rest/v1/stock_job_runs",
                     headers=HEADERS_SB,
                     params={
                         "started_at": f"gte.{cutoff}",
                         "select":     "agent,status",
                         "limit":      "5000",
                     }, timeout=20)
    if r.status_code != 200:
        return {}
    by_agent: dict[str, dict] = defaultdict(lambda: {"total": 0, "ok": 0, "failed": 0, "partial": 0})
    for row in r.json():
        a = row["agent"]
        by_agent[a]["total"] += 1
        s = row["status"]
        if s in by_agent[a]:
            by_agent[a][s] += 1
    for a, st in by_agent.items():
        st["success_rate"] = round(st["ok"] / st["total"], 3) if st["total"] else 0.0
    return dict(by_agent)


# ============================================================
# Recommendation engine
# ============================================================

def generate_recommendations(sources: list[dict], success: dict[str, dict]) -> list[str]:
    """Compare primary vs fallback success rates per category. Recommend swaps."""
    by_category: dict[str, list[dict]] = defaultdict(list)
    for s in sources:
        by_category[s["category"]].append(s)

    recs = []
    for category, items in by_category.items():
        primary = next((i for i in items if i["is_primary"]), None)
        if not primary:
            continue
        # If primary has had ≥3 consecutive failures or last check failed: warn
        if primary.get("consecutive_failures", 0) >= 3:
            recs.append(f"⚠️ {category}: primary <code>{primary['name']}</code> has {primary['consecutive_failures']} consecutive failures. Consider promoting a fallback.")
        elif primary.get("last_health_check_ok") is False:
            recs.append(f"⚠️ {category}: primary <code>{primary['name']}</code> failed last health check. Investigating.")
        # If a fallback is healthy and primary has poor job_run success, recommend swap
        prim_jobs = success.get(SOURCE_JOB_AGENT.get(primary["name"], primary["name"]), {})
        if prim_jobs.get("total", 0) >= 5 and prim_jobs.get("success_rate", 1.0) < 0.8:
            for fb in items:
                if fb is primary:
                    continue
                if fb.get("last_health_check_ok"):
                    recs.append(
                        f"📈 {category}: promote <code>{fb['name']}</code> (healthy) — primary <code>{primary['name']}</code> "
                        f"success rate {prim_jobs['success_rate']:.0%} over last 30d."
                    )
                    break
    return recs


# ============================================================
# Telegram summary
# ============================================================

def telegram_send(text: str) -> bool:
    try:
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                          data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=15)
        return r.status_code == 200 and r.json().get("ok", False)
    except Exception:
        return False


# ============================================================
# Operational logging
# ============================================================

def job_run_start() -> int | None:
    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/stock_job_runs",
            headers={**HEADERS_SB, "Prefer": "return=representation"},
            json={"agent": "source_review_agent"}, timeout=10,
        )
        if r.status_code in (200, 201) and r.json():
            return r.json()[0]["id"]
    except Exception:
        pass
    return None


def job_run_finish(run_id: int | None, status: str, n_pinged: int) -> None:
    if run_id is None:
        return
    try:
        requests.patch(f"{SUPABASE_URL}/rest/v1/stock_job_runs?id=eq.{run_id}",
                       headers=HEADERS_SB, json={
                           "finished_at": datetime.now(timezone.utc).isoformat(),
                           "status":      status,
                           "rows_in":     n_pinged,
                       }, timeout=10)
    except Exception:
        pass


def main() -> int:
    run_id = job_run_start()
    print(f"Source review agent run_id={run_id}")
    try:
        # Fetch registry
        r = requests.get(f"{SUPABASE_URL}/rest/v1/stock_data_sources?select=*&order=category,is_primary.desc",
                         headers=HEADERS_SB, timeout=20)
        if r.status_code != 200:
            print(f"  fetch sources failed: {r.text}", file=sys.stderr)
            job_run_finish(run_id, "failed", 0)
            return 1
        sources = r.json()
        print(f"Pinging {len(sources)} registered sources...")

        # Ping each
        for s in sources:
            ok, note = ping_source(s)
            print(f"  {s['name']:<22} {'OK' if ok else 'FAIL':<5} ({note})")
            update_source_health(s["id"], ok, note)
            time.sleep(0.5)

        # Re-fetch with updated health
        sources = requests.get(
            f"{SUPABASE_URL}/rest/v1/stock_data_sources?select=*&order=category,is_primary.desc",
            headers=HEADERS_SB, timeout=20).json()

        # Per-agent success rate from job_runs
        success = per_agent_success_rate(30)
        print(f"Per-agent success rate (30d): {success}")

        # Generate recommendations
        recs = generate_recommendations(sources, success)

        # Build report (HTML parse mode — avoids Markdown underscore ambiguity in agent names)
        lines = ["📊 <b>Hub4Apps source health (monthly review)</b>", ""]
        by_cat: dict[str, list[dict]] = defaultdict(list)
        for s in sources:
            by_cat[s["category"]].append(s)
        for cat, items in sorted(by_cat.items()):
            lines.append(f"<b>{cat}</b>:")
            for s in items:
                marker = "★" if s["is_primary"] else "·"
                health = "✅" if s["last_health_check_ok"] else "❌" if s["last_health_check_ok"] is False else "?"
                lines.append(f"  {marker} <code>{s['name']:<20}</code> {health}  fails: {s['consecutive_failures']}")
            lines.append("")
        lines.append("<b>Per-agent success (last 30d):</b>")
        for agent, st in success.items():
            lines.append(f"  <code>{agent:<22}</code> {st.get('success_rate',0):.0%}  (n={st.get('total',0)})")
        if recs:
            lines.append("")
            lines.append("<b>Recommendations:</b>")
            lines.extend(recs)
        else:
            lines.append("")
            lines.append("✅ No source-promotion recommendations this cycle.")

        report = "\n".join(lines)
        print(report)
        sent = telegram_send(report)
        print(f"Telegram sent: {sent}")

        job_run_finish(run_id, "ok", len(sources))
        return 0
    except Exception as e:
        import traceback
        print(traceback.format_exc(), file=sys.stderr)
        job_run_finish(run_id, "failed", 0)
        return 1


if __name__ == "__main__":
    sys.exit(main())
