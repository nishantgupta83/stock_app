"""
Static site generator.

Pulls data from Supabase, renders static HTML pages + CSS via Jinja2 into dist/.
The workflow deploys dist/ to Hostinger by FTPS.

Run via .github/workflows/site_generator.yml on */15 cron.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yfinance as yf
from jinja2 import Environment, FileSystemLoader, select_autoescape

try:
    from curl_cffi import requests as cffi_requests
    _CF_SESSION = cffi_requests.Session(impersonate="chrome")
except ImportError:
    _CF_SESSION = None

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = ROOT / "templates"
DIST_DIR = ROOT / "dist"

HEADERS_SB = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
}

SB_ERRORS: list[str] = []

# Known agent inventory — drives the agents tab even if agent has no signals yet
# Single source of truth for agent inventory + expected freshness SLA.
# expected_minutes = wall clock between consecutive successful runs;
# anything past 2x that is considered stale. Set to None for manual-only.
AGENT_INVENTORY: dict[str, dict] = {
    "filing":              {"job": "filing_agent",              "expected_minutes": 360},   # every 6h
    "news":                {"job": "news_agent",                "expected_minutes": 60},
    "truth_social":        {"job": "truth_social_agent",        "expected_minutes": 60},
    "thesis":              {"job": "thesis_agent",              "expected_minutes": 60},
    "earnings":            {"job": "earnings_agent",            "expected_minutes": 10080},  # weekly
    "price":               {"job": "price_agent",               "expected_minutes": 1440},   # daily EOD
    "paper_trade":         {"job": "paper_trade_agent",         "expected_minutes": 60},
    "backtester":          {"job": "backtester",                "expected_minutes": None},   # manual
    "source_review":       {"job": "source_review_agent",       "expected_minutes": 43200},  # monthly
    "telegram_dispatcher": {"job": "telegram_dispatcher",       "expected_minutes": 60},
    "flows":               {"job": "flows_agent",               "expected_minutes": 10080},  # weekly Sun
    "site_generator":      {"job": "site_generator",            "expected_minutes": 60},
    "event_paper":         {"job": "event_paper_agent",         "expected_minutes": 90},
    "market_scanner":      {"job": "market_scanner_agent",      "expected_minutes": 1440},   # daily EOD
    "crypto_macro":        {"job": "crypto_macro_agent",        "expected_minutes": 1440},   # daily EOD
    "archive":             {"job": "archive_agent",             "expected_minutes": 10080},  # weekly Sun
    "intraday_alert":      {"job": "intraday_alert_agent",      "expected_minutes": 15},     # market hours
    "macro_rates":         {"job": "macro_rates_agent",         "expected_minutes": 1440},   # daily
    "activist_insider":    {"job": "activist_insider_agent",    "expected_minutes": 120},
    "defense":             {"job": "defense_agent",             "expected_minutes": 1440},   # daily
    "biotech":             {"job": "biotech_agent",             "expected_minutes": 1440},   # daily
    "energy_transition":   {"job": "energy_transition_agent",   "expected_minutes": 1440},   # daily
    "consumer_health":     {"job": "consumer_health_agent",     "expected_minutes": 1440},   # daily
}
KNOWN_AGENTS = list(AGENT_INVENTORY.keys()) + [
    f"workflow_{v['job']}" for v in AGENT_INVENTORY.values()
    if v["job"] not in ("backtester", "site_generator", "telegram_dispatcher")
]
_JOB_NAME = {k: v["job"] for k, v in AGENT_INVENTORY.items()}


def sb_get(path: str, params: dict | list[tuple[str, str]] | None = None) -> list[dict]:
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{path}", headers=HEADERS_SB, params=params or {}, timeout=20)
    if r.status_code != 200:
        msg = f"SB {path} {r.status_code}: {r.text[:200]}"
        SB_ERRORS.append(msg)
        print(f"  {msg}", file=sys.stderr)
        return []
    return r.json()


_SENSITIVE_RE = re.compile(
    r"(eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)"
    r"|((?:apikey|authorization|bearer|token|password|passwd|secret|service_key)"
    r"\s*[:=]\s*)[^\s,;'\"]+",
    re.I,
)


def redact_sensitive(value: object) -> str:
    """Bound debug text before it is published to the static site."""
    text = "" if value is None else str(value)
    text = _SENSITIVE_RE.sub(lambda m: "[REDACTED]" if m.group(1) else f"{m.group(2)}[REDACTED]", text)
    return text[:500]


def public_event(row: dict) -> dict:
    """Return a payload-minimized event safe for the public static dashboard."""
    payload = row.get("payload") or {}
    et = row.get("event_type") or ""
    allowed: dict = {}
    if et == "news_article":
        allowed = {
            "headline": payload.get("headline"),
            "url": payload.get("url"),
            "source": payload.get("source"),
            "direction_prior": payload.get("direction_prior"),
        }
    elif et == "truth_social_post":
        allowed = {
            "rule_label": payload.get("rule_label"),
            "direction_prior": payload.get("direction_prior"),
            "post_excerpt": payload.get("post_excerpt"),
            "url": payload.get("url"),
        }
    elif et == "8k_material_event":
        allowed = {
            "form_type": payload.get("form_type"),
            "primary_doc_url": payload.get("primary_doc_url"),
            "primary_doc_desc": payload.get("primary_doc_desc"),
            "8k_items": payload.get("8k_items"),
        }
    elif et.startswith("filing_"):
        allowed = {
            "form_type": payload.get("form_type"),
            "primary_doc_url": payload.get("primary_doc_url"),
            "primary_doc_desc": payload.get("primary_doc_desc"),
            "matched_keyword": payload.get("matched_keyword"),
            "direction_prior": payload.get("direction_prior"),
        }
    elif et == "earnings_release":
        allowed = {
            "actual_eps": payload.get("actual_eps"),
            "estimated_eps": payload.get("estimated_eps"),
            "surprise_pct": payload.get("surprise_pct"),
        }
    elif et == "momentum":
        allowed = {
            "ticker_return_pct": payload.get("ticker_return_pct"),
            "spy_return_pct": payload.get("spy_return_pct"),
            "rel_strength_pct": payload.get("rel_strength_pct"),
            "lookback_days": payload.get("lookback_days"),
        }
    else:
        allowed = {
            "rule_label": payload.get("rule_label"),
            "direction_prior": payload.get("direction_prior"),
        }
    clean = {k: redact_sensitive(v) if isinstance(v, str) else v
             for k, v in allowed.items() if v is not None}
    return {**row, "payload": clean}


# ============================================================
# Data fetchers
# ============================================================

def fetch_signals(limit: int = 500) -> list[dict]:
    rows = sb_get("stock_signals", {
        "select": "id,ticker,fired_at,action,score,confidence,evidence_summary,status_v2,model_version,weight_at_time,score_breakdown,direction,horizon_days",
        "order":  "fired_at.desc",
        "limit":  str(limit),
    })
    # Flatten weight_at_time.agents → agents list for the UI
    for r in rows:
        wt = r.get("weight_at_time") or {}
        r["agents"] = wt.get("agents", []) if isinstance(wt, dict) else []
        r["score"] = float(r.get("score") or 0)
        r["score_pct"] = max(0, min(100, r["score"]))
        r["status_v2"] = r.get("status_v2") or "candidate"
        r["action"] = r.get("action") or "RESEARCH"
        if isinstance(wt, dict) and wt.get("display_action"):
            r["display_action"] = wt["display_action"]
        else:
            r["display_action"] = r["action"]
        bd = r.get("score_breakdown") or {}
        if isinstance(bd, dict) and isinstance(bd.get("items"), list):
            for item in bd["items"]:
                if isinstance(item, dict) and item.get("detail"):
                    item["detail"] = redact_sensitive(item["detail"])
    return rows


def fetch_recent_events(limit: int = 200) -> list[dict]:
    rows = sb_get("stock_normalized_events", {
        "event_at": f"lte.{datetime.now(timezone.utc).isoformat()}",
        "select": "id,ticker,event_type,event_subtype,event_at,severity,payload",
        "order":  "event_at.desc",
        "limit":  str(limit),
    })
    return [public_event(r) for r in rows]


def fetch_agent_freshness() -> list[dict]:
    return sb_get("stock_agent_freshness", {"select": "*"})


def fetch_recent_failures(limit: int = 10) -> list[dict]:
    rows = sb_get("stock_dead_letter_events", {
        "select": "occurred_at,agent,reason,detail",
        "order":  "occurred_at.desc",
        "limit":  str(limit),
    })
    for row in rows:
        row["detail"] = redact_sensitive(row.get("detail"))
    return rows


def fetch_latest_agent_weights() -> dict[str, dict]:
    """Return {agent_name: {weight, accuracy_ema, n_signals}} from most recent date per agent."""
    rows = sb_get("stock_agent_weights", {
        "select": "agent,date,weight,accuracy_ema,n_signals",
        "order":  "date.desc",
        "limit":  "1000",
    })
    latest: dict[str, dict] = {}
    for r in rows:
        latest.setdefault(r["agent"], r)
    return latest


def fetch_latest_backtest() -> dict | None:
    runs = sb_get("stock_backtest_runs", {
        "select": "id,model_version,started_at,finished_at,metrics",
        "order":  "finished_at.desc.nullslast",
        "limit":  "1",
    })
    if not runs or not runs[0].get("metrics"):
        return None
    run = runs[0]
    m = run["metrics"]
    m["model_version"]    = run["model_version"]
    m["run_finished_at"]  = (run.get("finished_at") or "")[:16]
    return m


def count_alerts_today() -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    rows = sb_get("stock_signals", {
        "fired_at":  f"gte.{today}T00:00:00Z",
        "status_v2": "eq.sent",
        "select":    "id",
    })
    return len(rows)


def count_alerts_today_split() -> tuple[int, int]:
    """(cap_counted, bypass) — split today's sent signals by whether they
    consumed a daily-cap slot or rode the severity-4 bypass.

    Pre-fix the dashboard showed `5 - alerts_today` as "remaining", going
    negative once severity-4 bypasses pushed sent above 5. Splitting the
    count exactly lets the template render "X / 5 cap used + Y severity-4
    bypass" instead of a nonsense negative.

    A signal is classified as bypass if its score_breakdown contains a
    severity_uplift_sev4 rule entry (added by thesis_agent.score_evidence
    when any contributing event has severity=4)."""
    today = datetime.now(timezone.utc).date().isoformat()
    rows = sb_get("stock_signals", {
        "fired_at":  f"gte.{today}T00:00:00Z",
        "status_v2": "eq.sent",
        "select":    "id,score_breakdown",
    })
    bypass = 0
    for r in rows:
        breakdown = r.get("score_breakdown") or []
        if isinstance(breakdown, list) and any(
            isinstance(b, dict) and b.get("rule") == "severity_uplift_sev4"
            for b in breakdown
        ):
            bypass += 1
    cap_counted = len(rows) - bypass
    return cap_counted, bypass


def count_open_signals() -> int:
    rows = sb_get("stock_signals", {
        "status_v2": "eq.candidate",
        "select":    "id",
    })
    return len(rows)


def count_fresh_events() -> int:
    """Events that LANDED in the last 180 minutes. Filter by created_at, not
    event_at — same bug class as event_paper_agent (fixed May 2026): event_at
    is the real-world event date which can be days/weeks old for backfilled
    SEC filings or institutional 13F-HR submissions."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=180)).isoformat()
    rows = sb_get("stock_normalized_events", [
        ("created_at", f"gte.{cutoff}"),
        ("select", "id"),
    ])
    return len(rows)


def fetch_latest_job_run(agent: str) -> dict:
    rows = sb_get("stock_job_runs", {
        "agent":  f"eq.{agent}",
        "select": "started_at,finished_at,status,rows_in,rows_out,error_text",
        "order":  "started_at.desc",
        "limit":  "1",
    })
    if not rows:
        return {}
    row = rows[0]
    row["error_text"] = redact_sensitive(row.get("error_text"))
    return row


# ============================================================
# Build derived views
# ============================================================

def fetch_agent_weight_history() -> list[dict]:
    """All agent_weights rows ordered by date — used for the learning chart."""
    return sb_get("stock_agent_weights", {
        "select": "agent,date,accuracy_ema,weight,n_signals",
        "order":  "date.asc",
        "limit":  "2000",
    })


def fetch_forecast_audit() -> list[dict]:
    """All closed signal outcomes for the paper-trade review table."""
    return sb_get("stock_forecast_audit", {
        "select": "signal_id,horizon_days,realized_return,realized_at,correct",
        "order":  "realized_at.desc",
        "limit":  "200",
    })


def _fetch_archive_index() -> dict:
    """Fetch archive/index.json from Hostinger for calibration tier display. Non-fatal."""
    try:
        r = requests.get("https://hub4apps.com/stock_app/archive/index.json", timeout=8)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return {}


def fetch_rule_calibration() -> list[dict]:
    """Per-event-type calibration rows; used by the Calibration dashboard tab."""
    rows = sb_get("stock_rule_calibration", {
        "select": "rule_key,n_observations,n_correct,accuracy,mean_realized_pct,is_mature,matured_at,last_updated",
        "order":  "n_observations.desc",
        "limit":  "200",
    })
    # Attach n_archived from Hostinger archive index so the template can show tier split.
    arc_cal = _fetch_archive_index().get("rule_calibration", {})
    for r in rows:
        arc = arc_cal.get(r.get("rule_key") or "", {})
        r["n_archived"] = int(arc.get("n_observations") or 0)
    # Sort by signal-strength × log(sample): strong-effect rules with N rank first
    import math
    def rank(r: dict) -> float:
        m = float(r.get("mean_realized_pct") or 0)
        n = int(r.get("n_observations") or 0)
        return abs(m) * math.log1p(n)
    rows.sort(key=rank, reverse=True)
    return rows


def fetch_recent_trade_setups(limit: int = 200) -> list[dict]:
    """Layer 3 output: setups still inside their valid_until window.

    Surfaces both sized and skip-tagged setups so an operator can see why
    the risk layer ignored some of them. Bounded to last 14 days by
    created_at as a sanity belt against accidentally-distant valid_until.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    floor_iso = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    return sb_get("stock_trade_setups", {
        "valid_until": f"gte.{now_iso}",
        "created_at":  f"gte.{floor_iso}",
        "select":      "id,signal_id,ticker,direction,setup_type,confidence,"
                       "stop_pct,target_pct,target_source,horizon_days,"
                       "valid_until,reason_to_skip,rule_key,created_at",
        "order":       "created_at.desc",
        "limit":       str(limit),
    })


def fetch_recent_risk_decisions(limit: int = 200) -> list[dict]:
    """Layer 4 output: capital-allocation decisions written today.

    rules_applied is JSONB on the table — PostgREST returns it parsed.
    The template iterates it to render the audit trail.
    """
    today_iso = datetime.now(timezone.utc).date().isoformat()
    return sb_get("stock_risk_decisions", {
        "created_at": f"gte.{today_iso}T00:00:00Z",
        "select":     "id,setup_id,decision,size_pct_portfolio,"
                      "size_dollars_at_100k,max_loss_dollars,reason,"
                      "rules_applied,created_at",
        "order":      "created_at.desc",
        "limit":      str(limit),
    })


def fetch_event_paper_trades(only_status: str | None = None, limit: int = 200) -> list[dict]:
    params = {
        "select": "id,event_type,event_subtype,ticker,direction,entry_at,entry_price,"
                  "exit_at,exit_price,realized_return,correct,status,rule_key",
        "order":  "entry_at.desc",
        "limit":  str(limit),
    }
    if only_status:
        params["status"] = f"eq.{only_status}"
    return sb_get("stock_event_paper_trades", params)


def derive_calibration_summary(cal_rows: list[dict],
                               closed_trades: list[dict]) -> dict:
    """Headline KPIs for the Calibration tab."""
    from datetime import timedelta as _td
    cutoff_30 = (datetime.now(timezone.utc) - _td(days=30)).isoformat()
    closed_30 = [t for t in closed_trades if (t.get("exit_at") or "") >= cutoff_30]
    n_closed = len(closed_30)
    n_correct = sum(1 for t in closed_30 if t.get("correct"))
    avg_ret = (sum(float(t.get("realized_return") or 0) for t in closed_30) / n_closed) if n_closed else 0.0
    return {
        "total_obs":          sum(int(r.get("n_observations") or 0) for r in cal_rows),
        "mature_rules":       [r["rule_key"] for r in cal_rows if r.get("is_mature")],
        "closed_30d_count":   n_closed,
        "closed_30d_winrate": (n_correct / n_closed) if n_closed else 0.0,
        "closed_30d_avg_ret": avg_ret,
    }


def _fetch_paper_forecast_page(limit: int, mode: str | None = None,
                               include_mode_col: bool = True) -> tuple[int, list[dict], str]:
    select_cols = (
        "id,signal_id,ticker,created_at,fired_at,horizon_days,direction,"
        "source_action,paper_action,forecast_mode,prob_win,base_rate,setup_hit_rate,"
        "sample_size,score_bucket,avg_win,avg_loss,expected_value,"
        "risk_reward,entry_price,target_price,stop_price,status,"
        "exit_price,realized_return,realized_at,correct,reason_summary,"
        "features_json,calibration_method"
    )
    if not include_mode_col:
        select_cols = select_cols.replace("forecast_mode,", "")
    params = {
        "select": select_cols,
        "order": "created_at.desc",
        "limit": str(limit),
    }
    if mode and include_mode_col:
        params["forecast_mode"] = f"eq.{mode}"
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/stock_paper_forecasts",
        headers=HEADERS_SB,
        params=params,
        timeout=20,
    )
    return r.status_code, r.json() if r.status_code == 200 else [], r.text


def fetch_paper_forecasts(limit: int = 300) -> list[dict]:
    """Phase 6A probability-calibrated paper forecasts.

    During rollout, sql/0008 may not be applied yet. Suppress missing-table
    noise so site generation continues with an empty Paper Trades page.
    """
    status, rows, text = _fetch_paper_forecast_page(limit, "live")
    if status == 404:
        return []
    if status == 400 and "forecast_mode" in text:
        status, rows, text = _fetch_paper_forecast_page(limit, None, include_mode_col=False)
    elif status == 200:
        shadow_status, shadow_rows, shadow_text = _fetch_paper_forecast_page(limit, "shadow_backtest")
        if shadow_status == 200:
            rows = rows + shadow_rows
        else:
            msg = f"SB stock_paper_forecasts shadow {shadow_status}: {shadow_text[:200]}"
            SB_ERRORS.append(msg)
            print(f"  {msg}", file=sys.stderr)
    if status != 200:
        msg = f"SB stock_paper_forecasts {status}: {text[:200]}"
        SB_ERRORS.append(msg)
        print(f"  {msg}", file=sys.stderr)
        return []
    for row in rows:
        for key in (
            "prob_win", "base_rate", "setup_hit_rate", "avg_win", "avg_loss",
            "expected_value", "risk_reward", "entry_price", "target_price",
            "stop_price", "exit_price", "realized_return",
        ):
            if row.get(key) is not None:
                try:
                    row[key] = float(row[key])
                except (TypeError, ValueError):
                    row[key] = None
        row["sample_size"] = int(row.get("sample_size") or 0)
        row["forecast_mode"] = row.get("forecast_mode") or "live"
        if row.get("reason_summary"):
            row["reason_summary"] = redact_sensitive(row["reason_summary"])
    return rows


def _yfinance_fetch_one(ticker: str, days: int) -> list[dict] | None:
    """Per-ticker yfinance fallback. Used when DB is empty or stale."""
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        t = yf.Ticker(ticker, session=_CF_SESSION) if _CF_SESSION else yf.Ticker(ticker)
        df = t.history(start=start, auto_adjust=False)
        if df.empty:
            return None
        return [
            {
                "ts":     ts.strftime("%Y-%m-%dT00:00:00+00:00"),
                "open":   round(float(row["Open"]),   4) if row.get("Open")  is not None else None,
                "high":   round(float(row["High"]),   4) if row.get("High")  is not None else None,
                "low":    round(float(row["Low"]),    4) if row.get("Low")   is not None else None,
                "close":  round(float(row["Close"]),  4) if row.get("Close") is not None else None,
                "volume": int(row["Volume"]) if row.get("Volume") is not None else None,
            }
            for ts, row in df.iterrows()
        ]
    except Exception as e:
        print(f"  yfinance fallback {ticker}: {e}", file=sys.stderr)
        return None


def fetch_ticker_prices(tickers: list[str], days: int = 180) -> dict[str, list[dict]]:
    """Read daily bars from stock_raw_prices for each ticker.

    If DB rows are missing or stale, yfinance is used only as a render fallback.
    The site generator does not write raw price storage; price ingestion belongs
    to price_agent/historical_ingest.

    Returns {ticker: [{date, close}]} ordered by date asc.
    """
    result: dict[str, list[dict]] = {}
    if not tickers:
        return result

    cutoff_date    = (datetime.now(timezone.utc) - timedelta(days=days)).date()
    stale_cutoff   = (datetime.now(timezone.utc) - timedelta(days=3)).date()

    for ticker in tickers:
        # 1. Try DB
        bars = sb_get("stock_raw_prices", {
            "ticker": f"eq.{ticker}",
            "ts":     f"gte.{cutoff_date.isoformat()}",
            "select": "ts,close",
            "order":  "ts.asc",
            "limit":  "300",
        })
        latest_db_date = None
        if bars:
            try:
                latest_db_date = datetime.fromisoformat(bars[-1]["ts"].replace("Z", "+00:00")).date()
            except Exception:
                pass

        # 2. Refresh from yfinance if DB is empty or stale
        need_refresh = (not bars) or (latest_db_date is None) or (latest_db_date < stale_cutoff)
        if need_refresh:
            yf_bars = _yfinance_fetch_one(ticker, days)
            if yf_bars:
                bars = yf_bars

        if bars:
            result[ticker] = [
                {"date": b["ts"][:10], "close": round(float(b["close"]), 2)}
                for b in bars if b.get("close") is not None
            ]
    return result


def fetch_all_watchlist_tickers() -> list[str]:
    """Every distinct ticker on any watchlist — used to render a ticker page per ticker."""
    rows = sb_get("stock_watchlists", {"select": "ticker"})
    return sorted({r["ticker"] for r in rows if r.get("ticker")})


def fetch_events_for_tickers(tickers: list[str], days: int = 180) -> dict[str, list[dict]]:
    """Pull historical normalized events per ticker for chart annotations + Big Moves."""
    result: dict[str, list[dict]] = {ticker: [] for ticker in tickers}
    if not tickers:
        return result
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    in_list = ",".join(f'"{t}"' for t in tickers)
    rows = sb_get("stock_normalized_events", [
        ("ticker", f"in.({in_list})"),
        ("event_at", f"gte.{cutoff}"),
        ("event_at", f"lte.{datetime.now(timezone.utc).isoformat()}"),
        ("select", "ticker,event_type,event_subtype,event_at,severity,payload"),
        ("order", "event_at.asc"),
        ("limit", "5000"),
    ])
    for r in rows:
        ticker = r.get("ticker")
        if ticker in result:
            result[ticker].append(public_event(r))
    return result


def sector_rotation_data(events: list[dict]) -> list[dict]:
    """Per-watchlist activity in the last 24h: count of events, dominant direction.
    Drives the dashboard's sector heatmap.

    Returns a list of dicts:
        [{"name":..., "label":..., "n_events":..., "n_tickers":..., "direction":...,
          "score": int (-100..100)}, ...]
    where direction is one of 'bull'/'bear'/'neutral' and score sums per-event
    bull (+1) and bear (-1) tagging from the event_type + payload.direction_prior.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

    # Pull watchlist memberships (ticker → set of watchlist names)
    rows = sb_get("stock_watchlists", {"select": "name,ticker", "limit": "5000"})
    ticker_to_lists: dict[str, list[str]] = {}
    for r in rows:
        t = r.get("ticker"); n = r.get("name")
        if t and n:
            ticker_to_lists.setdefault(t, []).append(n)

    # Watchlists we surface on the dashboard, in display order
    SURFACED = [
        ("ai_compute",    "AI · compute"),
        ("ai_optical",    "AI · optical"),
        ("ai_servers",    "AI · servers"),
        ("ai_power",      "AI · power"),
        ("ai_software",   "AI · software"),
        ("ai_neocloud",   "AI · neocloud"),
        ("defense_primes","defense · primes"),
        ("defense_cyber", "defense · cyber"),
        ("biotech_glp1",  "biotech · GLP-1"),
        ("pharma_majors", "pharma · majors"),
        ("ev_makers",     "energy · EV"),
        ("nuclear",       "energy · nuclear"),
        ("retail_big_box","consumer · retail"),
        ("travel_leisure","consumer · travel"),
        ("macro_rates",   "macro · rates"),
    ]

    def _bull_bear(ev: dict) -> int:
        """+1 bull, -1 bear, 0 neutral from event_type + direction_prior."""
        d = ((ev.get("payload") or {}).get("direction_prior") or "").lower()
        if d == "long":  return 1
        if d == "short": return -1
        et = ev.get("event_type") or ""
        if et in ("8k_material_event", "filing_13d", "institutional_new_position",
                  "institutional_increase", "activist_initial_position",
                  "insider_cluster_buy", "fda_pdufa_decision",
                  "nuclear_license_approval", "dod_contract_award"):
            sub = (ev.get("event_subtype") or "").lower()
            if sub == "rejection":
                return -1
            return 1
        if et in ("filing_s-3", "filing_s-3/a", "filing_dilution",
                  "institutional_exit", "institutional_decrease",
                  "vix_spike", "yield_milestone"):
            return -1
        if et == "earnings_release":
            sub = (ev.get("event_subtype") or "").lower()
            if sub == "beat":  return 1
            if sub == "miss":  return -1
        return 0

    # Aggregate per watchlist
    by_list: dict[str, dict] = {}
    for ev in events:
        try:
            ts = datetime.fromisoformat((ev.get("event_at") or "").replace("Z","+00:00"))
        except Exception:
            continue
        if ts < cutoff:
            continue
        t = ev.get("ticker") or ""
        wls = ticker_to_lists.get(t, [])
        bb = _bull_bear(ev)
        for wl in wls:
            agg = by_list.setdefault(wl, {"n": 0, "score": 0, "tickers": set()})
            agg["n"] += 1
            agg["score"] += bb
            agg["tickers"].add(t)

    out: list[dict] = []
    for wl, label in SURFACED:
        agg = by_list.get(wl, {"n": 0, "score": 0, "tickers": set()})
        if agg["n"] == 0:
            direction = "neutral"
        elif agg["score"] > 0:
            direction = "bull"
        elif agg["score"] < 0:
            direction = "bear"
        else:
            direction = "neutral"
        out.append({
            "name":      wl,
            "label":     label,
            "n_events":  agg["n"],
            "n_tickers": len(agg["tickers"]),
            "direction": direction,
            "score":     agg["score"],
        })
    return out


def recent_intraday_alerts(limit: int = 9) -> list[dict]:
    """Intraday spike alerts for the dashboard wire feed.

    Pulls today's intraday_alert_agent signals (dedupe_key starts with
    'intraday_spike_'). Returns enough context for the alert card."""
    today = datetime.now(timezone.utc).date().isoformat()
    rows = sb_get("stock_signals", {
        "dedupe_key": f"like.intraday_spike_*_{today}",
        "select":     "ticker,action,score,fired_at,evidence_summary,direction",
        "order":      "score.desc",
        "limit":      str(limit),
    })
    return rows


def build_pre_signal_candidates(events: list[dict]) -> list[dict]:
    """Tickers with events in the last 5 days that haven't yet clustered into a signal.
    Shows the user what's building toward a signal."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=5)
    by_ticker: dict[str, dict] = {}
    for e in events:
        try:
            t = datetime.fromisoformat(e["event_at"].replace("Z", "+00:00"))
        except Exception:
            continue
        if t < cutoff or t > now:
            continue
        ticker = e.get("ticker") or "?"
        if ticker not in by_ticker:
            by_ticker[ticker] = {"ticker": ticker, "agents": set(), "events": [], "latest": t}
        by_ticker[ticker]["events"].append(e)
        et = e["event_type"]
        agent = ("filing" if et.startswith("filing_") or et == "8k_material_event"
                 else "truth_social" if et == "truth_social_post"
                 else "news" if et == "news_article" else "other")
        by_ticker[ticker]["agents"].add(agent)
        if t > by_ticker[ticker]["latest"]:
            by_ticker[ticker]["latest"] = t

    rows = []
    for d in sorted(by_ticker.values(), key=lambda x: len(x["agents"]), reverse=True):
        rows.append({
            "ticker":      d["ticker"],
            "event_count": len(d["events"]),
            "agents":      sorted(d["agents"]),
            "agent_count": len(d["agents"]),
            "latest":      d["latest"].strftime("%Y-%m-%d %H:%M"),
        })
    return rows[:15]


def derive_agent_rows(weights: dict, freshness: list[dict], signals: list[dict]) -> list[dict]:
    # job_runs uses long names ("filing_agent"), KNOWN_AGENTS uses short ("filing")
    fresh_map = {f["agent"]: f for f in freshness}
    contrib = Counter()
    cutoff_30d = datetime.now(timezone.utc) - timedelta(days=30)
    for s in signals:
        try:
            t = datetime.fromisoformat(s["fired_at"].replace("Z", "+00:00"))
        except Exception:
            continue
        if t < cutoff_30d:
            continue
        for a in s.get("agents", []):
            contrib[a] += 1

    rows = []
    for name in KNOWN_AGENTS:
        w        = weights.get(name) or {}
        job_name = _JOB_NAME.get(name, name)
        f        = fresh_map.get(job_name) or fresh_map.get(name) or {}
        rows.append({
            "name":               name,
            "weight":             float(w.get("weight") or 1.0),
            "accuracy_ema":       float(w.get("accuracy_ema") or 0.5),
            "contributions_30d":  contrib.get(name, 0),
            "last_seen":          (f.get("last_seen") or "")[:16],
            "last_status":        f.get("last_status") or "",
            "failures_1h":        int(f.get("failures_last_hour") or 0),
            "stale_running":      int(f.get("stale_running") or 0),
        })
    return rows


def derive_dashboard_metrics(events: list[dict], freshness: list[dict]) -> dict:
    cutoff_24h = datetime.now(timezone.utc) - timedelta(hours=24)
    by_agent = defaultdict(int)
    samples = {}
    # Central event_type → agent mapping so news, macro, defense, biotech,
    # energy, consumer-health activity is visible in the dashboard summary
    # (previously everything except filings + truth_social got bucketed into "other").
    def _agent_for(event_type: str) -> str:
        et = event_type
        if et.startswith("filing_") or et == "8k_material_event": return "filing"
        if et == "truth_social_post":                              return "truth_social"
        if et == "news_article":                                   return "news"
        if et.startswith("earnings_"):                             return "earnings"
        if et.startswith("institutional_") or et == "activist_initial_position" \
           or et == "insider_cluster_buy":                         return "activist"
        if et == "crypto_macro_move":                              return "crypto_macro"
        if et == "dod_contract_award":                             return "defense"
        if et in ("fda_pdufa_decision", "clinical_readout"):       return "biotech"
        if et == "nuclear_license_approval":                       return "energy"
        if et in ("consumer_sentiment", "traffic_data"):           return "consumer"
        if et in ("vix_spike", "yield_milestone", "yield_snapshot",
                  "fomc_decision", "cpi_release", "nfp_release"):  return "macro"
        if et == "momentum":                                       return "price"
        return "other"

    for e in events:
        try:
            t = datetime.fromisoformat(e["event_at"].replace("Z", "+00:00"))
        except Exception:
            continue
        if t < cutoff_24h:
            continue
        agent = _agent_for(e["event_type"])
        by_agent[agent] += 1
        samples.setdefault(agent, e.get("event_subtype") or e["event_type"])

    # Agent health: schedule-aware. Each agent's SLA defines its own stale
    # threshold (2x expected_minutes); manual-only agents are skipped from
    # the health roster entirely so they don't poison the dashboard count.
    #
    # Dedupe first: stock_agent_freshness contains BOTH the agent's internal
    # heartbeat row (e.g. filing_agent) AND the workflow wrapper row
    # (workflow_filing_agent) emitted by ops_recorder. Counting both inflates
    # the "X / Y healthy" denominator. Prefer the bare row because it reflects
    # whether data work actually completed; the wrapper row can be "ok" even
    # if the agent itself produced nothing useful (e.g. deps installed but
    # agent main() short-circuited).
    by_canonical: dict[str, dict] = {}
    for f in freshness:
        name = f.get("agent") or ""
        canonical = name[len("workflow_"):] if name.startswith("workflow_") else name
        stored = by_canonical.get(canonical)
        if stored is None:
            by_canonical[canonical] = f
        else:
            stored_is_wrapper = (stored.get("agent") or "").startswith("workflow_")
            current_is_wrapper = name.startswith("workflow_")
            if stored_is_wrapper and not current_is_wrapper:
                by_canonical[canonical] = f

    now = datetime.now(timezone.utc)
    healthy = []
    stale = []
    manual = []
    for f in by_canonical.values():
        agent_name = f.get("agent") or ""
        short = agent_name[len("workflow_"):] if agent_name.startswith("workflow_") else agent_name
        # Try direct + reverse-mapped lookup
        inv = AGENT_INVENTORY.get(short)
        if inv is None:
            for k, v in AGENT_INVENTORY.items():
                if v["job"] == short:
                    inv = v
                    break
        expected = (inv or {}).get("expected_minutes")
        if expected is None:
            manual.append(agent_name)
            continue
        # Schedule-aware threshold: 2x expected interval, floor 30 min.
        # No upper cap — earlier formula capped at 24h, which falsely flagged
        # weekly (expected=10080) and monthly (expected=43200) agents as stale
        # whenever they hadn't run in a day. 2x their natural cadence is the
        # right window: weekly tolerates 14d, monthly tolerates ~60d.
        threshold_min = max(30, expected * 2)
        cutoff = now - timedelta(minutes=threshold_min)
        last = f.get("last_seen") or ""
        try:
            t = datetime.fromisoformat(last.replace("Z", "+00:00"))
            latest_status = f.get("last_status") or ""
            stale_running = int(f.get("stale_running") or 0)
            if t > cutoff and latest_status in ("ok", "partial") and stale_running == 0:
                healthy.append(agent_name)
            else:
                stale.append(agent_name)
        except Exception:
            stale.append(agent_name)

    # Surface all event-source agents in the activity row so news/macro/defense/
    # biotech/energy/consumer/activist/crypto are visible.
    activity_agents = ("filing", "news", "truth_social", "thesis", "macro", "activist",
                       "defense", "biotech", "energy", "consumer", "crypto_macro")
    return {
        "agent_activity": [(a, by_agent[a], samples.get(a, "")) for a in activity_agents if by_agent[a] > 0],
        "healthy_agents": healthy,
        "stale_agents":   stale,
        "manual_agents":  manual,
    }


def derive_paper_metrics(forecasts: list[dict]) -> dict:
    open_rows = [f for f in forecasts if f.get("status") == "open"]
    closed_rows = [f for f in forecasts if f.get("status") == "closed"]
    live_rows = [f for f in forecasts if f.get("forecast_mode", "live") == "live"]
    shadow_rows = [f for f in forecasts if f.get("forecast_mode") == "shadow_backtest"]
    live_open_rows = [f for f in live_rows if f.get("status") == "open"]
    shadow_closed_rows = [f for f in shadow_rows if f.get("status") == "closed"]
    long_rows = [f for f in open_rows if f.get("paper_action") == "PAPER_LONG"]
    avg_prob = (
        sum(float(f.get("prob_win") or 0) for f in open_rows) / len(open_rows)
        if open_rows else 0.0
    )
    positive_ev = [
        f for f in open_rows
        if f.get("expected_value") is not None and float(f["expected_value"]) > 0
    ]
    closed_scored = [f for f in closed_rows if f.get("correct") is not None]
    shadow_scored = [f for f in shadow_closed_rows if f.get("correct") is not None]
    closed_correct = [f for f in closed_scored if f.get("correct") is True]
    shadow_correct = [f for f in shadow_scored if f.get("correct") is True]
    return {
        "total":       len(forecasts),
        "open":        len(open_rows),
        "closed":      len(closed_rows),
        "live_total":  len(live_rows),
        "live_open":   len(live_open_rows),
        "shadow_total": len(shadow_rows),
        "shadow_closed": len(shadow_closed_rows),
        "paper_long":  len(long_rows),
        "positive_ev": len(positive_ev),
        "avg_prob":    avg_prob,
        "closed_hit_rate": (len(closed_correct) / len(closed_scored)) if closed_scored else None,
        "shadow_hit_rate": (len(shadow_correct) / len(shadow_scored)) if shadow_scored else None,
    }


def derive_calibration_groups(forecasts: list[dict], limit: int = 20) -> list[dict]:
    """Group paper forecasts by mode/setup so sparse calibration is visible."""
    groups: dict[tuple, dict] = {}
    for f in forecasts:
        features = f.get("features_json") if isinstance(f.get("features_json"), dict) else {}
        setup_key = features.get("setup_key") or f.get("source_action") or "unknown"
        key = (
            f.get("forecast_mode", "live"),
            f.get("ticker") or "?",
            f.get("horizon_days") or 1,
            f.get("score_bucket") or "?",
            setup_key,
            f.get("paper_action") or "?",
        )
        row = groups.setdefault(key, {
            "forecast_mode": key[0],
            "ticker": key[1],
            "horizon_days": key[2],
            "score_bucket": key[3],
            "setup_key": key[4],
            "paper_action": key[5],
            "n": 0,
            "closed": 0,
            "correct": 0,
            "prob_sum": 0.0,
            "ev_sum": 0.0,
        })
        row["n"] += 1
        row["prob_sum"] += float(f.get("prob_win") or 0)
        row["ev_sum"] += float(f.get("expected_value") or 0)
        if f.get("status") == "closed" and f.get("correct") is not None:
            row["closed"] += 1
            if f.get("correct") is True:
                row["correct"] += 1
    out = []
    for row in groups.values():
        row["avg_prob"] = row["prob_sum"] / row["n"] if row["n"] else None
        row["avg_ev"] = row["ev_sum"] / row["n"] if row["n"] else None
        row["hit_rate"] = row["correct"] / row["closed"] if row["closed"] else None
        out.append(row)
    out.sort(key=lambda r: (r["forecast_mode"] != "live", -r["n"], r["ticker"], r["setup_key"]))
    return out[:limit]


# ============================================================
# Render
# ============================================================

# Two-tier maturity gates surfaced in status.json. The production gate
# (0.90 accuracy, n>=30) is canonical and lives in price_agent.py; the
# training gate is a parallel surface used by the digest routines and
# dashboard so paper-mode rule progress is visible before production
# graduation. Lowering the training gate does NOT change BUY/SELL emission
# in thesis_agent — that stays gated on the production tier. Training-tier
# rules are paper-only and slated for PROVISIONAL_LONG/SHORT emission in a
# follow-up (see docs/next-phases-roadmap.md).
MATURITY_PRODUCTION_ACC = 0.90
MATURITY_TRAINING_ACC   = 0.70
MATURITY_MIN_N          = 30


def _emit_status_json(
    *,
    dist_dir: Path,
    freshness: list[dict],
    cal_rows: list[dict],
    cal_summary: dict,
    open_paper: list[dict],
    closed_paper: list[dict],
    signals: list[dict],
    events: list[dict],
    failures: list[dict],
    alerts_today: int,
    open_signals: int,
    fresh_events: int,
) -> None:
    """Emit dist/status.json — machine-readable view of pipeline state.

    Consumers (morning routine, external monitors) read this instead of
    scraping the dashboard HTML. AGENT_INVENTORY is the single source of
    truth for expected_minutes; production/training maturity constants are
    defined at module top so the dashboard, digest, and any future consumer
    all reference the same numbers.
    """
    import json
    now = datetime.now(timezone.utc)
    today_prefix = now.date().isoformat()
    cutoff_24h = now - timedelta(hours=24)

    freshness_out = []
    for f in freshness:
        agent_name = f.get("agent") or ""
        short = agent_name.replace("workflow_", "")
        inv = AGENT_INVENTORY.get(short)
        if inv is None:
            for v in AGENT_INVENTORY.values():
                if v["job"] == short:
                    inv = v
                    break
        expected = (inv or {}).get("expected_minutes")
        last_seen = f.get("last_seen") or ""
        minutes_since = None
        try:
            t = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
            minutes_since = int((now - t).total_seconds() / 60)
        except Exception:
            pass
        freshness_out.append({
            "agent":              agent_name,
            "last_seen":          last_seen,
            "last_status":        f.get("last_status") or "",
            "stale_running":      int(f.get("stale_running") or 0),
            "minutes_since_last": minutes_since,
            "expected_minutes":   expected,
        })

    by_event_type: Counter = Counter()
    for e in events:
        try:
            t = datetime.fromisoformat((e.get("event_at") or "").replace("Z", "+00:00"))
        except Exception:
            continue
        if t >= cutoff_24h:
            by_event_type[e.get("event_type") or "unknown"] += 1

    by_action: Counter = Counter()
    for s in signals:
        if (s.get("status_v2") == "sent"
                and (s.get("fired_at") or "").startswith(today_prefix)):
            by_action[s.get("action") or "unknown"] += 1

    # Tag every rule with both gates so consumers (digest, dashboard) can
    # filter by either. is_mature is canonical (set by price_agent on the
    # production gate); is_mature_training is derived here from the same
    # underlying numbers using the lower threshold.
    def _tier_flags(r: dict) -> dict:
        n   = int(r.get("n_observations") or 0)
        acc = float(r.get("accuracy") or 0)
        return {
            "meets_training_gate":   (n >= MATURITY_MIN_N) and (acc >= MATURITY_TRAINING_ACC),
            "meets_production_gate": (n >= MATURITY_MIN_N) and (acc >= MATURITY_PRODUCTION_ACC),
        }

    mature_production_keys = [r["rule_key"] for r in cal_rows
                              if r.get("is_mature") and r.get("rule_key")]
    # Training-mature includes production-mature (production is a superset)
    mature_training_keys = [r["rule_key"] for r in cal_rows
                            if _tier_flags(r)["meets_training_gate"] and r.get("rule_key")]

    closest = sorted(
        (r for r in cal_rows if not _tier_flags(r)["meets_training_gate"]),
        key=lambda r: (int(r.get("n_observations") or 0),
                       float(r.get("accuracy") or 0)),
        reverse=True,
    )[:10]
    closest_out = [{
        "rule_key":              r.get("rule_key"),
        "n_observations":        int(r.get("n_observations") or 0),
        "n_correct":             int(r.get("n_correct") or 0),
        "accuracy":              float(r.get("accuracy") or 0),
        "is_mature":             bool(r.get("is_mature")),
        **_tier_flags(r),
    } for r in closest]

    # Today's closed paper trades — surfaced for the 4 PM market-close digest.
    closed_today = [t for t in closed_paper
                    if (t.get("exit_at") or "").startswith(today_prefix)]
    n_closed_today = len(closed_today)
    n_correct_today = sum(1 for t in closed_today if t.get("correct"))
    avg_ret_today = (sum(float(t.get("realized_return") or 0) for t in closed_today)
                     / n_closed_today) if n_closed_today else 0.0
    closed_today_out = [{
        "ticker":          t.get("ticker"),
        "rule_key":        t.get("rule_key"),
        "direction":       t.get("direction"),
        "entry_at":        t.get("entry_at"),
        "exit_at":         t.get("exit_at"),
        "realized_return": float(t.get("realized_return") or 0),
        "correct":         bool(t.get("correct")),
    } for t in closed_today[:20]]

    inventory_out = {
        k: {"job": v["job"], "expected_minutes": v["expected_minutes"]}
        for k, v in AGENT_INVENTORY.items()
    }

    payload = {
        "schema_version": "1.1",
        "generated_at":   now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generator":      "site_generator",
        "platform": {
            "name":  "stock_app · Hub4Apps Terminal",
            "phase": "calibration accumulation · two-tier maturity",
            "vocabulary": {
                "pre_maturity":   ["WATCH", "RESEARCH", "AVOID_CHASE", "CHASE_RISK"],
                "training_tier":  ["PROVISIONAL_LONG", "PROVISIONAL_SHORT"],
                "production_tier": ["BUY", "SELL"],
                "emission_status": {
                    "pre_maturity":     "live (thesis_agent emits these now)",
                    "training_tier":    "planned (see docs/next-phases-roadmap.md) "
                                        "— rules can graduate at training gate today, "
                                        "but thesis_agent vocabulary wiring is pending",
                    "production_tier":  "live but gated (no rule has crossed 0.90 yet)",
                },
            },
            "maturity_gate": {
                "production": {
                    "min_observations": MATURITY_MIN_N,
                    "min_accuracy":     MATURITY_PRODUCTION_ACC,
                    "vocabulary":       ["BUY", "SELL"],
                    "purpose":          "Canonical maturity — unlocks BUY/SELL in Telegram alerts.",
                },
                "training": {
                    "min_observations": MATURITY_MIN_N,
                    "min_accuracy":     MATURITY_TRAINING_ACC,
                    "vocabulary":       ["PROVISIONAL_LONG", "PROVISIONAL_SHORT"],
                    "purpose":          "Visibility tier — rules that have shown signal but aren't "
                                        "production-ready. Paper-trade outcomes feed calibration.",
                },
            },
            "alerting":   {"daily_cap": 5, "severity_4_bypass": True},
            "data_notes": [
                "event_at = real-world event date; created_at = DB landing time.",
                "Use created_at when filtering for recent activity.",
                "4 paper trades emitted per event (horizons 1d/7d/15d/30d).",
                "Staleness rule of thumb: minutes_since_last > 2 * expected_minutes.",
                "Training tier is a superset of production tier (every prod-mature rule is also training-mature).",
            ],
        },
        "agents": {
            "inventory": inventory_out,
            "freshness": freshness_out,
        },
        "calibration": {
            "summary": {
                "total_observations":     cal_summary.get("total_obs", 0),
                "n_mature_production":    len(mature_production_keys),
                "mature_production_keys": mature_production_keys,
                "n_mature_training":      len(mature_training_keys),
                "mature_training_keys":   mature_training_keys,
                "closed_30d_count":       cal_summary.get("closed_30d_count", 0),
                "closed_30d_winrate":     cal_summary.get("closed_30d_winrate", 0.0),
                "closed_30d_avg_return":  cal_summary.get("closed_30d_avg_ret", 0.0),
            },
            "closest_to_maturity": closest_out,
        },
        "events": {
            "fresh_last_180min": fresh_events,
            "by_event_type_24h": dict(by_event_type),
        },
        "signals": {
            "dispatched_today":           alerts_today,
            "dispatched_today_cap":       alerts_cap_counted,
            "dispatched_today_bypass":    alerts_bypass,
            "dispatched_today_by_action": dict(by_action),
            "open_candidates":            open_signals,
        },
        "paper_trades": {
            "open_count":           len(open_paper),
            "closed_today_count":   n_closed_today,
            "closed_today_winrate": (n_correct_today / n_closed_today) if n_closed_today else 0.0,
            "closed_today_avg_return": avg_ret_today,
            "closed_today":         closed_today_out,
        },
        "recent_failures": [
            {
                "occurred_at": f.get("occurred_at"),
                "agent":       f.get("agent"),
                "reason":      f.get("reason"),
                "detail":      f.get("detail"),
            }
            for f in failures[:10]
        ],
    }

    text = json.dumps(payload, indent=2, sort_keys=False)
    (dist_dir / "status.json").write_text(text)
    print(f"Wrote status.json ({len(text)} bytes)")


def render_all() -> int:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "j2"]),
        keep_trailing_newline=True,
    )

    # Pull data once
    signals      = fetch_signals(500)
    events       = fetch_recent_events(200)
    freshness    = fetch_agent_freshness()
    failures     = fetch_recent_failures(10)
    weights      = fetch_latest_agent_weights()
    backtest     = fetch_latest_backtest()
    alerts_today = count_alerts_today()
    alerts_cap_counted, alerts_bypass = count_alerts_today_split()
    open_signals = count_open_signals()
    fresh_events = count_fresh_events()
    weight_hist  = fetch_agent_weight_history()
    audit_rows   = fetch_forecast_audit()
    paper_forecasts = fetch_paper_forecasts(300)
    paper_job    = fetch_latest_job_run("paper_trade_agent")
    thesis_job   = fetch_latest_job_run("thesis_agent")

    if SB_ERRORS:
        raise RuntimeError("Supabase read errors; refusing to publish stale/empty dashboard: " + " | ".join(SB_ERRORS[:5]))

    agent_rows   = derive_agent_rows(weights, freshness, signals)
    dash         = derive_dashboard_metrics(events, freshness)
    candidates   = build_pre_signal_candidates(events)
    paper_metrics = derive_paper_metrics(paper_forecasts)
    calibration_groups = derive_calibration_groups(paper_forecasts)

    # Build ticker pages for the entire watchlist (not only signal-bearing ones)
    # so any tracked ticker can be inspected. Signal tickers get prioritized
    # by sorting them first; cap at 30 to keep render time bounded.
    all_watchlist  = fetch_all_watchlist_tickers()
    signal_tickers = list({s["ticker"] for s in signals if s.get("ticker")})
    sorted_tickers = sorted(set(all_watchlist),
                            key=lambda t: (t not in signal_tickers, t))[:30]
    prices = fetch_ticker_prices(sorted_tickers, days=180)
    context_prices = fetch_ticker_prices(
        ["SPY", "QQQ", "BTC-USD", "XLK", "XLF", "XLE", "XLI", "XLV", "XLY", "TLT", "USO"],
        days=180,
    )
    ticker_events = fetch_events_for_tickers(list(prices.keys()), days=180)

    if SB_ERRORS:
        raise RuntimeError("Supabase read errors; refusing to publish stale/empty dashboard: " + " | ".join(SB_ERRORS[:5]))

    distinct_agents = sorted({a for s in signals for a in s.get("agents", [])} | set(KNOWN_AGENTS))
    distinct_types  = sorted({e["event_type"] for e in events})

    common = {
        "generated_at":  datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
    }

    DIST_DIR.mkdir(exist_ok=True)
    shutil.copy(TEMPLATES_DIR / "styles.css", DIST_DIR / "styles.css")

    # Vendor: copy Chart.js + annotation plugin into dist/vendor/. Hostinger
    # LiteSpeed enforces script-src 'self' on the CSP, so external CDN scripts
    # (cdn.jsdelivr.net) are blocked. Templates load these via relative paths.
    vendor_src = TEMPLATES_DIR / "vendor"
    if vendor_src.is_dir():
        vendor_dst = DIST_DIR / "vendor"
        vendor_dst.mkdir(exist_ok=True)
        for f in vendor_src.iterdir():
            if f.is_file():
                shutil.copy(f, vendor_dst / f.name)

    # .htaccess: Hostinger LiteSpeed defaults to a strict CSP that blocks BOTH
    # external scripts AND inline <script> blocks. Vendoring chart.js fixes the
    # first; this header override fixes the second. We restrict to 'self' and
    # 'unsafe-inline' for scripts (no external CDN trust). connect-src stays
    # 'self' so Supabase/external POSTs from the browser would be blocked
    # (we never make any from the rendered HTML — all data is pre-baked).
    (DIST_DIR / ".htaccess").write_text(
        "<IfModule mod_headers.c>\n"
        "    # Override the platform-default Content-Security-Policy. The dashboard\n"
        "    # is fully pre-rendered and uses inline scripts to bind data to\n"
        "    # Chart.js; without 'unsafe-inline' those blocks are dropped silently.\n"
        "    Header always set Content-Security-Policy \"default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "frame-ancestors 'self'; "
        "base-uri 'self'; "
        "form-action 'self';\"\n"
        "</IfModule>\n"
    )

    # Dashboard
    sector_rows = sector_rotation_data(events)
    intraday_alerts = recent_intraday_alerts(limit=9)
    (DIST_DIR / "index.html").write_text(env.get_template("index.html.j2").render(
        **common,
        title="Dashboard", active="index",
        alerts_today=alerts_today,
        alerts_cap_counted=alerts_cap_counted,
        alerts_bypass=alerts_bypass,
        open_signals=open_signals,
        fresh_events=fresh_events,
        recent_signals=[s for s in signals if s.get("status_v2") != "backtest"][:10],
        agent_activity=dash["agent_activity"],
        all_agents_healthy=len(dash["stale_agents"]) == 0 and len(dash["healthy_agents"]) > 0,
        healthy_agent_count=len(dash["healthy_agents"]),
        total_agent_count=len(dash["healthy_agents"]) + len(dash["stale_agents"]),
        stale_agents=dash["stale_agents"],
        candidates=candidates,
        signal_tickers=set(prices.keys()),
        sector_rows=sector_rows,
        intraday_alerts=intraday_alerts,
    ))

    # Signals (with embedded JSON for client-side filter)
    (DIST_DIR / "signals.html").write_text(env.get_template("signals.html.j2").render(
        **common,
        title="Signals", active="signals",
        distinct_agents=distinct_agents,
        signals_json=signals,
    ))

    # Events — annotate with drove_signal flag so the Events tab can mark
    # high-leverage rows. Heuristic: same ticker + same hour bucket as any
    # signal fired today. Cheap O(n+m) in-memory join; no extra DB calls.
    signal_buckets = {
        (s["ticker"], (s.get("fired_at") or "")[:13])
        for s in signals
        if s.get("ticker") and s.get("fired_at")
    }
    for e in events:
        bucket = (e.get("ticker"), (e.get("event_at") or "")[:13])
        e["drove_signal"] = bucket in signal_buckets

    (DIST_DIR / "events.html").write_text(env.get_template("events.html.j2").render(
        **common,
        title="Events", active="events",
        distinct_types=distinct_types,
        events_json=events,
    ))

    # Agents
    (DIST_DIR / "agents.html").write_text(env.get_template("agents.html.j2").render(
        **common,
        title="Agents", active="agents",
        agent_rows=agent_rows,
        recent_failures=failures,
    ))

    # Backtest
    (DIST_DIR / "backtest.html").write_text(env.get_template("backtest.html.j2").render(
        **common,
        title="Backtest", active="backtest",
        bt=backtest,
    ))

    # Trade Setups — Layer 3 output (D2). Surfaces tradable proposals before
    # the risk layer applies survival rules.
    trade_setups = fetch_recent_trade_setups()
    (DIST_DIR / "trade_setups.html").write_text(env.get_template("trade_setups.html.j2").render(
        **common,
        title="Trade Setups", active="trade_setups",
        trade_setups=trade_setups,
    ))

    # Risk Decisions — Layer 4 output (D2). The "size or skip" audit log.
    risk_decisions = fetch_recent_risk_decisions()
    (DIST_DIR / "risk_decisions.html").write_text(env.get_template("risk_decisions.html.j2").render(
        **common,
        title="Risk Decisions", active="risk_decisions",
        risk_decisions=risk_decisions,
    ))

    # Paper Trades — calibrated forecasts generated from live signals
    (DIST_DIR / "paper_trades.html").write_text(env.get_template("paper_trades.html.j2").render(
        **common,
        title="Paper Trades", active="paper_trades",
        forecasts_json=paper_forecasts,
        paper_metrics=paper_metrics,
        calibration_groups=calibration_groups,
        paper_job=paper_job,
        thesis_job=thesis_job,
    ))

    # Calibration — per-rule paper-trade accuracy + open trades + mature rules.
    # Maturity gate: rule needs ≥0.90 accuracy with n≥30 closed trades to unlock BUY/SELL.
    cal_rows = fetch_rule_calibration()
    open_paper = fetch_event_paper_trades(only_status="open", limit=500)
    closed_paper = fetch_event_paper_trades(only_status="closed", limit=200)
    cal_summary = derive_calibration_summary(cal_rows, closed_paper)

    # Per-rule × horizon heatmap. event_paper_agent stores rule_keys as
    # "event_type:subtype:hNd" with N ∈ (1,7,15,30). Group rows by the base
    # rule (event_type:subtype) and surface one column per horizon.
    HORIZONS = (1, 7, 15, 30)
    heat_groups: dict[str, dict] = {}
    for r in cal_rows:
        rk = r.get("rule_key") or ""
        base, _, htag = rk.rpartition(":")
        # htag like "h1d" / "h30d"; legacy keys without :hNd suffix get base=rk
        if not (htag.startswith("h") and htag.endswith("d")):
            base = rk; horizon = None
        else:
            try:
                horizon = int(htag[1:-1])
            except (TypeError, ValueError):
                horizon = None
        grp = heat_groups.setdefault(base, {"base": base, "horizons": {}, "total_obs": 0})
        if horizon is not None:
            grp["horizons"][horizon] = r
        else:
            # legacy: surface in 1d column for visibility
            grp["horizons"].setdefault(1, r)
        grp["total_obs"] += int(r.get("n_observations") or 0)
    heatmap_rows = sorted(heat_groups.values(), key=lambda g: -g["total_obs"])[:20]

    (DIST_DIR / "calibration.html").write_text(env.get_template("calibration.html.j2").render(
        **common,
        title="Calibration", active="calibration",
        rule_rows=cal_rows,
        recent_closed=closed_paper[:30],
        open_paper_count=len(open_paper),
        maturity_acc_pct=90,
        maturity_min_n=30,
        heatmap_rows=heatmap_rows,
        horizons=HORIZONS,
        **cal_summary,
    ))

    # Learning — agent weight evolution over time + paper-trade audit
    (DIST_DIR / "learning.html").write_text(env.get_template("learning.html.j2").render(
        **common,
        title="Learning", active="learning",
        weight_history_json=weight_hist,
        audit_rows=audit_rows,
        signals_by_id={s["id"]: s for s in signals},
    ))

    # Per-ticker chart pages
    ticker_dir = DIST_DIR / "ticker"
    ticker_dir.mkdir(exist_ok=True)
    shutil.copy(DIST_DIR / "styles.css", ticker_dir / "styles.css")
    ticker_tmpl  = env.get_template("ticker_chart.html.j2")
    signals_by_ticker: dict[str, list[dict]] = {}
    for s in signals:
        signals_by_ticker.setdefault(s["ticker"], []).append(s)
    audit_by_signal: dict[int, dict] = {a["signal_id"]: a for a in audit_rows}
    for ticker, price_data in prices.items():
        ticker_sigs = signals_by_ticker.get(ticker, [])
        # Attach audit outcome to each signal
        for s in ticker_sigs:
            s["_audit"] = audit_by_signal.get(s["id"])
        events_for_ticker = ticker_events.get(ticker, [])
        (ticker_dir / f"{ticker}.html").write_text(ticker_tmpl.render(
            **common,
            title=f"{ticker} · Chart",
            active="signals",
            root_path="../",
            ticker=ticker,
            price_json=price_data,
            context_json=context_prices,
            signals_json=ticker_sigs,
            events_json=events_for_ticker,
        ))
    shutil.copy(DIST_DIR / "styles.css", ticker_dir / "styles.css")

    # Per-alert detail pages — one file per signal so Telegram links resolve
    alert_dir = DIST_DIR / "alert"
    alert_dir.mkdir(exist_ok=True)
    # Reuse the CSS by symlinking-equivalent relative path (copy already done above)
    detail_tmpl = env.get_template("signal_detail.html.j2")
    # Group events by ticker for efficient related-event lookup
    events_by_ticker: dict[str, list[dict]] = {}
    for ev in events:
        events_by_ticker.setdefault(ev["ticker"], []).append(ev)
    for sig in signals:
        related = events_by_ticker.get(sig["ticker"], [])[:10]
        (alert_dir / f"{sig['id']}.html").write_text(detail_tmpl.render(
            **common,
            title=f"{sig['ticker']} · {sig['action']}",
            active="signals",
            root_path="../",
            sig=sig,
            related=related,
            SITE_BASE="https://hub4apps.com/stock_app",
        ))
    # Copy styles.css into alert/ so relative links work when opened standalone
    shutil.copy(DIST_DIR / "styles.css", alert_dir / "styles.css")

    _emit_status_json(
        dist_dir=DIST_DIR,
        freshness=freshness,
        cal_rows=cal_rows,
        cal_summary=cal_summary,
        open_paper=open_paper,
        closed_paper=closed_paper,
        signals=signals,
        events=events,
        failures=failures,
        alerts_today=alerts_today,
        open_signals=open_signals,
        fresh_events=fresh_events,
    )

    total_html = len(list(DIST_DIR.glob("*.html"))) + len(list(alert_dir.glob("*.html")))
    print(f"Wrote {total_html} HTML files (incl. {len(signals)} alert pages) + styles.css to {DIST_DIR}")
    return 0


# ============================================================
# Operational logging
# ============================================================

def job_run_start() -> int | None:
    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/stock_job_runs",
            headers={**HEADERS_SB, "Content-Type": "application/json", "Prefer": "return=representation"},
            json={"agent": "site_generator"}, timeout=10,
        )
        if r.status_code in (200, 201) and r.json():
            return r.json()[0]["id"]
    except Exception:
        pass
    return None


def job_run_finish(run_id: int | None, status: str, err: str | None = None) -> None:
    if run_id is None:
        return
    try:
        requests.patch(
            f"{SUPABASE_URL}/rest/v1/stock_job_runs?id=eq.{run_id}",
            headers={**HEADERS_SB, "Content-Type": "application/json"},
            json={
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "status":      status,
                "error_text":  err,
            }, timeout=10,
        )
    except Exception:
        pass


def main() -> int:
    run_id = job_run_start()
    try:
        rc = render_all()
        job_run_finish(run_id, "ok" if rc == 0 else "failed")
        return rc
    except Exception as e:
        import traceback
        print(traceback.format_exc(), file=sys.stderr)
        job_run_finish(run_id, "failed", err=str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
