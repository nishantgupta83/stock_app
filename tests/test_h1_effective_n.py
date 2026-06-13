"""H1 — effective-n: collapse pseudo-replicated trades to independent evidence.

Calibration n over-counts: one market move fans into many (ticker, entry-day)
paper trades for the same rule. The maturity gate must run on EFFECTIVE n — one
observation per (ticker, entry-day) cluster, the cluster's representative
outcome = the MEAN of its trades' realized_return (Codex: mean, not majority/
first; zero counts as not-correct). These pin that collapse so the gate can't
silently revert to raw n.
"""
from __future__ import annotations

from _maturity import collapse_to_effective


def _t(ticker, day, ret):
    return {"ticker": ticker, "entry_at": f"{day}T14:30:00+00:00", "realized_return": ret}


def test_collapses_same_ticker_day_into_one_observation() -> None:
    trades = [
        _t("AAA", "2026-06-01", 0.05),
        _t("AAA", "2026-06-01", 0.03),   # same (ticker,day) → one cluster, mean 0.04 (win)
        _t("BBB", "2026-06-01", -0.02),  # loss
        _t("AAA", "2026-06-02", 0.01),   # win
    ]
    e = collapse_to_effective(trades)
    assert e["effective_n"] == 3                       # 4 trades → 3 clusters
    assert e["effective_n_correct"] == 2               # 0.04 and 0.01 > 0
    assert abs(e["effective_accuracy"] - 2 / 3) < 1e-9
    assert abs(e["effective_mean_realized_pct"] - (0.04 - 0.02 + 0.01) / 3) < 1e-9
    # PF = sum(positive cluster means) / |sum(negative)| = (0.04+0.01)/0.02 = 2.5
    assert abs(e["effective_profit_factor"] - 2.5) < 1e-9


def test_zero_cluster_mean_is_not_correct_and_not_a_loss() -> None:
    e = collapse_to_effective([_t("AAA", "2026-06-01", 0.0)])
    assert e["effective_n"] == 1
    assert e["effective_n_correct"] == 0               # zero is not correct
    assert e["effective_profit_factor"] is None        # no losses → PF undefined


def test_all_losses_pf_is_finite() -> None:
    e = collapse_to_effective([_t("AAA", "2026-06-01", -0.02), _t("BBB", "2026-06-02", -0.01)])
    assert e["effective_n"] == 2
    assert e["effective_n_correct"] == 0
    assert e["effective_profit_factor"] == 0.0         # no wins → 0/loss = 0.0


def test_skips_null_returns() -> None:
    e = collapse_to_effective([_t("AAA", "2026-06-01", None), _t("BBB", "2026-06-02", 0.03)])
    assert e["effective_n"] == 1                        # null-return trade dropped


def test_empty() -> None:
    e = collapse_to_effective([])
    assert e["effective_n"] == 0 and e["effective_profit_factor"] is None


# ---------- integration: recompute_rule_payoff gates on effective ----------

def test_recompute_rule_payoff_gates_on_effective(monkeypatch) -> None:
    """A rule that is RAW-adult (n=120, PF=10, mean=1.5%) but whose trades are
    ALL one (ticker, entry-day) cluster must be written is_mature=False — the
    gate runs on effective-n=1, not raw n. Also pins that recompute imports the
    collapse helper (a missing import would NameError here)."""
    import price_agent

    trades = ([{"ticker": "AAA", "entry_at": "2026-06-01T14:00:00+00:00",
                "realized_return": 0.02, "correct": True, "mfe_pct": None,
                "mae_pct": None, "target_hit": None, "stop_hit": None}] * 100
              + [{"ticker": "AAA", "entry_at": "2026-06-01T14:00:00+00:00",
                  "realized_return": -0.01, "correct": False, "mfe_pct": None,
                  "mae_pct": None, "target_hit": None, "stop_hit": None}] * 20)

    def fake_sb_get(table, params):
        if params.get("status") == "eq.closed":          # the trades page
            return trades if params.get("offset") == "0" else []
        return [{"is_mature": True}]                       # prev-flags row (was adult)

    captured: list[dict] = []
    monkeypatch.setattr(price_agent, "sb_get", fake_sb_get)
    monkeypatch.setattr(price_agent, "sb_upsert", lambda t, rows, on_conflict: captured.extend(rows) or True)

    price_agent.recompute_rule_payoff("raw_adult_but_pseudo::h7d")

    flags = next(r for r in captured if "is_mature" in r)
    assert flags["is_mature"] is False                    # gated on effective-n=1, not raw 120
    assert flags["tier"] == "child"
    eff = next(r for r in captured if "effective_n" in r)
    assert eff["effective_n"] == 1                         # 120 trades → 1 (ticker,day) cluster
