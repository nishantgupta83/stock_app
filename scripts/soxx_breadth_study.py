#!/usr/bin/env python3
"""Reproduce every number quoted in the "What is moving SOXX" section of the ai_humanoid page.

Standalone, read-only, no Supabase, no Telegram. Prints a table and exits 0.

    python3 scripts/soxx_breadth_study.py

Questions answered
------------------
1. Does the breadth of SOXX's top-10 holdings predict SOXX's next move?  (page says: no)
2. Does SOXX's OWN Bollinger %B?  (page says: yes, in two specific places)

Method
------
- Daily bars, auto-adjusted, entry at the NEXT session's open, exit at the close h sessions
  later (h = 1, 5, 20). No lookahead: every signal uses bars up to and including day t and
  the return starts at the open of t+1.
- Every conditional result is printed beside the UNCONDITIONAL mean over the same sample
  (the "null"), and the reported edge is the difference. A conditional number without its
  null is meaningless.
- Breadth   = share of top-10 fund weight sitting above its own 20-day SMA.
- Composite = fund-weighted mean of the ten holdings' Bollinger %B.
- %B        = (close - lower) / (upper - lower), 20-day, 2 sigma, population std (ddof=0).

Caveats, stated rather than buried
----------------------------------
- Overlapping windows: at h = 20 consecutive signals share 19 of 20 days, so the raw n
  overstates the number of independent observations several-fold. Treat the edges as
  suggestive, not as significance tests.
- The top-10 list and weights are TODAY's holdings applied to history (survivorship: NVDA and
  MU were not always the top two). That biases the breadth test toward finding an edge, so a
  null result here is if anything conservative.
- The window slides with the run date, so figures move slightly from run to run. The page
  quotes the values at the time it was written; re-run this to refresh them.
"""
from __future__ import annotations

import sys
import warnings

warnings.filterwarnings("ignore")

TOP10 = {"NVDA": 9.43, "MU": 8.91, "AMD": 8.23, "AVGO": 7.48, "INTC": 5.09,
         "MRVL": 4.66, "TSM": 4.65, "AMAT": 4.59, "LRCX": 4.27, "KLAC": 4.12}
N, K = 20, 2.0


def _pct_b(s):
    m = s.rolling(N).mean()
    sd = s.rolling(N).std(ddof=0)
    return (s - (m - K * sd)) / ((m + K * sd) - (m - K * sd))


def _table(title, cond, fwd, horizons):
    import numpy as np
    print(f"\n{title}")
    print(f"{'condition':<38}{'h':>4}{'n':>7}{'mean':>9}{'% pos':>8}{'null':>9}{'edge':>9}")
    for h in horizons:
        f = fwd[h]
        for label, m in cond:
            ok = f.notna() & m.reindex(f.index).fillna(False)
            v = f[ok]
            base = f.dropna()
            if len(v) < 30:
                continue
            print(f"{label:<38}{h:>4}{len(v):>7,}{v.mean()*100:>+8.2f}%{(v > 0).mean()*100:>7.0f}%"
                  f"{base.mean()*100:>+8.2f}%{(v.mean()-base.mean())*100:>+8.2f}")


def main() -> int:
    import numpy as np
    import pandas as pd
    import yfinance as yf

    HZ = (1, 5, 20)
    raw = yf.download(list(TOP10) + ["SOXX"], period="10y", interval="1d",
                      auto_adjust=True, group_by="ticker", progress=False)
    cl = {t: raw[t]["Close"].dropna() for t in list(TOP10) + ["SOXX"]}
    op = raw["SOXX"]["Open"]
    sx = cl["SOXX"]
    idx = sx.index
    print(f"SOXX study — {idx[0].date()} .. {idx[-1].date()}  ({len(idx):,} sessions), entry next open")

    def fwd_returns(index, closes, opens):
        return {h: (closes.shift(-h) / opens.shift(-1) - 1).reindex(index) for h in HZ}

    # ---- A. SOXX's own %B, full 10 years  (the "stall, not a short" claim)
    pb = _pct_b(sx)
    _table("A. SOXX own %B, ~10 years", [
        ("%B > 1.00  (above upper band)", pb > 1.0),
        ("%B < 0.20  (dip band)", pb < 0.20),
    ], fwd_returns(idx, sx, op), HZ)

    # ---- B. the last 8 years: own %B vs top-10 breadth (the page's headline comparison)
    cut = idx[-1] - pd.DateOffset(years=8)
    i8 = idx[idx >= cut]
    wt = pd.Series(TOP10) / sum(TOP10.values())
    above = pd.DataFrame({t: (cl[t] > cl[t].rolling(20).mean()).reindex(idx).astype(float)
                          for t in TOP10})
    breadth = (above * wt).sum(axis=1)
    comp = (pd.DataFrame({t: _pct_b(cl[t]).reindex(idx) for t in TOP10}) * wt).sum(axis=1)
    f8 = {h: v.reindex(i8) for h, v in fwd_returns(idx, sx, op).items()}
    valid = breadth.notna() & comp.notna() & pb.notna()
    _table(f"B. last 8 years ({i8[0].date()} ..) — does top-10 breadth predict SOXX?", [
        ("breadth > 0.8 (broad strength)", (breadth > 0.8) & valid),
        ("breadth < 0.2 (broad weakness)", (breadth < 0.2) & valid),
        ("composite top-10 %B < 0.20", (comp < 0.20) & valid),
        ("composite top-10 %B > 1.00", (comp > 1.00) & valid),
        ("SOXX OWN %B < 0.20", (pb < 0.20) & valid),
    ], f8, HZ)

    print("\nRead the EDGE column, not this line: SOXX's own %B < 0.20 is the strongest at 20d;")
    print("broad STRENGTH (breadth > 0.8) sits at ~0; weakness rows are positive but smaller and rest")
    print("on a few hundred overlapping observations. Page copy: ai_humanoid_render._soxx_section.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
