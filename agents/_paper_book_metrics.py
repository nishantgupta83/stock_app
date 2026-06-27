"""Pure metrics for the Paper Book forward-edge test. No DB, no network.

Gate = full $5k book equity (incl. idle cash at the risk-free rate) vs $5k QQQ
buy-and-hold from forward_epoch, cumulative + unannualized. OLS alpha/beta and
same-slot QQQ are diagnostics only (unstable at this n/sparsity)."""
from __future__ import annotations
import datetime as dt

TRADING_DAYS = 252

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
    curve, interest = {}, 0.0
    for day in days:
        idle = max(0.0, capital - _open_notional_on(positions, day))
        interest += idle * rf_daily
        realized = sum(float(p.get("realized_pnl") or 0) for p in positions
                       if p.get("status") == "closed" and p.get("closed_at")
                       and _d(p["closed_at"]) <= day)
        curve[day] = round(capital + realized + interest, 2)
    return curve


def qqq_buy_hold_curve(qqq_daily, days, capital, epoch) -> dict:
    base = qqq_daily.get(epoch)
    if base is None:
        base = next((qqq_daily[d] for d in days if d in qqq_daily), None)
    if not base:
        return {}
    return {day: round(capital * qqq_daily[day] / base, 2) for day in days if day in qqq_daily}


def max_drawdown(curve: dict) -> float:
    peak = None
    mdd = 0.0
    for day in sorted(curve):
        v = curve[day]
        peak = v if peak is None else max(peak, v)
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak)
    return round(mdd, 4)


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
