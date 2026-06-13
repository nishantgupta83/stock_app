"""Single, env-free source of truth for the v1 maturity-tier gate.

Every writer of stock_rule_calibration.is_mature* must use derive_maturity_flags
so the gate cannot drift between paths:
  - agents/price_agent.py          (upsert teen flag + recompute authoritative)
  - scripts/backfill_paper_trades.py
  - scripts/recompute_maturity_flags.py

This module imports nothing with side effects (no os.environ, no network) so a
script can `from _maturity import derive_maturity_flags` without booting an agent.

Tier definitions (2026-06-04 payoff-first adult gate):
  teen         : n≥30,  acc≥0.70, mean>0
  young_adult  : n≥30,  acc≥0.80, PF>1.2
  adult        : n≥100, PF≥2.0,   mean≥0.5%        ← the BUY/SELL gate
  high_conv    : n≥30,  acc≥0.90, PF>1.5, mean≥0   (analysis flag, not the gate)
"""
from __future__ import annotations

from collections import defaultdict

MATURITY_ACCURACY     = 0.90    # legacy const; now used only by HIGH_CONVICTION
MATURITY_MIN_N        = 30      # teen tier minimum sample
TIER_GATE_TEEN_ACC    = 0.70
TIER_GATE_YOUNG_ACC   = 0.80
TIER_GATE_ADULT_ACC   = MATURITY_ACCURACY
TIER_GATE_TEEN_MR     = 0.0     # teen also requires mean_realized_pct > 0
TIER_GATE_YOUNG_PF    = 1.2     # young_adult also requires profit_factor > 1.2
TIER_GATE_ADULT_PF    = 1.5     # legacy adult PF gate (still used in HIGH_CONVICTION)

ADULT_MIN_N      = 100
ADULT_MIN_PF     = 2.0
ADULT_MIN_MEAN   = 0.005        # 0.5% mean realized per closed trade (after slippage)
HIGH_CONV_MIN_N  = MATURITY_MIN_N
HIGH_CONV_MIN_ACC = MATURITY_ACCURACY
HIGH_CONV_MIN_PF  = TIER_GATE_ADULT_PF
HIGH_CONV_MIN_MEAN = 0.0


def collapse_to_effective(trades) -> dict:
    """Collapse pseudo-replicated closed trades to INDEPENDENT evidence (H1).

    One market move fans into many (ticker, entry-day) paper trades for the same
    rule, so raw n over-counts 2-4x. This collapses each (ticker, entry-day) into
    ONE observation whose representative return is the MEAN of the cluster's
    trades (Codex: mean, not majority/first). correct = mean > 0 (zero is NOT
    correct); PF = Σ(positive cluster means) / |Σ(negative cluster means)|.

    trades: iterable of dicts with 'ticker', 'entry_at' (ISO; date is the first
    10 chars), 'realized_return'. Null-return trades are skipped.

    Returns effective_{n, n_correct, accuracy, mean_realized_pct, profit_factor}.
    """
    clusters: dict[tuple[str, str], list[float]] = defaultdict(list)
    for t in trades:
        rr = t.get("realized_return")
        if rr is None:
            continue
        key = (t.get("ticker") or "", (t.get("entry_at") or "")[:10])
        clusters[key].append(float(rr))

    means = [sum(v) / len(v) for v in clusters.values()]
    n = len(means)
    n_correct = sum(1 for m in means if m > 0)
    sum_wins = sum(m for m in means if m > 0)
    sum_losses = sum(m for m in means if m < 0)
    pf = (sum_wins / abs(sum_losses)) if sum_losses < 0 else None
    return {
        "effective_n":               n,
        "effective_n_correct":       n_correct,
        "effective_accuracy":        (n_correct / n) if n else 0.0,
        "effective_mean_realized_pct": (sum(means) / n) if n else 0.0,
        "effective_profit_factor":   pf,
    }


def derive_maturity_flags(n: int, pf: float | None, mean: float,
                          accuracy: float) -> dict:
    """PURE maturity gate. The PF-gated tiers (young_adult, adult,
    high_conviction) require a profit factor and are False when pf is None.

    Callers MUST pass a FRESH profit_factor — promoting on a previous batch's
    stale PF is the lag this gate exists to close (C2-pflag)."""
    n_ok = n >= MATURITY_MIN_N
    is_mature_70 = bool(n_ok and accuracy >= TIER_GATE_TEEN_ACC and mean > TIER_GATE_TEEN_MR)
    is_mature_80 = bool(n_ok and accuracy >= TIER_GATE_YOUNG_ACC
                        and pf is not None and pf > TIER_GATE_YOUNG_PF)
    is_mature = bool(n >= ADULT_MIN_N and pf is not None
                     and pf >= ADULT_MIN_PF and mean >= ADULT_MIN_MEAN)
    is_high_conviction = bool(n >= HIGH_CONV_MIN_N and accuracy >= HIGH_CONV_MIN_ACC
                              and pf is not None and pf > HIGH_CONV_MIN_PF
                              and mean >= HIGH_CONV_MIN_MEAN)
    if is_mature:        tier = "adult"
    elif is_mature_80:   tier = "young_adult"
    elif is_mature_70:   tier = "teen"
    else:                tier = "child"
    return {"is_mature": is_mature, "is_mature_70": is_mature_70,
            "is_mature_80": is_mature_80, "is_high_conviction": is_high_conviction,
            "tier": tier}
