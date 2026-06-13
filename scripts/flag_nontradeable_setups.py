#!/usr/bin/env python3
"""flag_nontradeable_setups.py — C2 one-shot: retro-flag actionable
stock_trade_setups rows whose ticker is NOT a tradeable instrument
(index mutual funds, INST_* placeholders) with a reason_to_skip.

WHY: the C2 instrument guard only runs on NEW signals; setup rows are written
with ignore-duplicates and processed signals are filtered out of later runs, so
any pre-C2 actionable VTSAX/VFIAX/INST_* setup stays actionable forever and the
realistic loop could open it. This sweeps the existing backlog once.

Tradeable is the SAME definition the live guard uses (agents/_instruments).

USAGE (private shell, SUPABASE_URL + SUPABASE_SERVICE_KEY):
  python3 scripts/flag_nontradeable_setups.py            # DRY-RUN (default)
  python3 scripts/flag_nontradeable_setups.py --commit   # PATCH reason_to_skip
"""
from __future__ import annotations

import argparse
import os
import sys

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agents"))
import _instruments  # noqa: E402  shared tradeable definition

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
H = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true", help="actually PATCH (default: dry-run)")
    args = ap.parse_args()
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("ERROR: set SUPABASE_URL + SUPABASE_SERVICE_KEY.", file=sys.stderr); return 2

    tradeable = _instruments.fetch_tradeable_tickers(SUPABASE_URL, H)
    if tradeable is None:
        print("ABORT: could not fetch tradeable set (None) — refusing to flag on uncertain data.",
              file=sys.stderr); return 3
    print(f"tradeable instruments (stock/etf): {len(tradeable)}")

    rows = requests.get(f"{SUPABASE_URL}/rest/v1/stock_trade_setups", headers=H,
                        params={"select": "id,ticker,reason_to_skip",
                                "reason_to_skip": "is.null", "limit": "5000"},
                        timeout=30).json()
    bad = [r for r in rows if not _instruments.is_tradeable(r["ticker"], tradeable)]
    print(f"actionable setups: {len(rows)}; non-tradeable to flag: {len(bad)}")
    from collections import Counter
    for tk, n in Counter(r["ticker"] for r in bad).most_common():
        print(f"  {tk}: {n}")

    if not args.commit:
        print("\nDRY-RUN — nothing written. Re-run with --commit to flag.")
        return 0

    n_done = 0
    for r in bad:
        reason = f"{r['ticker']} not a tradeable instrument (fund/placeholder)"
        resp = requests.patch(f"{SUPABASE_URL}/rest/v1/stock_trade_setups",
                              headers={**H, "Content-Type": "application/json",
                                       "Prefer": "return=minimal"},
                              params={"id": f"eq.{r['id']}"},
                              json={"reason_to_skip": reason}, timeout=20)
        resp.raise_for_status(); n_done += 1
    print(f"\nFlagged {n_done} setups non-tradeable.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
