#!/usr/bin/env python3
"""estimate_egress.py — monthly Supabase READ-egress simulation, to decide
whether a reference-cache blob is worth building (see the 2026-06-09 discussion).

THE QUESTION
------------
Read egress = Σ over every (agent, table) read of:
    runs/day  ×  rows/read  ×  bytes/row  ×  30
We split tables into:
  * REFERENCE (slow-changing — watchlists/calibration/weights/symbols/multiplier):
    cacheable into ONE blob read once/run.
  * BUS (freshness-critical — normalized_events/signals/raw_prices): NOT cacheable.
The blob is only worth building if REFERENCE egress is a material fraction of the
total AND of the free-tier budget. This script projects the monthly numbers so
that's a decision you READ off a curve, not a guess.

MODES
-----
  (default) pure simulation — uses the documented estimates below; runs anywhere.
  --live    refine runs/day from stock_job_runs (last 7d) and bytes/row from a
            1-row sample per table (needs SUPABASE_URL + SUPABASE_SERVICE_KEY).

ESTIMATES ARE EXPLICIT so they can be challenged. rows/read and bytes/row are the
dominant uncertainty; --live calibrates them. Read-only; no writes.
"""
from __future__ import annotations

import argparse
import os
import sys

# Supabase free tier egress budget (GB/month). Configurable via --budget-gb.
FREE_TIER_EGRESS_GB = 5.0

# runs/day per agent, computed from the GitHub-Actions crons (weekday-only and
# market-hours crons are scaled by 5/7 and hours/24). Override per-agent from
# stock_job_runs in --live mode.
RUNS_PER_DAY = {
    "filing_agent": 288, "news_agent": 288, "thesis_agent": 288, "truth_social_agent": 288,  # */5
    "paper_trade_agent": 96,                                                                   # */15
    "intraday_alert_agent": 25.7,            # */15 within 13-21 UTC, weekdays
    "risk_agent": 48, "trade_setup_agent": 48,                                                 # */30
    "event_paper_agent": 24, "realistic_loop_agent": 24, "pulsecheck": 24,                     # hourly
    "price_agent": 8.6,                      # 0 */2 weekdays
    "activist_insider_agent": 12,            # 15 */2
    "audit_agent": 1, "orchestrator_agent": 1,                                                 # daily
    "biotech_agent": 0.71, "consumer_health_agent": 0.71, "crypto_macro_agent": 0.71,
    "defense_agent": 0.71, "energy_transition_agent": 0.71, "macro_rates_agent": 0.71,
    "market_scanner_agent": 0.71, "learning_snapshot": 0.71,                                   # daily weekday
    "archive_agent": 0.143, "earnings_agent": 0.143, "flows_agent": 0.143,                     # weekly
    "source_review_agent": 0.033,                                                              # monthly
}

# Per-table average bytes/row (estimate; --live samples a real row). Event rows
# carry a payload JSON so they're large; reference rows are small.
ROW_BYTES = {
    "stock_normalized_events": 2000,
    "stock_signals":           1200,
    "stock_raw_prices":          90,
    "stock_rule_calibration":   180,
    "stock_agent_weights":       90,
    "stock_watchlists":         120,
    "stock_symbols":            150,
    "stock_rule_sector_multiplier": 120,
}

# REFERENCE tables = cacheable into one blob. BUS = must stay live.
REFERENCE_TABLES = {"stock_rule_calibration", "stock_agent_weights", "stock_watchlists",
                    "stock_symbols", "stock_rule_sector_multiplier"}

# Curated read map: (agent, table, rows_per_read). Focuses on the SIGNIFICANT
# reads that drive egress + every reference read. rows/read estimated from the
# query patterns (limits/filters); --live can't auto-refine these, so they're
# the main lever to challenge. Post-H1, trade_setup reads ~5 thesis rows.
READ_MAP = [
    # --- BUS (freshness-critical, NOT cacheable) ---
    # (read-map corrected per Codex 2026-06-09: filing reads stock_raw_filings
    #  not events; truth_social/news read keyword_rules+raw tables not events;
    #  thesis has a SECOND payload read — the 168h intelligence-layer window.)
    ("thesis_agent",          "stock_normalized_events", 200),  # fetch_fresh_events (180min, payload)
    ("thesis_agent",          "stock_normalized_events", 300),  # fetch_recent_events_window (168h, payload)
    ("event_paper_agent",     "stock_normalized_events", 300),  # recent events for paper trades
    ("intraday_alert_agent",  "stock_normalized_events", 50),
    ("thesis_agent",          "stock_signals",            20),  # alerts_sent_today + dedupe
    ("trade_setup_agent",     "stock_signals",             5),  # post-H1: thesis-lane filtered
    ("site_generator",        "stock_signals",           500),  # EOD dashboard (1/day)
    ("paper_trade_agent",     "stock_signals",            50),
    ("price_agent",           "stock_raw_prices",        400),  # reconcile bars per open trade
    ("market_scanner_agent",  "stock_raw_prices",       2000),
    ("event_paper_agent",     "stock_raw_prices",        100),
    # --- REFERENCE (cacheable into one blob) ---
    ("thesis_agent",          "stock_watchlists",        200),
    ("market_scanner_agent",  "stock_watchlists",        200),
    ("news_agent",            "stock_symbols",           300),  # news reads symbols, not watchlists
    ("thesis_agent",          "stock_rule_calibration",  100),
    ("trade_setup_agent",     "stock_rule_calibration",   30),
    ("event_paper_agent",     "stock_rule_calibration",  100),
    ("price_agent",           "stock_rule_calibration",  100),
    ("thesis_agent",          "stock_agent_weights",      25),
    ("price_agent",           "stock_agent_weights",      25),
    ("thesis_agent",          "stock_symbols",           300),
    ("event_paper_agent",     "stock_symbols",           300),
    ("thesis_agent",          "stock_rule_sector_multiplier", 80),
]

# site_generator runs EOD (~1/day) not on a listed cron here; pin it.
RUNS_PER_DAY.setdefault("site_generator", 1)


def simulate(runs_per_day: dict, row_bytes: dict, budget_gb: float) -> dict:
    per_table: dict[str, float] = {}
    for agent, table, rows in READ_MAP:
        rpd = runs_per_day.get(agent, 0)
        b = row_bytes.get(table, 500)
        per_table[table] = per_table.get(table, 0.0) + rpd * rows * b
    monthly = {t: v * 30 for t, v in per_table.items()}
    ref = sum(v for t, v in monthly.items() if t in REFERENCE_TABLES)
    bus = sum(v for t, v in monthly.items() if t not in REFERENCE_TABLES)
    total = ref + bus
    return {"monthly_by_table": monthly, "ref": ref, "bus": bus, "total": total,
            "budget": budget_gb * 1e9}


def _gb(n: float) -> str:
    return f"{n/1e9:.2f} GB" if n >= 1e9 else f"{n/1e6:.0f} MB"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", action="store_true", help="refine from stock_job_runs + row sampling")
    ap.add_argument("--budget-gb", type=float, default=FREE_TIER_EGRESS_GB)
    args = ap.parse_args()

    runs = dict(RUNS_PER_DAY)
    row_bytes = dict(ROW_BYTES)
    mode = "PURE-SIM (documented estimates)"

    if args.live:
        url = os.environ.get("SUPABASE_URL", "").rstrip("/")
        key = os.environ.get("SUPABASE_SERVICE_KEY", "")
        if not url or not key:
            print("ERROR: --live needs SUPABASE_URL + SUPABASE_SERVICE_KEY.", file=sys.stderr)
            return 2
        import requests
        h = {"apikey": key, "Authorization": f"Bearer {key}"}
        # runs/day from last 7d of stock_job_runs
        from datetime import datetime, timedelta, timezone
        since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        for agent in list(runs):
            r = requests.get(f"{url}/rest/v1/stock_job_runs", headers={**h, "Prefer": "count=exact",
                             "Range-Unit": "items", "Range": "0-0"},
                             params={"agent": f"eq.{agent}", "started_at": f"gte.{since}", "select": "id"}, timeout=20)
            cr = r.headers.get("content-range", "")
            if "/" in cr:
                try:
                    runs[agent] = int(cr.split("/")[-1]) / 7.0
                except ValueError:
                    pass
        # bytes/row from a multi-row sample (avg of ~20 rows — 1 row is too weak;
        # payload-bearing tables vary a lot row-to-row, Codex review).
        for table in list(row_bytes):
            r = requests.get(f"{url}/rest/v1/{table}", headers=h,
                             params={"select": "*", "limit": "20"}, timeout=20)
            if r.status_code == 200 and r.json():
                rows = r.json()
                row_bytes[table] = max(1, sum(len(str(x)) for x in rows) // len(rows))
        mode = "LIVE (runs from stock_job_runs 7d, bytes = 20-row avg)"

    out = simulate(runs, row_bytes, args.budget_gb)

    print(f"\nMonthly Supabase READ-egress simulation — {mode}\n" + "─" * 64)
    print(f"{'table':<32} {'monthly':>10} {'layer':>11}")
    for t, v in sorted(out["monthly_by_table"].items(), key=lambda kv: kv[1], reverse=True):
        layer = "REFERENCE" if t in REFERENCE_TABLES else "bus"
        print(f"{t:<32} {_gb(v):>10} {layer:>11}")
    print("─" * 64)
    print(f"{'BUS (uncacheable)':<32} {_gb(out['bus']):>10}")
    print(f"{'REFERENCE (cacheable→blob)':<32} {_gb(out['ref']):>10}")
    print(f"{'TOTAL':<32} {_gb(out['total']):>10}   "
          f"({100*out['total']/out['budget']:.0f}% of {args.budget_gb:.0f}GB budget)")
    ref_pct = 100 * out["ref"] / out["total"] if out["total"] else 0
    print(f"\nVERDICT: reference layer is {ref_pct:.0f}% of total egress, "
          f"{_gb(out['ref'])}/mo.")
    if ref_pct >= 20 or out["total"] > 0.7 * out["budget"]:
        print("→ A reference-cache blob is WORTH building (material slice and/or near budget).")
    else:
        print("→ A reference-cache blob saves little; the bus dominates. Likely NOT worth the new failure surface.")
    print("\n(Estimates. rows/read + bytes/row are the dominant uncertainty — "
          "run --live to calibrate against real cadence + row sizes.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
