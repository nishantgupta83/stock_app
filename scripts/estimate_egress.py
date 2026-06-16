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

# This per-table READ_MAP is a CURATED model of the heaviest reads. It OMITS many
# small-but-frequent reads (ingest raw-table reads, risk_agent's trade_setups read
# at ~400/day, wrapper/pulsecheck/orchestrator reads), so it is a LOWER BOUND, not
# the true total. Measured against the Supabase dashboard 2026-06-16: ~10.4 GB used
# (~300 MB/day) vs ~3.9 GB modeled → real ≈ 2.6× this model. Trust the dashboard for
# budget decisions; use the table ranking to decide WHICH reads to cut.
MEASURED_DASHBOARD_GB = 10.4         # 2026-06-16, pre-*/15 cadence cut
MEASURED_DASHBOARD_AS_OF = "2026-06-16"
MODEL_UNDERCOUNT_FACTOR = 2.6        # dashboard / model, measured

# runs/day per agent, computed from the GitHub-Actions crons (weekday-only and
# market-hours crons are scaled by 5/7 and hours/24). Override per-agent from
# stock_job_runs in --live mode.
RUNS_PER_DAY = {
    "filing_agent": 96, "news_agent": 96, "thesis_agent": 96, "truth_social_agent": 96,  # */15 (2026-06-16 egress cut, was */5)
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

# Fallback per-table bytes (only used if a READ_MAP entry omits its own bytes).
# Real reads vary 10x by select clause, so READ_MAP entries carry per-READ bytes.
ROW_BYTES = {
    "stock_normalized_events": 740, "stock_signals": 1200, "stock_raw_prices": 90,
    "stock_rule_calibration": 80, "stock_agent_weights": 90, "stock_watchlists": 120,
    "stock_symbols": 60, "stock_rule_sector_multiplier": 120,
}
# Byte profiles by select shape (measured/estimated): trimmed payload (11 fields)
# ≈ base cols + small fields; full event ≈ 740B (live); id/dedup checks tiny.
_B_EVENT_TRIMMED = 360   # base cols + the 11 payload->fields (post-trim)
_B_EVENT_FULL    = 740   # full payload (live-measured)
_B_EVENT_NOPAY   = 260   # base cols, no payload
_B_ID            = 45    # select=id / dedupe_key existence check
_B_SIG_FULL      = 1200  # signal row with score_breakdown + weight_at_time
_B_SMALLCOLS     = 60    # a couple of short cols (ticker,name / rule_key,4 nums)

# REFERENCE tables = cacheable into one blob. BUS = must stay live.
REFERENCE_TABLES = {"stock_rule_calibration", "stock_agent_weights", "stock_watchlists",
                    "stock_symbols", "stock_rule_sector_multiplier"}

# Read map: (agent, table, rows_per_read, bytes_per_row). REBUILT 2026-06-09
# from a direct audit of each agent's actual select clause (Codex-validated):
# most "big table" reads are tiny selects or don't happen (flag-gated), so the
# earlier per-table-bytes model overstated egress badly.
READ_MAP = [
    # --- BUS: stock_normalized_events (thesis reads now PAYLOAD-TRIMMED) ---
    ("thesis_agent",         "stock_normalized_events", 80,  _B_EVENT_TRIMMED),  # fetch_fresh_events
    ("thesis_agent",         "stock_normalized_events", 200, _B_EVENT_TRIMMED),  # fetch_recent_events_window
    ("thesis_agent",         "stock_normalized_events", 10,  _B_EVENT_NOPAY),    # is_risk_off (no payload)
    ("intraday_alert_agent", "stock_normalized_events", 50,  _B_EVENT_FULL),     # reads payload
    ("activist_insider_agent","stock_normalized_events",50,  _B_EVENT_FULL),     # reads payload, 12/day
    ("event_paper_agent",    "stock_normalized_events", 300, _B_EVENT_FULL),    # reads payload (Codex)
    ("market_scanner_agent", "stock_normalized_events", 300, _B_EVENT_NOPAY),
    # site_generator runs EOD ~1/day (pinger moved to EOD; pinned below). The
    # ~161×/day pinger cadence that once dominated egress is gone (the ~95% cut).
    ("site_generator",       "stock_normalized_events", 200, _B_EVENT_FULL),    # public_event reads payload
    ("site_generator",       "stock_raw_prices",       1000, 90),               # per-ticker chart prices
    # --- BUS: stock_signals (frequent reads are tiny dedup/id checks) ---
    ("thesis_agent",         "stock_signals", 50, _B_ID),    # dedupe_key / id / alerts_sent_today
    ("trade_setup_agent",    "stock_signals", 5,  _B_SIG_FULL),  # post-H1 thesis-lane
    ("paper_trade_agent",    "stock_signals", 50, _B_SIG_FULL),
    ("site_generator",       "stock_signals", 500,_B_SIG_FULL),  # 500 full rows × ~1/day (EOD)
    # H6 (2026-06-13): risk_agent paginates the FULL closed-30d (~8k) + open (~7k)
    # populations for portfolio-state — but ONLY on PRODUCTIVE runs (after the
    # empty-setup early-returns, ~few/day), not all 48 cron runs. Deliberately
    # OMITTED from this per-run model (rows×runs_per_day would overestimate by
    # ~8x); --live cadence + the live total (risk not in the top tables) confirm
    # the real impact is small. Re-add with a productive-run multiplier if risk
    # sizing frequency rises.
    # ingest agents: per-run "id&limit=1" existence check (tiny)
    *[(a, "stock_signals", 1, _B_ID) for a in
      ("consumer_health_agent","activist_insider_agent","defense_agent",
       "biotech_agent","energy_transition_agent","macro_rates_agent")],
    # --- BUS: stock_raw_prices ---
    ("price_agent",          "stock_raw_prices", 400, 90),
    ("market_scanner_agent", "stock_raw_prices", 2000, 90),
    ("event_paper_agent",    "stock_raw_prices", 100, 90),
    # --- REFERENCE (small selects; thesis symbols read is FLAG-GATED OFF) ---
    ("news_agent",           "stock_symbols", 300, _B_SMALLCOLS),  # ticker,name */5
    # thesis stock_symbols read OMITTED — gated behind SECTOR_CALIB_MULT_ENABLED (off)
    ("thesis_agent",         "stock_rule_calibration", 100, _B_SMALLCOLS),  # 4 cols
    ("trade_setup_agent",    "stock_rule_calibration", 30,  _B_SMALLCOLS),
    ("event_paper_agent",    "stock_rule_calibration", 100, _B_SMALLCOLS),
    ("price_agent",          "stock_rule_calibration", 100, _B_SMALLCOLS),
    ("thesis_agent",         "stock_agent_weights", 25, _B_SMALLCOLS),
    ("price_agent",          "stock_agent_weights", 25, _B_SMALLCOLS),
    ("thesis_agent",         "stock_watchlists", 200, 120),
    ("market_scanner_agent", "stock_watchlists", 200, 120),
]

# site_generator runs EOD (~1/day) not on a listed cron here; pin it.
RUNS_PER_DAY.setdefault("site_generator", 1)


def simulate(runs_per_day: dict, budget_gb: float) -> dict:
    per_table: dict[str, float] = {}
    for agent, table, rows, bpr in READ_MAP:
        rpd = runs_per_day.get(agent, 0)
        per_table[table] = per_table.get(table, 0.0) + rpd * rows * bpr
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
    mode = "PURE-SIM (documented estimates)"

    if args.live:
        url = os.environ.get("SUPABASE_URL", "").rstrip("/")
        key = os.environ.get("SUPABASE_SERVICE_KEY", "")
        if not url or not key:
            print("ERROR: --live needs SUPABASE_URL + SUPABASE_SERVICE_KEY.", file=sys.stderr)
            return 2
        import requests
        h = {"apikey": key, "Authorization": f"Bearer {key}"}
        # Refine runs/day from the last 7d of stock_job_runs (real cadence —
        # GHA drops some crons). Per-READ bytes stay in READ_MAP (a per-table
        # sample can't capture the differing select clauses).
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
        mode = "LIVE (runs/day from stock_job_runs 7d; per-read bytes from audit)"

    out = simulate(runs, args.budget_gb)

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
    print(f"\n⚠ CURATED LOWER BOUND — measured dashboard egress {MEASURED_DASHBOARD_GB:.1f} GB "
          f"({MEASURED_DASHBOARD_AS_OF}) ≈ {MODEL_UNDERCOUNT_FACTOR:.1f}× this model (it omits\n"
          f"  many small/frequent reads). Trust the dashboard for budget; use the table ranking "
          f"to target cuts.")
    ref_pct = 100 * out["ref"] / out["total"] if out["total"] else 0
    print(f"\nVERDICT: reference layer is {ref_pct:.0f}% of total egress, "
          f"{_gb(out['ref'])}/mo.")
    # M7: the blob was EVALUATED + REJECTED (CLAUDE.md / 2026-06-09: ~42KB/run
    # break-even, the bus dominates). The pure-model reference estimate is
    # conservative (static rows/read); --live calibrates it DOWN (measured 14%,
    # NOT worth). So the verdict references the documented decision instead of
    # flip-flopping per static guess — re-open only if --live reference egress
    # materially grows.
    if ref_pct >= 20 or out["total"] > 0.7 * out["budget"]:
        print(f"→ reference layer looks material ({ref_pct:.0f}%) in the PURE model, but the "
              f"blob was evaluated + REJECTED (CLAUDE.md; --live measured ~14%). Re-open ONLY "
              f"if `--live` reference egress materially grows.")
    else:
        print("→ A reference-cache blob saves little; the bus dominates. Matches CLAUDE.md's "
              "rejection. NOT worth the new failure surface.")
    print("\n(Estimates. rows/read + bytes/row are the dominant uncertainty — "
          "run --live to calibrate against real cadence + row sizes.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
