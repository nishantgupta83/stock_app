#!/usr/bin/env python3
"""Reconstruct the last N trading days of the 6:15 AM PT brief (price-based parts only)
and grade each against what actually happened.

What CAN be rebuilt as-of 06:15 PT (09:15 ET) each morning: the top-10 implied SOXX open
(5m premarket bars, ~60 days of history), SOXL/SOXS premarket, Asia's closes (TSMC, SK
Hynix, Samsung, Tokyo Electron — all closed before 09:15 ET), and Nasdaq futures.
What CANNOT: news, filings, Truth Social, StockTwits (those feeds only return the present),
and ASML (Amsterdam is mid-session at 09:15 ET — using its daily close would be lookahead).
Weights are today's SOXX top-10 (they drift slowly; noted, not corrected).

Output: semis_brief/backfill/backfill.json — kept SEPARATE from the live calls/grades so
the forward scorecard stays clean. Uses the same pure functions as the live brief.
"""
from __future__ import annotations

import json
import statistics
import sys
from datetime import date, datetime, time, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import semis_brief as sb  # noqa: E402

CUTOFF_ET = time(9, 15)       # == 06:15 PT
ASIA = ["2330.TW", "000660.KS", "005930.KS", "8035.T"]


def main(n_days: int = 20) -> int:
    yf = sb._yf()
    weights, wsrc = sb.soxx_weights()
    names = list(weights) + sb.ETFS

    intraday, daily = {}, {}
    for t in names + ["NQ=F"]:
        h = yf.Ticker(t).history(period="60d", interval="5m", prepost=True, auto_adjust=False)
        intraday[t] = [(ts.to_pydatetime(), r["Close"]) for ts, r in h.iterrows()]
    for t in names + ASIA:
        daily[t] = sb.daily_bars(t, "6mo")

    today = datetime.now(sb.PT).date()
    days = sorted(d for d in daily["SOXX"] if d < today and sb.is_trading_day(d)
                  and sb.finite(daily["SOXX"][d].get("close")))[-n_days:]
    rows = []
    for D in days:
        prev = sb.previous_trading_day(D)
        cutoff = datetime.combine(D, CUTOFF_ET, sb.ET).astimezone(timezone.utc)
        pm = {}
        for t in names:
            pc = sb.finite(daily[t].get(prev, {}).get("close"))
            last = sb.premarket_last(intraday[t], D, cutoff)
            pm[t] = (last[0] / pc - 1) if (pc and last and not last[2]) else None
        imp = sb.implied_open(weights, {t: pm[t] for t in weights})
        implied = imp["implied_soxx"]
        bias = sb.bias_from(implied)

        asia = []
        for t in ASIA:
            b = daily[t]
            ks = sorted(k for k in b if k <= D and sb.finite(b[k].get("close")))
            if len(ks) >= 2 and ks[-1] == D:
                asia.append(b[ks[-1]]["close"] / b[ks[-2]]["close"] - 1)
        nq_prev = [p for ts, p in intraday["NQ=F"]
                   if ts.astimezone(sb.ET).date() == prev and ts.astimezone(sb.ET).time() <= time(15, 55)
                   and sb.finite(p)]
        nq_now = sb.premarket_last(intraday["NQ=F"], D, cutoff)
        nq = (nq_now[0] / nq_prev[-1] - 1) if (nq_now and nq_prev) else None

        call = {"date": D.isoformat(), "bias": bias, "implied_soxx": implied,
                "implied_soxl": 3 * implied if implied is not None else None,
                "levels": {"soxl_prior_close": sb.finite(daily["SOXL"].get(prev, {}).get("close"))},
                "components": {"overnight_asia": statistics.mean(asia) if asia else None,
                               "nasdaq_futures": nq}}
        soxx_1030 = None
        for ts, p in intraday["SOXX"]:
            e = ts.astimezone(sb.ET)
            if e.date() == D and e.time() == time(10, 25):
                soxx_1030 = sb.finite(p)
        g = sb.grade_day(call, daily["SOXX"].get(D, {}), daily["SOXL"].get(D, {}),
                         daily["SOXS"].get(D, {}), soxx_1030)
        rows.append({"call": call, "grade": g, "soxl_premarket": pm.get("SOXL"),
                     "soxs_premarket": pm.get("SOXS"), "n_holdings_used": imp["n_used"]})

    grades = [r["grade"] for r in rows]
    sc = sb.scorecard(grades, {g["date"] for g in grades})     # same scoring code as live
    graded_dates = [g["date"] for g in grades if g.get("status") == "graded"]
    summary = {"first": graded_dates[0], "last": graded_dates[-1],
               "n_days": sc["n_graded"], "follow": sc["all"]["bias"], "fade": sc["all"]["fade_called"],
               "follow_compounded": sc["all"]["follow_compounded"],
               "fade_compounded": sc["all"]["fade_compounded"]}
    out = sb.STATE / "backfill" / "backfill.json"
    sb.write_json(out, {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "weights_source": wsrc, "cutoff_et": "09:15",
                        "note": "reconstructed, price-based components only; not part of the live scorecard",
                        "summary": summary, "scorecard": sc, "days": rows})

    # ---- report
    def p(x, d=2):
        return "   n/a" if x is None else f"{x*100:+6.{d}f}%"
    lab = {"lean_soxl": "SOXL", "lean_soxs": "SOXS", "no_edge": "  --"}
    print(f"{'date':<11}{'call':>5}{'implied':>9}{'SOXLpre':>9}{'Asia':>8}{'NQ':>8} | "
          f"{'SOXXo→c':>8}{'SOXLo→c':>9}{'SOXSo→c':>9}{'gapFill':>8}{'hit':>6}")
    for r in rows:
        c, g = r["call"], r["grade"]
        hit = {True: "  ✓", False: "  ✗", None: "  ·"}[g.get("bias_hit")] if g.get("status") == "graded" else g.get("status")
        print(f"{c['date']:<11}{lab[c['bias']]:>5}{p(c['implied_soxx'])}{p(r['soxl_premarket'],1):>9}"
              f"{p(c['components']['overnight_asia'],1):>8}{p(c['components']['nasdaq_futures'],1):>8} | "
              f"{p(g.get('soxx_open_to_close'))}{p(g.get('soxl_open_to_close'),1):>9}{p(g.get('soxs_open_to_close'),1):>9}"
              f"{str(g.get('gap_filled'))[:5]:>8}{hit:>6}")

    gr = [r["grade"] for r in rows if r["grade"].get("status") == "graded"]
    called = [g for g in gr if g["bias"] != "no_edge"]
    hits = [g["bias_hit"] for g in called if g["bias_hit"] is not None]
    comp = {k: [g["component_hits"].get(k) for g in gr] for k in ("overnight_asia", "nasdaq_futures")}

    def rate(v):
        v = [x for x in v if x is not None]
        return f"{sum(v)}/{len(v)} = {sum(v)/len(v)*100:.0f}%" if v else "n/a"

    def comp_ret(vals):
        eq = 1.0
        for v in vals:
            eq *= (1 + v - 0.001)   # 10 bps round trip
        return eq - 1
    follow = []
    for g in called:
        if g["bias"] == "lean_soxl" and g.get("soxl_open_to_close") is not None:
            follow.append(g["soxl_open_to_close"])
        if g["bias"] == "lean_soxs" and g.get("soxs_open_to_close") is not None:
            follow.append(g["soxs_open_to_close"])
    fade = []
    for g in called:
        if g["bias"] == "lean_soxl" and g.get("soxs_open_to_close") is not None:
            fade.append(g["soxs_open_to_close"])
        if g["bias"] == "lean_soxs" and g.get("soxl_open_to_close") is not None:
            fade.append(g["soxl_open_to_close"])
    all_l = [g["soxl_open_to_close"] for g in gr if g.get("soxl_open_to_close") is not None]
    all_s = [g["soxs_open_to_close"] for g in gr if g.get("soxs_open_to_close") is not None]
    ups = [g["outcome_sign"] > 0 for g in gr if g["outcome_sign"]]
    fills = [g["gap_filled"] for g in gr if g.get("gap_filled") is not None]
    errs = [abs(g["soxl_open_error"]) for g in gr if g.get("soxl_open_error") is not None]
    print(f"\n{len(gr)} days graded · {len(called)} called ({sum(1 for g in called if g['bias']=='lean_soxl')} SOXL,"
          f" {sum(1 for g in called if g['bias']=='lean_soxs')} SOXS) · {len(gr)-len(called)} no edge")
    print(f"bias direction hit (SOXX open→close):  {rate(hits)}")
    print(f"up-day base rate, all days:            {rate(ups)}")
    print(f"fade-the-gap, all days:                {rate([g.get('fade_gap_hit') for g in gr])}")
    print(f"component hits — Asia overnight {rate(comp['overnight_asia'])} · Nasdaq futures {rate(comp['nasdaq_futures'])}")
    print(f"gap filled intraday:                   {rate(fills)}")
    print(f"SOXL implied-open error (mean |err|):  {statistics.mean(errs)*100:.2f}%" if errs else "")
    print(f"\ncompounded open→close, 10 bps/round trip, called days only:")
    print(f"  follow the call (SOXL on lean-SOXL, SOXS on lean-SOXS): {comp_ret(follow)*100:+.1f}%  (n={len(follow)})")
    print(f"  do the opposite:                                         {comp_ret(fade)*100:+.1f}%  (n={len(fade)})")
    print(f"every day, all {len(all_l)} days:  always SOXL {comp_ret(all_l)*100:+.1f}%  ·  always SOXS {comp_ret(all_s)*100:+.1f}%")
    print(f"\nsaved: {out.relative_to(sb.REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 20))
