"""Pure metrics for the Paper Book forward-edge test. No DB, no network.

Gate = full $5k book equity (incl. idle cash at the risk-free rate) vs $5k QQQ
buy-and-hold from forward_epoch, cumulative + unannualized. OLS alpha/beta and
same-slot QQQ are diagnostics only (unstable at this n/sparsity)."""
from __future__ import annotations
import datetime as dt
import math

TRADING_DAYS = 252


def _finite(x):
    """Return x as a float, or None if it is missing or non-finite.

    yfinance emits NaN for gap days. `bool(float("nan")) is True`, so the usual
    falsy guard does NOT catch a NaN price — it flows through the buy-and-hold
    arithmetic into qqq_buy_hold_end / cumulative_excess and reaches the
    dashboard as "$nan" (observed live 2026-09-04). Worse, `NaN < 0` is False,
    so a NaN excess also slips past the `fail` branch in classify_tier. Every
    benchmark price crosses this helper so a missing bar stays MISSING instead
    of silently becoming a number-shaped hole.
    """
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None

TIERS = {
    "alive": {"min_cohorts": 30, "min_weeks": 8, "max_dd": 0.20},
    "edge":  {"min_cohorts": 50, "min_weeks": 13, "min_pf": 1.4, "min_subperiods_pos": 2},
}


def _d(x) -> dt.date:
    return dt.date.fromisoformat(str(x)[:10])


def independent_cohorts(positions: list[dict]) -> int:
    return len({_d(p["opened_at"]) for p in positions if p.get("opened_at")})


def _open_notional_on(positions, day) -> float:
    tot = 0.0
    for p in positions:
        if not p.get("opened_at"):
            continue
        o = _d(p["opened_at"])
        c = _d(p["closed_at"]) if p.get("closed_at") else None
        if o <= day and (c is None or day < c):
            tot += float(p.get("notional") or 0)
    return tot


def book_equity_curve(positions, days, capital, rf_annual) -> dict:
    rf_daily = rf_annual / TRADING_DAYS
    curve = {}
    interest = 0.0
    for i, day in enumerate(sorted(days)):
        realized = sum(float(p.get("realized_pnl") or 0) for p in positions
                       if p.get("status") == "closed" and p.get("closed_at")
                       and _d(p["closed_at"]) <= day)
        if i > 0:  # no interest on the baseline day; idle cash includes realized PnL
            idle = max(0.0, capital + realized - _open_notional_on(positions, day))
            interest += idle * rf_daily
        curve[day] = round(capital + realized + interest, 2)
    return curve


def qqq_buy_hold_curve(qqq_daily, days, capital, epoch) -> dict:
    base = _finite(qqq_daily.get(epoch))
    if base is None:                       # epoch bar missing OR NaN -> first finite bar
        base = next((px for d in days if (px := _finite(qqq_daily.get(d))) is not None), None)
    if base is None or base <= 0:
        return {}
    curve = {}
    for day in days:
        px = _finite(qqq_daily.get(day))
        if px is not None:                 # a NaN bar is a MISSING day, not a NaN value
            curve[day] = round(capital * px / base, 2)
    return curve


def max_drawdown(curve: dict) -> float:
    peak = None
    mdd = 0.0
    for day in sorted(curve):
        v = curve[day]
        peak = v if peak is None else max(peak, v)
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak)
    return round(mdd, 4)


def matched_endpoints(book_curve, qqq_curve, capital):
    """Book and benchmark end values on the LAST DAY BOTH ARE KNOWN.

    Dropping a non-finite benchmark bar can leave the two curves ending on
    different days. Reporting each curve's own last value would then print a
    book/QQQ pair whose difference is NOT the reported cumulative_excess (which
    is already matched-day). The pre-registration requires the benchmark share
    the same exit bar, so both endpoints come from the last common day.
    """
    common = sorted(set(book_curve) & set(qqq_curve))
    if not common:
        # No shared day => the benchmark end is UNKNOWN. Returning `capital`
        # here printed a QQQ figure that did not subtract to the reported
        # excess (which cumulative_excess defaults to 0.0). Report None rather
        # than fabricate a comparison.
        return (book_curve[max(book_curve)] if book_curve else capital), None
    last = common[-1]
    return book_curve[last], qqq_curve[last]


def cumulative_excess(book_curve, qqq_curve) -> float:
    days = sorted(set(book_curve) & set(qqq_curve))
    if not days:
        return 0.0
    last = days[-1]
    return round(book_curve[last] - qqq_curve[last], 2)


def profit_factor(closed) -> float:
    wins = sum(float(p.get("realized_pnl") or 0) for p in closed if (p.get("realized_pnl") or 0) > 0)
    losses = -sum(float(p.get("realized_pnl") or 0) for p in closed if (p.get("realized_pnl") or 0) < 0)
    if losses <= 0:
        return float("inf") if wins > 0 else 0.0
    return round(wins / losses, 4)


def top_cohort_excess_share(positions) -> float:
    by_day: dict[dt.date, float] = {}
    for p in positions:
        if p.get("status") == "closed" and p.get("opened_at"):
            k = _d(p["opened_at"])
            by_day[k] = by_day.get(k, 0.0) + float(p.get("realized_pnl") or 0)
    total = sum(by_day.values())
    if not by_day or abs(total) < 1e-9:
        return 0.0
    return round(max(by_day.values()) / total, 4)


def weeks_span(positions) -> float:
    ds = [_d(p["opened_at"]) for p in positions if p.get("opened_at")]
    if not ds:
        return 0.0
    return round((max(ds) - min(ds)).days / 7.0, 1)


def subperiods_positive(curve, halves=2) -> int:
    days = sorted(curve)
    if len(days) < halves + 1:
        return 0
    size = len(days) // halves
    pos = 0
    for h in range(halves):
        lo = days[h * size]
        hi = days[(h + 1) * size - 1] if h < halves - 1 else days[-1]
        if curve[hi] - curve[lo] > 0:
            pos += 1
    return pos


def beta_alpha(book_daily, qqq_daily_ret):
    n = len(book_daily)
    if n < 2 or len(qqq_daily_ret) != n:
        return (None, None)
    mb = sum(book_daily) / n
    mq = sum(qqq_daily_ret) / n
    var = sum((q - mq) ** 2 for q in qqq_daily_ret) / n
    if var == 0:
        return (None, None)
    cov = sum((book_daily[i] - mb) * (qqq_daily_ret[i] - mq) for i in range(n)) / n
    beta = cov / var
    return (round(beta, 4), round(mb - beta * mq, 6))


def classify_tier(fwd: dict, tiers=TIERS, sync_ok=True) -> dict:
    if not sync_ok:
        return {"status": "inconclusive", "reason": "sync_failed", "next": "alive"}
    a = tiers["alive"]
    cohorts = fwd.get("n_independent_cohorts", 0)
    weeks = fwd.get("weeks", 0)
    excess = fwd.get("cumulative_excess", 0.0)
    dd = fwd.get("max_drawdown", 0.0)
    top_share = abs(fwd.get("top_cohort_excess_share", 0.0))
    if cohorts < a["min_cohorts"] or weeks < a["min_weeks"]:
        return {"status": "inconclusive", "reason": "insufficient_sample",
                "next": "alive", "have_cohorts": cohorts, "need_cohorts": a["min_cohorts"],
                "have_weeks": weeks, "need_weeks": a["min_weeks"]}
    # No usable benchmark bar in THIS block's window => no verdict. Must be
    # checked explicitly: with an empty benchmark curve cumulative_excess
    # returns its 0.0 default, which is finite and not < 0, so both the
    # non-finite guard and the fail branch below would wave it through.
    # Fewer than TWO bars cannot express a benchmark return: with exactly one
    # (the epoch bar) both curves pin to that day, excess is 0.0 by construction,
    # and the fail branch below is skipped -> 'alive' on nothing (review C1).
    # `_block` always emits benchmark_days; a hand-built dict without it (tests,
    # legacy callers) is not evidence of a missing benchmark, so only a PRESENT
    # count below 2 withholds.
    bdays = fwd.get("benchmark_days")
    if bdays is not None and bdays < 2:
        return {"status": "inconclusive", "reason": "benchmark_unavailable", "next": "alive"}
    # Defense in depth: a non-finite excess must never reach a verdict. `NaN < 0`
    # is False, so without this the fail branch below is skipped and a book with
    # no usable benchmark reports "alive".
    if _finite(excess) is None:
        return {"status": "inconclusive", "reason": "benchmark_unavailable", "next": "alive"}
    if excess < 0 or dd > a["max_dd"]:
        return {"status": "fail", "reason": "negative_excess_or_drawdown",
                "excess": excess, "max_drawdown": dd}
    if top_share >= 1.0:
        return {"status": "inconclusive", "reason": "single_cohort_dominates",
                "top_cohort_share": top_share}
    status = "alive"
    e = tiers["edge"]
    if (cohorts >= e["min_cohorts"] and weeks >= e["min_weeks"] and excess > 0
            and fwd.get("profit_factor", 0) > e["min_pf"]
            and fwd.get("subperiods_positive", 0) >= e["min_subperiods_pos"]):
        status = "edge"
    return {"status": status, "excess": excess, "max_drawdown": dd,
            "cohorts": cohorts, "weeks": weeks}


def _block(sub, qqq_daily, days, capital, rf_annual, sync_ok) -> dict:
    closed = [p for p in sub if p.get("status") == "closed"]
    bcurve = book_equity_curve(sub, days, capital, rf_annual)
    qcurve = qqq_buy_hold_curve(qqq_daily, days, capital, days[0] if days else None)
    book_end, qqq_end = matched_endpoints(bcurve, qcurve, capital)
    return {
        "n_raw_trades": len(closed),
        "n_independent_cohorts": independent_cohorts([p for p in sub if p.get("status") == "closed"]),
        "weeks": weeks_span(sub),
        "book_equity_end": book_end,
        "qqq_buy_hold_end": qqq_end,
        # Usable benchmark bars IN THIS BLOCK'S window. classify_tier withholds
        # a verdict on 0 — a dict-wide "any finite price" check is NOT enough,
        # because finite pre-epoch bars satisfy it while the forward window is
        # entirely NaN (the live yfinance failure shape).
        "benchmark_days": len(qcurve),
        "cumulative_excess": cumulative_excess(bcurve, qcurve),
        "max_drawdown": max_drawdown(bcurve),
        "top_cohort_excess_share": top_cohort_excess_share(sub),
        "profit_factor": profit_factor(closed),
        "subperiods_positive": subperiods_positive(bcurve),
        "sync_ok": sync_ok,
    }


def compute_metrics(positions, qqq_daily, forward_epoch, capital,
                    sync_ok=True, rf_annual=0.05, tiers=TIERS) -> dict:
    epoch = _d(forward_epoch) if forward_epoch else None

    def is_fwd(p):
        return epoch and p.get("opened_at") and _d(p["opened_at"]) >= epoch

    fwd_days = sorted(d for d in qqq_daily if (not epoch) or d >= epoch)
    rep_days = sorted(d for d in qqq_daily if epoch and d < epoch)
    out = {
        "replay": _block([p for p in positions if not is_fwd(p)], qqq_daily, rep_days,
                         capital, rf_annual, sync_ok),
        "forward": _block([p for p in positions if is_fwd(p)], qqq_daily, fwd_days,
                          capital, rf_annual, sync_ok),
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    # An all-NaN benchmark is an ABSENT benchmark, not a usable one.
    if not any(_finite(v) is not None for v in qqq_daily.values()):
        out["tier"] = {"status": "inconclusive", "reason": "benchmark_unavailable"}
    else:
        out["tier"] = classify_tier(out["forward"], tiers, sync_ok)
    return out
