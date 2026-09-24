"""Pure methodology primitives for the AI/humanoid quarterly-rotation research.

This module intentionally has no network, Supabase, filesystem, or production-screen
side effects. The existing ai_humanoid_screen remains the Strategy-0 reference.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from statistics import quantiles
from typing import Sequence


@dataclass(frozen=True)
class RotationConfig:
    band_window: int = 20
    band_sigma: float = 2.0
    dip_pct_b: float = 0.20
    extended_pct_b: float = 1.00
    sma_window: int = 200
    hold_horizon: int = 504
    min_independent_windows: int = 4
    hold_positive_floor: float = 0.80
    hold_p10_floor: float = -0.10


@dataclass(frozen=True)
class HoldStats:
    raw_windows: int
    independent_windows: int
    positive_rate: float | None
    p10: float | None
    median: float | None


def bollinger_pct_b(closes: Sequence[float], window: int = 20, sigma: float = 2.0) -> float | None:
    if len(closes) < window:
        return None
    w = list(closes[-window:])
    mean = sum(w) / window
    variance = sum((x - mean) ** 2 for x in w) / window
    sd = sqrt(variance)
    upper = mean + sigma * sd
    lower = mean - sigma * sd
    if upper <= lower:
        return None
    return (w[-1] - lower) / (upper - lower)


def sma(closes: Sequence[float], window: int) -> float | None:
    if len(closes) < window:
        return None
    return sum(closes[-window:]) / window


def hold_stats(closes: Sequence[float], horizon: int = 504,
               min_independent_windows: int = 4) -> HoldStats:
    """Historical forward-return distribution using overlapping observations.

    The raw sample is retained for distributional detail, but independent window count is
    reported separately and is the minimum-history gate. This prevents a long uninterrupted
    uptrend from masquerading as hundreds of independent observations.
    """
    if len(closes) < horizon * min_independent_windows:
        return HoldStats(0, 0, None, None, None)
    returns = sorted(closes[i + horizon] / closes[i] - 1 for i in range(len(closes) - horizon))
    n = len(returns)
    p10 = returns[max(0, min(n - 1, int(n * 0.10)))]
    return HoldStats(
        raw_windows=n,
        independent_windows=len(closes) // horizon,
        positive_rate=sum(r > 0 for r in returns) / n,
        p10=p10,
        median=returns[n // 2],
    )


def hold_gate(stats: HoldStats, cfg: RotationConfig) -> bool:
    return (
        stats.independent_windows >= cfg.min_independent_windows
        and stats.positive_rate is not None
        and stats.positive_rate >= cfg.hold_positive_floor
        and stats.p10 is not None
        and stats.p10 >= cfg.hold_p10_floor
    )


def quarterly_dates(dates: Sequence[str]) -> list[str]:
    """Return the last available observation in each calendar quarter."""
    if not dates:
        return []
    chosen: dict[tuple[int, int], str] = {}
    for d in sorted(dates):
        y, m, _ = (int(x) for x in d[:10].split("-"))
        q = (m - 1) // 3 + 1
        chosen[(y, q)] = d[:10]
    return list(chosen.values())


def next_open_entry(rebalance_date: str, sessions: Sequence[str]) -> str | None:
    """First available session strictly after the decision date."""
    target = rebalance_date[:10]
    for d in sorted(sessions):
        if d[:10] > target:
            return d[:10]
    return None
