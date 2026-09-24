"""S1 (dip-only quarterly rotation) vs a same-universe permutation null.

Pre-registered in docs/experiments/2026-09-24-preregistration-qr-s1-v1.md -- parameters here
are FROZEN by that document; changing one is a new version, not an edit.

Pure Python (tests/conftest.py stubs pandas/yfinance in CI). The only I/O is `main()`.

Design in one paragraph: at each completed calendar quarter-end R (dates from ONE reference
calendar), every name with a bar on R and enough history is "eligible". The dip set is the
eligible names with %B < 0.20 on R. S1 buys the dip set equal-weight at the next session's
OPEN and exits at the open after the NEXT quarter-end. The quarter's excess is
mean(dip returns) - cost - mean(all eligible returns). The null re-draws the same NUMBER of
names uniformly from the same eligible set each quarter, so a universe chosen with hindsight
(today's AI winners) lifts S1 and the null alike. It does NOT fully cancel for a dip rule:
every name that dipped and never recovered was excluded by construction, so a positive
result is biased upward specifically for dip entries; a null result is unaffected.
"""
from __future__ import annotations

import hashlib
import json
import random
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

if __package__ in (None, ""):                       # allow `python3 scripts/quarterly_rotation/experiment.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.quarterly_rotation.methodology import (   # noqa: E402
    RotationConfig, bollinger_pct_b, durability_measurable, rebalance_dates,
)

EXPERIMENT_ID = "qr_s1_v1"
# Stock sleeve only. Frozen list from the screen's AI groups; ETFs, leveraged/inverse and the
# humanoid supply chain are excluded (short or no history, or not ordinary long instruments).
UNIVERSE = ("NVDA AMD AVGO MRVL TSM ARM ALAB CRDO SMCI MU WDC STX SNDK AMAT LRCX KLAC ASML "
            "TER ONTO ENTG COHR VRT CEG GEV PWR ETN ANET CIEN LITE MSFT GOOGL META ORCL PLTR NOW").split()
CALENDAR_TICKER = "QQQ"


@dataclass(frozen=True)
class ExperimentConfig:
    dip_pct_b: float = 0.20             # fixed; no threshold grid
    min_history: int = 252              # sessions of prior closes before a name is eligible
    min_universe: int = 8               # a quarter with fewer eligible names is not a cohort
    cost_round_trip: float = 0.0020     # 10 bps per side, applied to S1 and to every null draw
    n_draws: int = 2000
    seed: int = 20260924
    # promotion thresholds (ALL required) -- written before any result was computed
    max_p: float = 0.05
    min_mean_excess: float = 0.010      # +1.0 pts per quarter, net
    min_signal_quarters: int = 20
    min_hit_rate: float = 0.55

    def config_hash(self) -> str:
        blob = json.dumps({"cfg": asdict(self), "universe": UNIVERSE, "cal": CALENDAR_TICKER,
                           "id": EXPERIMENT_ID}, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]


Bars = dict[str, dict[str, tuple[float, float]]]     # ticker -> date -> (open, close)


def build_cohorts(bars: Bars, calendar: list[str], as_of: str, cfg: ExperimentConfig) -> list[dict]:
    """One dict per completed quarter with eligible names, their %B and next-quarter returns."""
    r_dates = rebalance_dates(calendar, as_of)
    idx = {d: i for i, d in enumerate(calendar)}
    sorted_dates = {t: sorted(b) for t, b in bars.items()}
    cohorts = []
    for k in range(len(r_dates) - 1):
        r, r_next = r_dates[k], r_dates[k + 1]
        i0, i1 = idx[r] + 1, idx[r_next] + 1
        if i1 >= len(calendar):
            continue                                  # exit open not observable yet
        entry, exit_ = calendar[i0], calendar[i1]
        names = {}
        n_eligible_no_price = 0
        for t, b in bars.items():
            if r not in b:
                continue
            ds = sorted_dates[t]
            # closes STRICTLY BEFORE the decision date (bisect), then the %B window is read off
            # the shared calendar so every name uses the same 20 sessions; a missing one
            # makes the name ineligible rather than silently stretching its window.
            lo, hi = 0, len(ds)
            while lo < hi:
                mid = (lo + hi) // 2
                if ds[mid] < r:
                    lo = mid + 1
                else:
                    hi = mid
            prior = lo
            if prior < cfg.min_history:
                continue
            if entry not in b or exit_ not in b:
                n_eligible_no_price += 1
                continue
            window = calendar[idx[r] - 19: idx[r] + 1]
            if len(window) < 20 or any(d not in b for d in window):
                continue
            closes = [b[d][1] for d in window]
            pb = bollinger_pct_b(closes, 20, 2.0)
            if pb is None:
                continue
            ret = b[exit_][0] / b[entry][0] - 1
            names[t] = {"pb": pb, "ret": ret, "prior": prior}
        cohorts.append({"decision": r, "entry": entry, "exit": exit_, "names": names,
                        "dropped_no_price": n_eligible_no_price})
    return cohorts


def _mean(xs):
    return sum(xs) / len(xs)


def evaluate(cohorts: list[dict], cfg: ExperimentConfig) -> dict:
    signal = []
    for c in cohorts:
        u = c["names"]
        if len(u) < cfg.min_universe:
            continue
        dips = [t for t, v in u.items() if v["pb"] < cfg.dip_pct_b]
        if not dips:
            continue
        signal.append({"decision": c["decision"], "universe": sorted(u), "dips": sorted(dips),
                       "u_ret": [u[t]["ret"] for t in sorted(u)],
                       "d_ret": [u[t]["ret"] for t in sorted(dips)]})
    excess = [_mean(s["d_ret"]) - cfg.cost_round_trip - _mean(s["u_ret"]) for s in signal]
    obs = _mean(excess) if excess else None

    rng = random.Random(cfg.seed)
    draws = []
    for _ in range(cfg.n_draws):
        acc = 0.0
        for s in signal:
            pick = rng.sample(s["u_ret"], len(s["d_ret"]))
            acc += _mean(pick) - cfg.cost_round_trip - _mean(s["u_ret"])
        draws.append(acc / len(signal) if signal else 0.0)
    p = ((1 + sum(1 for d in draws if d >= obs)) / (1 + cfg.n_draws)) if obs is not None else None
    draws.sort()
    pctile = (sum(1 for d in draws if d < obs) / len(draws)) if obs is not None else None

    n = len(excess)
    half = n // 2
    ex_best = (sum(excess) - max(excess)) / (n - 1) if n > 1 else None
    res = {
        "experiment_id": EXPERIMENT_ID, "config_hash": cfg.config_hash(),
        "configurations_tried": 1,
        "quarters_total": len(cohorts), "signal_quarters": n,
        "no_signal_quarters": sum(1 for c in cohorts if len(c["names"]) >= cfg.min_universe) - n,
        "thin_universe_quarters": sum(1 for c in cohorts if len(c["names"]) < cfg.min_universe),
        "mean_dips_per_signal_quarter": _mean([len(s["dips"]) for s in signal]) if signal else None,
        "mean_universe": _mean([len(s["universe"]) for s in signal]) if signal else None,
        "mean_net_excess": obs,
        "mean_gross_excess": (obs + cfg.cost_round_trip) if obs is not None else None,
        "hit_rate": (sum(1 for e in excess if e > 0) / n) if n else None,
        "mean_excess_ex_best_quarter": ex_best,
        "first_half_mean": _mean(excess[:half]) if half else None,
        "second_half_mean": _mean(excess[half:]) if n - half else None,
        "null_mean": _mean(draws), "null_p95": draws[int(0.95 * len(draws))],
        "null_percentile": pctile, "p_value": p,
        "per_quarter": [{"decision": s["decision"], "n_dips": len(s["dips"]),
                         "excess": round(e, 6)} for s, e in zip(signal, excess)],
        "durability_measurable_name_quarters": sum(
            1 for c in cohorts for v in c["names"].values() if durability_measurable(v["prior"])),
        "eligible_name_quarters": sum(len(c["names"]) for c in cohorts),
    }
    res["verdict"] = verdict(res, cfg)
    return res


def verdict(r: dict, cfg: ExperimentConfig) -> dict:
    checks = {
        "P1_p_value": r["p_value"] is not None and r["p_value"] <= cfg.max_p,
        "P2_mean_net_excess": r["mean_net_excess"] is not None and r["mean_net_excess"] >= cfg.min_mean_excess,
        "P3_signal_quarters": r["signal_quarters"] >= cfg.min_signal_quarters,
        "P4_hit_rate": r["hit_rate"] is not None and r["hit_rate"] >= cfg.min_hit_rate,
        "P5_ex_best_quarter_positive": r["mean_excess_ex_best_quarter"] is not None and r["mean_excess_ex_best_quarter"] > 0,
        "P6_both_halves_nonneg": (r["first_half_mean"] is not None and r["second_half_mean"] is not None
                                  and r["first_half_mean"] >= 0 and r["second_half_mean"] >= 0),
    }
    return {"checks": checks, "promote": all(checks.values())}


def main() -> int:
    import warnings
    warnings.filterwarnings("ignore")
    import yfinance as yf
    cfg = ExperimentConfig()
    tickers = list(UNIVERSE) + [CALENDAR_TICKER]
    raw = yf.download(tickers, period="10y", interval="1d", auto_adjust=True,
                      group_by="ticker", progress=False)
    bars: Bars = {}
    for t in tickers:
        try:
            df = raw[t][["Open", "Close"]].dropna()
        except KeyError:
            continue
        if df.empty:
            continue                                  # failed downloads come back all-NaN
        bars[t] = {i.strftime("%Y-%m-%d"): (float(o), float(c)) for i, o, c in
                   zip(df.index, df["Open"], df["Close"])}
    calendar = sorted(bars.pop(CALENDAR_TICKER))
    as_of = calendar[-1]
    cohorts = build_cohorts(bars, calendar, as_of, cfg)
    res = evaluate(cohorts, cfg)
    res["data_as_of"] = as_of
    res["calendar_sessions"] = len(calendar)
    res["tickers_loaded"] = sorted(bars)
    res["tickers_missing"] = sorted(set(UNIVERSE) - set(bars))
    out = Path(__file__).resolve().parent / "results" / f"{EXPERIMENT_ID}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(res, indent=1, sort_keys=True))
    v = res["verdict"]
    print(json.dumps({k: res[k] for k in ("signal_quarters", "mean_net_excess", "hit_rate", "p_value",
                                          "null_percentile", "mean_excess_ex_best_quarter",
                                          "first_half_mean", "second_half_mean")}, indent=1))
    print("checks:", v["checks"]); print("PROMOTE:" , v["promote"]); print("wrote", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
