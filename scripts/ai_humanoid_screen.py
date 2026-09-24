#!/usr/bin/env python3
"""ai_humanoid_screen — a daily screen over the Nasdaq-100 plus the AI / humanoid complex.

ISOLATED, like the semis brief: no Supabase reads or writes, no Telegram, no contact with
the frozen forward experiments or with Layers 2-5. It reads yfinance and StockTwits and
writes static JSON + HTML into ai_humanoid/ (a tracked folder at the repo root, like
paper_book/ and semis_brief/) and deploys THAT folder to its OWN Cloudflare Pages project.

Deliberately NOT the existing hub4apps-stock project: site_generator.yml:83 deploys the
whole of dist/ there, so publishing a subfolder to the same project would replace the
entire dashboard with this one page.

WHY THESE TWO SIGNALS, and not a momentum rank
----------------------------------------------
Measured on 26 AI/semis names over the 2023+ AI wave, forward 60 trading days, entry at the
next open (n=22,199 baseline observations):

    just owning the complex (the null)   +17.95%   69% positive
    %B < 0.20  (buy the dip)             +21.42%   73%   <- +3.47 pts vs null
    %B > 1.00  (buy strength)            +15.30%   66%   <- -2.65 pts
    top-quartile 12-month momentum       +14.94%   65%   <- -3.01 pts
    downtrend AND %B < 0.20              +25.43%   75%   <- +7.49 pts, the best bucket

Buying strength and buying momentum both LOST to simply holding. So this screen ranks by
%B ascending, not by momentum, even though "ride the wave" sounds like it should do the
opposite. Note the first line too: the null is enormous. Being in the complex mattered
about five times more than the entry timing did.

SIGNAL 2 exists because of Meta/Muse. Muse launched 2026-09-08 and META ran +20.3% before
the first analyst upgrade on 09-21 -- someone following that upgrade captured 2% of the
move. Replaying our rules over those sessions:

    card dip rule  %B<0.20    2026-08-21 @ 549.47  -> +35.4%   (18 days BEFORE the launch)
    move+volume spike         2026-09-09 @ 653.17  -> +13.9%   (the day AFTER the launch)
    follow the analyst        2026-09-21 @ 741.25  ->  +0.4%

The dip rule caught it early and by accident -- it was buying a name in its own drawdown
(META was 11.8% BELOW its 200-day that day), not predicting a product. The spike rule is
the one that reacts to a catalyst, and it is late but not useless. Both are reported, and
they are labelled differently so a catalyst is never mistaken for a setup.

Usage:  python scripts/ai_humanoid_screen.py [--out ai_humanoid] [--no-social]
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from datetime import date, datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import semis_brief as sb  # noqa: E402  (finite / destitch / bollinger / stocktwits)
from ai_humanoid_render import render  # noqa: E402

BAND_N, BAND_K = 20, 2.0
HOLD_DAYS = 504                    # 2 trading years, the operator's stated horizon
SPIKE_MOVE = 0.05                  # 5% daily move ...
SPIKE_VOL = 2.0                    # ... on 2x the 20-day average volume
DIP_PCT_B = 0.20
EXTENDED_PCT_B = 1.00
MIN_DOLLAR_VOLUME = 2_000_000      # below this, a scheduled buy needs limit orders
HOLD_MIN_POSITIVE = 0.80
HOLD_MIN_P10 = -0.10
MIN_INDEPENDENT_WINDOWS = 4        # 4 x 504 sessions ~ 8 years of history

# --- universe -----------------------------------------------------------------
# Tagged so the page can separate "the index" from "the theme". A name in both keeps
# both tags. NDX membership drifts; this list is a snapshot and is allowed to be stale
# by a few names -- the screen degrades gracefully when a ticker stops resolving.
NDX = """AAPL ABNB ADBE ADI ADP ADSK AEP ALNY AMAT AMD AMGN AMZN APP ARM ASML AVGO AXON
BKNG BKR BIIB CCEP CDNS CDW CEG CMCSA COST CPRT CRWD CSCO CSGP CSX CTAS DASH DDOG DXCM EA
EXE FANG FAST GEHC GFS GILD GOOGL HON IDXX INTC INTU ISRG KDP KHC KLAC LIN LITE LRCX MAR
MCHP MDLZ MELI META MNST MPWR MRNA MRVL MSFT MSTR MU NFLX NVDA NXPI ODFL ON ORLY PANW PAYX
PCAR PDD PEP PLTR PYPL QCOM REGN RKLB ROP ROST SBUX SHOP SNDK SNPS STX TER TMUS TRI TSLA
TTD TTWO TXN VRSK VRTX WBD WDAY WDC WMT XEL ZS""".split()

AI_HUMANOID = {
    # theme -> tickers. Everything here is US-listed and tradeable at retail size.
    "ai_compute":   ["NVDA", "AMD", "AVGO", "MRVL", "TSM", "ARM", "ALAB", "CRDO", "SMCI"],
    "ai_memory":    ["MU", "WDC", "STX", "SNDK"],
    "ai_equipment": ["AMAT", "LRCX", "KLAC", "ASML", "TER", "ONTO", "ENTG", "COHR"],
    "ai_power":     ["VRT", "CEG", "GEV", "PWR", "ETN"],
    "ai_network":   ["ANET", "CIEN", "LITE"],
    "ai_platform":  ["MSFT", "GOOGL", "META", "ORCL", "PLTR", "NOW"],
    # the humanoid supply chain, split by whether humanoid revenue is DISCLOSED or the
    # link is association only -- the distinction the research turned on.
    "humanoid_disclosed":   ["RRX", "ALGM", "JBL", "NOVT"],
    "humanoid_association": ["AME", "TEL", "APH", "PH", "ROK", "EMR", "ALNT", "ON", "NXPI"],
    "humanoid_oem":         ["TSLA", "CCXI", "SYM"],
    "humanoid_materials":   ["MP", "USAR", "ALB", "FCX"],
    "humanoid_etf":         ["KOID", "BOTT", "BOTZ", "ARKQ"],
    "benchmark":            ["VTI", "QQQ", "SPY", "SMH"],
    # Pinned sections the operator reads first, every day, regardless of verdict.
    "semis_etf":            ["SOXX", "SOXL", "SOXS"],
    # Daily-reset leveraged/inverse products. %B, the 200-day and every forward return on
    # this page were measured on ordinary long instruments; none of that transfers. SOXS is
    # inverse, so a low %B means the OPPOSITE of "on sale".
    "leveraged":            ["SOXL", "SOXS"],
    "inverse":              ["SOXS"],
    "megacap":              ["META", "GOOGL", "MSFT", "AAPL", "AMZN", "NFLX"],
}

# Rendered as their own sections at the top of the page, in this order, whatever their
# verdict is -- the operator tracks these daily and should not have to hunt for them
# across the verdict groups.
PINNED: list[tuple[str, str, str]] = [
    ("semis_etf", "Semis — SOXX / SOXL / SOXS",
     "The 3x pair moves with SOXX, so read SOXX's %B and size with SOXL/SOXS's ATR. "
     "SOXS is inverse: its %B means the opposite."),
    ("megacap", "Mega caps",
     "META, GOOGL, MSFT, AAPL, AMZN, NFLX — tracked every day whether or not they signal."),
]


def universe() -> dict[str, list[str]]:
    """ticker -> list of tags. NDX membership is a tag like any other."""
    tags: dict[str, list[str]] = {}
    for t in NDX:
        tags.setdefault(t, []).append("ndx100")
    for theme, names in AI_HUMANOID.items():
        for t in names:
            tags.setdefault(t, []).append(theme)
    return tags


# --- pure metrics -------------------------------------------------------------

def sma(xs: list[float], n: int) -> float | None:
    return sum(xs[-n:]) / n if len(xs) >= n else None


def pct_b(closes: list[float]) -> tuple[float | None, tuple | None]:
    bb = sb.bollinger(closes, BAND_N, BAND_K)
    if not bb or bb[-1] is None:
        return None, None
    m, u, lo = bb[-1]
    c = closes[-1]
    return (((c - lo) / (u - lo)) if u > lo else None), (m, u, lo)


def atr_pct(bars: list[dict], n: int = 14) -> float | None:
    """Wilder ATR as a share of the last close. Needs high/low/close."""
    rows = [b for b in bars
            if all(sb.finite(b.get(k)) is not None for k in ("high", "low", "close"))]
    if len(rows) < n + 1:
        return None
    val = None
    for i in range(1, len(rows)):
        pc = rows[i - 1]["close"]
        tr = max(rows[i]["high"] - rows[i]["low"],
                 abs(rows[i]["high"] - pc), abs(rows[i]["low"] - pc))
        val = tr if val is None else val + (tr - val) / n
    last = rows[-1]["close"]
    return (val / last) if (val is not None and last) else None


def hold_period(closes: list[float], horizon: int = HOLD_DAYS) -> dict:
    """Every start date -> forward return at `horizon`. The operator needs this money in
    1-3 years, so % positive and the 10th percentile are the gate, not the mean.

    The windows OVERLAP heavily: 10 years of daily bars gives ~2,000 windows of 504 days
    but only ~4 independent ones. Requiring `horizon + 60` admitted names with as few as
    60 windows carved from a single uninterrupted uptrend -- GEV reported 100% positive
    with a 10th percentile of +394% off 119 windows, which is one observation wearing a
    distribution's clothes. Require enough span for at least MIN_INDEPENDENT_WINDOWS
    non-overlapping windows, and report the independent count alongside the raw one."""
    if len(closes) < horizon * MIN_INDEPENDENT_WINDOWS:
        return {"n": 0, "n_independent": 0, "positive": None, "p10": None, "median": None}
    f = sorted((closes[i + horizon] / closes[i] - 1) for i in range(len(closes) - horizon))
    n = len(f)
    return {"n": n, "n_independent": len(closes) // horizon,
            "positive": sum(1 for x in f if x > 0) / n,
            "p10": f[int(n * 0.10)],
            "median": f[n // 2]}


def spike(bars: list[dict]) -> dict | None:
    """SIGNAL 2 — the catalyst reactor. A >=5% close-to-close move on >=2x the 20-day
    average volume. This is what fired on META the day AFTER Muse launched (+6.55% on
    2.07x volume) and it is explicitly a REACTION, not a setup."""
    rows = [b for b in bars if sb.finite(b.get("close")) and sb.finite(b.get("volume")) is not None]
    if len(rows) < 22:
        return None
    last, prev = rows[-1], rows[-2]
    move = last["close"] / prev["close"] - 1
    base = statistics.mean(b["volume"] for b in rows[-21:-1])
    mult = (last["volume"] / base) if base else None
    if abs(move) >= SPIKE_MOVE and mult is not None and mult >= SPIKE_VOL:
        return {"date": last["date"], "move": move, "vol_mult": mult}
    return None


def classify(row: dict, tags: list[str] | None = None) -> tuple[str, list[str]]:
    """(verdict, reasons). Gates in the order the decision card states them.

    Every input goes through finite() first: NaN is truthy and `nan < x` is False, so a
    NaN dollar_volume would sail through the liquidity gate and a NaN pct_b would land in
    the neutral bucket -- both failing OPEN, which is the wrong direction for a gate."""
    why = []
    tags = tags or row.get("tags") or []
    if "leveraged" in tags:
        # Never hand a daily-reset 3x product a dip verdict. The +3.47/+7.49 pt figures are
        # long-only cash-equity statistics; applying them here would be the same category
        # error as reading SOXS's low %B as "on sale" when it is the inverse leg.
        return "leveraged", [
            "daily-reset leveraged product — the %B and forward-return thresholds on this "
            "page were measured on ordinary long instruments and do not transfer"
            + (" · INVERSE: a low %B here means the underlying is STRONG" if "inverse" in tags else "")]
    dv = sb.finite(row.get("dollar_volume"))
    if dv is None or dv < MIN_DOLLAR_VOLUME:
        return "illiquid", ["below the liquidity gate — limit orders only, size in days-to-exit"]
    pb = sb.finite(row.get("pct_b"))
    if pb is None:
        return "no_data", ["not enough history for a band reading"]
    # A split yfinance failed to adjust survives SPLIT_RATIO_RANGE (0.25, 4.0) and lands as
    # a -50% day with %B far below zero -- which would read as the deepest dip on the page.
    chg = sb.finite(row.get("chg"))
    if (chg is not None and abs(chg) > 0.35) or pb < -2.0:
        return "no_data", [f"implausible bar (1d {chg if chg is None else f'{chg*100:+.0f}%'}, "
                           f"%B {pb:.2f}) — suspect an unadjusted split"]
    h = row.get("hold") or {}
    if h.get("positive") is None:
        return "no_hold_data", ["hold gate not measurable — under "
                                f"{MIN_INDEPENDENT_WINDOWS} independent 2-year windows of history"]
    hold_ok = (h["positive"] >= HOLD_MIN_POSITIVE
               and sb.finite(h.get("p10")) is not None and h["p10"] >= HOLD_MIN_P10)
    if pb < DIP_PCT_B:
        why.append(f"%B {pb:.2f} — the measured buy band (+3.47 pts vs null @60d on the AI wave)")
        if sb.finite(row.get("vs_sma200")) is not None and row["vs_sma200"] < 0:
            why.append("and below its 200-day — the best-testing bucket (+7.49 pts)")
        if not hold_ok:
            why.append("but it FAILS the 1-3 year hold gate")
            return "dip_but_fails_hold", why
        return "buy_zone", why
    if pb > EXTENDED_PCT_B:
        return "extended", [f"%B {pb:.2f} — the stall zone (−2.65 pts vs null @60d)"]
    return "neutral", [f"%B {pb:.2f} — no measured edge either way"]


# --- IO -----------------------------------------------------------------------

def fetch(tickers: list[str]) -> dict[str, list[dict]]:
    """Daily bars, split-corrected, strictly before today. Batched."""
    import warnings
    warnings.filterwarnings("ignore")
    import yfinance as yf
    today = datetime.now(sb.PT).date()
    raw = yf.download(tickers, period="10y", interval="1d", auto_adjust=True,
                      group_by="ticker", threads=True, progress=False)
    out = {}
    for t in tickers:
        try:
            df = raw[t].dropna(how="all")
        except Exception:
            continue
        rows = [{"date": ts.date().isoformat(),
                 "open": sb.finite(r["Open"]) or sb.finite(r["Close"]),
                 "high": sb.finite(r["High"]), "low": sb.finite(r["Low"]),
                 "close": sb.finite(r["Close"]), "volume": sb.finite(r["Volume"])}
                for ts, r in df.iterrows()
                if ts.date() < today and sb.finite(r["Close"])]
        if len(rows) < 60:
            continue
        # carry high/low/volume through the split rescale (volume moves the other way)
        fixed = sb.destitch([{"date": r["date"], "open": r["open"], "close": r["close"]} for r in rows])
        for src, fx in zip(rows, fixed):
            f = (fx["close"] / src["close"]) if src["close"] else 1.0
            fx["high"] = src["high"] * f if sb.finite(src["high"]) else None
            fx["low"] = src["low"] * f if sb.finite(src["low"]) else None
            fx["volume"] = (src["volume"] / f) if (sb.finite(src["volume"]) is not None and f) else None
        out[t] = fixed
    return out


def build(tickers: dict[str, list[str]], bars: dict[str, list[dict]], social: bool) -> dict:
    rows = []
    for t, tags in tickers.items():
        b = bars.get(t)
        if not b:
            continue
        closes = [x["close"] for x in b if sb.finite(x.get("close"))]
        if len(closes) < BAND_N + 1:
            continue
        pb, band = pct_b(closes)
        s20, s200 = sma(closes, 20), sma(closes, 200)
        w = closes[-252:]
        dv = [x["close"] * x["volume"] for x in b[-60:]
              if sb.finite(x.get("close")) and sb.finite(x.get("volume")) is not None]
        row = {
            "ticker": t, "tags": tags, "as_of": b[-1]["date"], "close": closes[-1],
            "chg": (closes[-1] / closes[-2] - 1) if len(closes) > 1 else None,
            "pct_b": pb,
            "band": {"mid": band[0], "upper": band[1], "lower": band[2]} if band else None,
            "vs_sma20": (closes[-1] / s20 - 1) if s20 else None,
            "vs_sma200": (closes[-1] / s200 - 1) if s200 else None,
            "range_pos": ((closes[-1] - min(w)) / (max(w) - min(w))) if (len(w) >= 60 and max(w) > min(w)) else None,
            "range_n": len(w) if len(w) >= 60 else None,
            "atr_pct": atr_pct(b),
            "dollar_volume": statistics.median(dv) if dv else None,
            "hold": hold_period(closes),
            "spike": spike(b),
        }
        row["verdict"], row["why"] = classify(row, tags)
        rows.append(row)

    # yfinance returns NaN for a real trading day on a PER-TICKER basis, so rows carry
    # different bar dates. Today 98 of 144 were a session older than the newest -- and the
    # page showed ONE as_of over all of them. Mark each row against the freshest bar.
    freshest = max((r["as_of"] for r in rows), default=None)
    for r in rows:
        r["stale"] = (r["as_of"] != freshest)
    rows.sort(key=lambda r: (r["pct_b"] is None, r["pct_b"] if r["pct_b"] is not None else 9))
    if social:
        now = datetime.now(timezone.utc)
        for r in rows[:14] + [x for x in rows if x.get("spike")][:8]:
            if "social" in r:
                continue
            s = sb.stocktwits(r["ticker"], now)
            # the tagged bull/bear ratio is near-useless (9 of 20 names read 100% bullish
            # in a live pull); message VOLUME is the part that carries information.
            r["social"] = {"messages_12h": s["last12h"], "bull": s["bull"], "bear": s["bear"]} if s else None
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "as_of": freshest,
        "n_stale": sum(1 for r in rows if r["stale"]),
        "n_universe": len(tickers), "n_resolved": len(rows),
        "thresholds": {"dip_pct_b": DIP_PCT_B, "extended_pct_b": EXTENDED_PCT_B,
                       "spike_move": SPIKE_MOVE, "spike_vol": SPIKE_VOL,
                       "min_dollar_volume": MIN_DOLLAR_VOLUME,
                       "hold_min_positive": HOLD_MIN_POSITIVE, "hold_min_p10": HOLD_MIN_P10},
        "rows": rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="ai_humanoid")
    ap.add_argument("--no-social", action="store_true")
    a = ap.parse_args()

    tags = universe()
    print(f"universe: {len(tags)} tickers ({len(NDX)} NDX + theme overlay)", file=sys.stderr)
    bars = fetch(sorted(tags))
    print(f"resolved: {len(bars)}", file=sys.stderr)
    data = build(tags, bars, social=not a.no_social)

    out = REPO / a.out
    out.mkdir(parents=True, exist_ok=True)
    (out / "screen.json").write_text(json.dumps(data, indent=1, default=str))
    (out / "index.html").write_text(render(data))
    buys = [r for r in data["rows"] if r["verdict"] == "buy_zone"]
    spikes = [r for r in data["rows"] if r.get("spike")]
    print(f"wrote {out}/screen.json + index.html — {len(buys)} in the buy zone, "
          f"{len(spikes)} catalyst spikes", file=sys.stderr)
    for r in buys[:12]:
        print(f"  BUY  {r['ticker']:<6} %B {r['pct_b']:.2f}  vs200d "
              f"{(r['vs_sma200'] or 0)*100:+6.1f}%  hold {(r['hold']['positive'] or 0)*100:.0f}%",
              file=sys.stderr)
    for r in spikes:
        print(f"  SPIKE {r['ticker']:<6} {r['spike']['move']*100:+.1f}% on "
              f"{r['spike']['vol_mult']:.1f}x volume", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
