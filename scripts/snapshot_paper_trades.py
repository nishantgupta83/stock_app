#!/usr/bin/env python3
"""One-time (re-runnable) LOCAL snapshot of closed paper trades + their price bars.

Purpose: pay the Supabase read-egress ONCE, then re-grade calibration OFFLINE under
any exit policy with zero further DB dependency (see scripts/regrade_calibration.py).
This is the "save locally so a future algorithm can re-grade cheaply" step.

Writes under REGRADE_DIR (default ./regrade_data, gitignored):
  trades.jsonl        — one closed stock_event_paper_trades row per line
  bars/<TICKER>.json  — {"YYYY-MM-DD": [open, high, low, close], ...} (stock_raw_prices)
  manifest.json       — captured_at, counts, tickers, date span

Read-only against Supabase. Re-run anytime to refresh. Needs SUPABASE_URL +
SUPABASE_SERVICE_KEY (run in a private shell — see CLAUDE.md key handling).

Usage:
  python3 scripts/snapshot_paper_trades.py            # all closed trades
  RULE=8k_material_event::h30d python3 scripts/snapshot_paper_trades.py
"""
from __future__ import annotations
import json, os, sys, urllib.request, datetime as dt
from pathlib import Path

URL = os.environ["SUPABASE_URL"].rstrip("/")
KEY = os.environ["SUPABASE_SERVICE_KEY"]
OUT = Path(os.environ.get("REGRADE_DIR", "regrade_data"))
RULE = os.environ.get("RULE")  # optional rule_key filter; default = all rules

TRADE_COLS = ("id,ticker,entry_at,entry_price,direction,horizon_days,target_pct,"
              "stop_pct,rule_key,realized_return,correct,exit_at,exit_price,"
              "mfe_pct,mae_pct,target_hit,stop_hit")


def fetch(path: str) -> list[dict]:
    rows, off = [], 0
    while True:
        req = urllib.request.Request(
            f"{URL}/rest/v1/{path}",
            headers={"apikey": KEY, "Authorization": f"Bearer {KEY}",
                     "Range": f"{off}-{off+999}"})
        page = json.load(urllib.request.urlopen(req))
        rows += page
        if len(page) < 1000:
            return rows
        off += 1000


def main() -> int:
    q = f"stock_event_paper_trades?status=eq.closed&select={TRADE_COLS}"
    if RULE:
        q += f"&rule_key=eq.{RULE}"
    print(f"fetching closed trades{' for ' + RULE if RULE else ' (all rules)'} ...")
    trades = [t for t in fetch(q) if t.get("entry_at") and t.get("ticker")]
    print(f"  {len(trades)} closed trades")

    tickers = sorted({t["ticker"] for t in trades})
    entries = [t["entry_at"][:10] for t in trades]
    lo = min(entries); hi = max(entries)
    # bar window: earliest entry .. latest entry + 45d (covers the longest horizon)
    bar_hi = (dt.date.fromisoformat(hi) + dt.timedelta(days=45)).isoformat()

    (OUT / "bars").mkdir(parents=True, exist_ok=True)
    with (OUT / "trades.jsonl").open("w") as f:
        for t in trades:
            f.write(json.dumps(t) + "\n")

    print(f"fetching bars for {len(tickers)} tickers ({lo}..{bar_hi}) ...")
    bar_rows_total = 0
    for i, tk in enumerate(tickers, 1):
        rows = fetch(f"stock_raw_prices?ticker=eq.{tk}&ts=gte.{lo}&ts=lte.{bar_hi}"
                     "&select=ts,open,high,low,close&order=ts.asc")
        out = {}
        for r in rows:
            try:
                out[r["ts"][:10]] = [float(r["open"]), float(r["high"]),
                                     float(r["low"]), float(r["close"])]
            except (TypeError, ValueError, KeyError):
                continue
        (OUT / "bars" / f"{tk}.json").write_text(json.dumps(out))
        bar_rows_total += len(out)
        if i % 25 == 0:
            print(f"  {i}/{len(tickers)} tickers")

    manifest = {
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "rule_filter": RULE,
        "n_trades": len(trades),
        "n_tickers": len(tickers),
        "n_bar_rows": bar_rows_total,
        "entry_span": [lo, hi],
        "bar_window": [lo, bar_hi],
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"snapshot written to {OUT}/  ({len(trades)} trades, {bar_rows_total} bars)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
