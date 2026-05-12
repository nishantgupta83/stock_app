"""
Crypto macro agent — closes the largest coverage gap surfaced by the
90-day market_scanner backfill (75% of >3% moves had no tracked event,
overwhelmingly on COIN / MSTR / IBIT — they move with BTC).

Once daily after US market close: fetch BTC-USD + ETH-USD daily closes
from yfinance. When abs(daily move) >= MOVE_THRESHOLD, emit one
crypto_macro_move event per crypto-correlated ticker with direction_prior
matching the move's sign. event_paper_agent then turns each into a paper
trade, and price_agent reconciles outcomes the next session — closing the
loop the same way as filings/earnings/news.

Run via .github/workflows/crypto_macro_agent.yml (cron 35 21 * * 1-5).
"""
from __future__ import annotations

import os
import sys
import time

import requests
import yfinance as yf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from filing_agent import (   # type: ignore
    job_run_start, job_run_finish, dead_letter,
    SUPABASE_URL, HEADERS_SB,
)

# 5% intraday close-to-close on BTC is roughly the threshold above which
# the COIN / MSTR derivatives consistently move 8-15% (high-beta names).
MOVE_THRESHOLD = 0.02

# Crypto-correlated tickers in our universe. COIN (Coinbase) is the most
# direct exchange exposure; MSTR holds 200k+ BTC on balance sheet; the
# others are listed in case you add ETFs later. Keep narrow — adding
# noise tickers dilutes per-rule calibration.
CRYPTO_CORRELATED = {
    "COIN":  "long",     # exchange volume scales with BTC volatility
    "MSTR":  "long",     # treasury company — direct BTC exposure
    # Add IBIT, MARA, RIOT, etc. once they're in your watchlist.
}

# yfinance crypto symbols
PROBES = {
    "BTC-USD": "btc",
    "ETH-USD": "eth",
}


def fetch_crypto_daily_move(symbol: str) -> tuple[float, str] | None:
    """Returns (daily_return, latest_close_iso). None if data unavailable."""
    try:
        df = yf.Ticker(symbol).history(period="5d", interval="1d", auto_adjust=False)
    except Exception as e:  # noqa: BLE001
        print(f"  yfinance {symbol}: {e}", file=sys.stderr)
        return None
    if df is None or df.empty or len(df) < 2:
        return None
    last = df.iloc[-1]
    prev = df.iloc[-2]
    try:
        last_close = float(last["Close"])
        prev_close = float(prev["Close"])
    except (KeyError, ValueError, TypeError):
        return None
    if prev_close <= 0:
        return None
    ret = (last_close - prev_close) / prev_close
    ts = df.index[-1]
    iso = ts.strftime("%Y-%m-%dT00:00:00+00:00")
    return ret, iso


def emit_macro_events(rows: list[dict]) -> int:
    """Insert with dedupe_key collision = silent skip. Re-runs on the same
    day overwrite no rows."""
    if not rows:
        return 0
    url = (
        f"{SUPABASE_URL}/rest/v1/stock_normalized_events"
        f"?on_conflict=dedupe_key"
    )
    headers = {**HEADERS_SB, "Prefer": "resolution=merge-duplicates,return=minimal"}
    r = requests.post(url, headers=headers, json=rows, timeout=20)
    if r.status_code not in (200, 201, 204):
        print(f"  events insert {r.status_code}: {r.text[:300]}", file=sys.stderr)
        return 0
    return len(rows)


def main() -> int:
    started = time.time()
    run_id = job_run_start("crypto_macro_agent")
    n_probes = n_significant = n_emitted = 0
    try:
        rows: list[dict] = []
        # Track the strongest move across BTC/ETH; when both move the same
        # direction, that's a stronger macro signal worth recording, but we
        # only emit on the first probe to keep one event per (ticker, day).
        triggered_for_today = False
        for symbol, label in PROBES.items():
            n_probes += 1
            res = fetch_crypto_daily_move(symbol)
            if res is None:
                continue
            ret, ts_iso = res
            print(f"  {symbol}: daily move {ret*100:+.2f}%")
            if abs(ret) < MOVE_THRESHOLD:
                continue
            n_significant += 1
            if triggered_for_today:
                continue            # already emitted today; don't double-fire
            direction = "long" if ret > 0 else "short"
            for ticker, base_dir in CRYPTO_CORRELATED.items():
                # base_dir is each ticker's correlation polarity to BTC. All current
                # entries are positively correlated, so we pass through `direction`.
                # If we ever add a SHORT-correlated vehicle, flip here.
                effective_dir = direction if base_dir == "long" else (
                    "short" if direction == "long" else "long"
                )
                rows.append({
                    "event_type":     "crypto_macro_move",
                    "event_subtype":  f"{label}_{direction}",
                    "ticker":         ticker,
                    "event_at":       ts_iso,
                    "severity":       3 if abs(ret) >= 0.10 else 2,
                    "source_table":   "yfinance_crypto",
                    "parser_confidence": 0.7,
                    "dedupe_key":     f"crypto_{label}_{ticker}_{ts_iso[:10]}",
                    "payload": {
                        "probe_symbol":   symbol,
                        "daily_return":   round(ret, 6),
                        "direction_prior": effective_dir,
                        "trigger_label":  label,
                    },
                })
            triggered_for_today = True

        n_emitted = emit_macro_events(rows)
        elapsed = time.time() - started
        print(f"DONE in {elapsed:.1f}s — {n_probes} probes, {n_significant} significant moves, "
              f"{n_emitted} events emitted")
        job_run_finish(run_id, "ok", n_probes, n_emitted)
        return 0

    except Exception as e:  # noqa: BLE001
        import traceback
        tb = traceback.format_exc()
        dead_letter("crypto_macro_agent", None, None, "top_level_failure", tb)
        job_run_finish(run_id, "failed", n_probes, n_emitted, err=str(e))
        print(f"FATAL: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
