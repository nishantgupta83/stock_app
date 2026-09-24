import random
from datetime import date, timedelta

from scripts.quarterly_rotation.experiment import (
    ExperimentConfig, build_cohorts, evaluate, verdict,
)
from scripts.quarterly_rotation.methodology import (
    RotationConfig, durability_measurable, rebalance_dates,
)


def _weekdays(start, n):
    d, out = date.fromisoformat(start), []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def test_rebalance_dates_exclude_incomplete_quarter():
    cal = _weekdays("2025-01-01", 300)             # runs to ~2026-02
    got = rebalance_dates(cal, "2025-09-24")
    assert got == ["2025-03-31", "2025-06-30"]     # Q3 not over yet: no 2025-09-24 "quarter end"
    assert rebalance_dates(cal, "2025-09-30")[-1] == "2025-09-30"


def test_rebalance_dates_ignore_a_dropped_asset_session():
    cal = _weekdays("2025-01-01", 200)
    # one asset missing the true quarter-end must not move the shared decision date
    assert "2025-03-31" in rebalance_dates(cal, "2025-12-31")


def test_durability_measurable_boundary():
    assert not durability_measurable(2015)
    assert durability_measurable(2016)
    assert not durability_measurable(2513 - 2200)


def _synthetic(n_names=12, sessions=900, seed=1, dip_bonus=0.0):
    """Random-walk names. `dip_bonus` plants a real edge: the session after a quarter-end at
    which a name is in the dip band, its open-to-open drift is lifted."""
    rng = random.Random(seed)
    cal = _weekdays("2020-01-01", sessions)
    bars = {}
    for j in range(n_names):
        px, series = 100.0, {}
        for d in cal:
            px *= 1 + rng.gauss(0.0004, 0.02)
            series[d] = (px, px)
        bars[f"T{j}"] = series
    return bars, cal


def _plant(bars, cal, cfg, bonus):
    """Rewrite each name's prices so that dip names rebound `bonus` over the next quarter."""
    as_of = cal[-1]
    r_dates = rebalance_dates(cal, as_of)
    idx = {d: i for i, d in enumerate(cal)}
    from scripts.quarterly_rotation.methodology import bollinger_pct_b
    for k in range(len(r_dates) - 1):
        r, rn = r_dates[k], r_dates[k + 1]
        for t, b in bars.items():
            closes = [b[d][1] for d in cal[max(0, idx[r] - 59): idx[r] + 1]]
            if len(closes) >= 20 and (bollinger_pct_b(closes) or 1) < cfg.dip_pct_b:
                a, z = idx[r] + 1, idx[rn] + 1
                for i in range(a, min(z + 1, len(cal))):
                    o, c = b[cal[i]]
                    f = (1 + bonus) ** ((i - a + 1) / max(1, z - a + 1))
                    b[cal[i]] = (o * f, c * f)
    return bars


def test_planted_edge_is_detected_and_null_is_not():
    cfg = ExperimentConfig(min_history=100, min_universe=8, n_draws=400)
    bars, cal = _synthetic(n_names=14, sessions=1500, seed=3)
    null_res = evaluate(build_cohorts(bars, cal, cal[-1], cfg), cfg)
    assert null_res["signal_quarters"] > 5
    assert null_res["p_value"] > 0.05                        # pure noise is not promoted
    edge_res = evaluate(build_cohorts(_plant(bars, cal, cfg, 0.15), cal, cal[-1], cfg), cfg)
    assert edge_res["mean_net_excess"] > 0.05
    assert edge_res["p_value"] <= 0.05


def test_deterministic_same_seed():
    cfg = ExperimentConfig(min_history=100, n_draws=200)
    bars, cal = _synthetic(sessions=1200, seed=5)
    a = evaluate(build_cohorts(bars, cal, cal[-1], cfg), cfg)
    b = evaluate(build_cohorts(bars, cal, cal[-1], cfg), cfg)
    assert a == b


def test_config_hash_covers_thresholds_and_costs():
    base = ExperimentConfig().config_hash()
    assert ExperimentConfig(cost_round_trip=0.0).config_hash() != base
    assert ExperimentConfig(max_p=0.10).config_hash() != base
    assert ExperimentConfig().config_hash() == base


def test_unfinished_quarter_and_missing_exit_open_are_not_cohorts():
    cfg = ExperimentConfig(min_history=50, min_universe=3)
    bars, cal = _synthetic(n_names=4, sessions=400, seed=9)
    cohorts = build_cohorts(bars, cal, cal[-1], cfg)
    assert all(c["exit"] <= cal[-1] for c in cohorts)
    assert all(c["entry"] > c["decision"] for c in cohorts)
    for a, b in zip(cohorts, cohorts[1:]):
        assert a["entry"] == cal[cal.index(a["decision"]) + 1]
        assert a["exit"] == cal[cal.index(b["decision"]) + 1] == b["entry"]
    # a name with no bar on the entry session is dropped from that cohort, not zero-filled
    victim = cohorts[2]
    del bars["T0"][victim["entry"]]
    again = build_cohorts(bars, cal, cal[-1], cfg)[2]
    assert "T0" not in again["names"] and again["dropped_no_price"] >= 1


def test_verdict_requires_every_threshold():
    cfg = ExperimentConfig()
    good = {"p_value": 0.01, "mean_net_excess": 0.02, "signal_quarters": 25, "hit_rate": 0.6,
            "mean_excess_ex_best_quarter": 0.01, "first_half_mean": 0.01, "second_half_mean": 0.03}
    assert verdict(good, cfg)["promote"]
    for k, bad in (("p_value", 0.2), ("mean_net_excess", 0.005), ("signal_quarters", 10),
                   ("hit_rate", 0.4), ("mean_excess_ex_best_quarter", -0.001),
                   ("second_half_mean", -0.01), ("first_half_mean", -0.01),
                   ("first_half_mean", None)):
        assert not verdict({**good, k: bad}, cfg)["promote"], k


def test_pct_b_ignores_every_bar_after_the_decision_date():
    cfg = ExperimentConfig(min_history=50, min_universe=3)
    bars, cal = _synthetic(n_names=4, sessions=400, seed=11)
    base = build_cohorts(bars, cal, cal[-1], cfg)[3]
    i = cal.index(base["decision"])
    for t in bars:
        for d in cal[i + 1: i + 6]:
            o, c = bars[t][d]
            bars[t][d] = (o * 3, c * 3)
    again = build_cohorts(bars, cal, cal[-1], cfg)[3]
    assert {t: v["pb"] for t, v in base["names"].items()} == {t: v["pb"] for t, v in again["names"].items()}


def test_null_carries_the_cost():
    # every name is a dip name -> observed excess is exactly -cost, and so is each null draw
    cfg = ExperimentConfig(dip_pct_b=1e9, min_history=50, min_universe=3, n_draws=50)
    bars, cal = _synthetic(n_names=5, sessions=400, seed=13)
    res = evaluate(build_cohorts(bars, cal, cal[-1], cfg), cfg)
    assert abs(res["mean_net_excess"] + cfg.cost_round_trip) < 1e-12
    assert abs(res["null_mean"] + cfg.cost_round_trip) < 1e-12


def test_name_missing_a_window_session_is_ineligible_not_stretched():
    cfg = ExperimentConfig(min_history=50, min_universe=3)
    bars, cal = _synthetic(n_names=4, sessions=400, seed=17)
    c0 = build_cohorts(bars, cal, cal[-1], cfg)[3]
    del bars["T1"][cal[cal.index(c0["decision"]) - 5]]
    assert "T1" not in build_cohorts(bars, cal, cal[-1], cfg)[3]["names"]


def test_v1_hash_is_stable_and_v2_differs():
    from scripts.quarterly_rotation.experiment import ExperimentConfigV2
    assert ExperimentConfig().config_hash() == "1774730074739b66"   # v1 froze with this hash
    v2 = ExperimentConfigV2()
    assert v2.config_hash() != ExperimentConfig().config_hash()
    assert (v2.experiment_id, v2.sma_window, v2.max_p, v2.min_ex_best, v2.trials) == \
        ("qr_s1_v2", 200, 0.025, 0.005, 2)
    assert (v2.dip_pct_b, v2.min_signal_quarters, v2.min_hit_rate, v2.cost_round_trip, v2.seed) == \
        (0.20, 20, 0.55, 0.0020, 20260924)
    assert ExperimentConfig().trials == 1


def test_v2_dip_must_be_above_200d_sma():
    from scripts.quarterly_rotation.experiment import ExperimentConfigV2
    cfg1 = ExperimentConfig(min_history=250, min_universe=8, n_draws=50)
    cfg2 = ExperimentConfigV2(min_history=250, min_universe=8, n_draws=50)
    bars, cal = _synthetic(n_names=14, sessions=1500, seed=23)
    cohorts = build_cohorts(bars, cal, cal[-1], cfg2)
    for c in cohorts:                                  # every flag matches a hand computation
        i = cal.index(c["decision"])
        for t, v in c["names"].items():
            w = [bars[t][d][1] for d in cal[max(0, i - 199): i + 1]]
            assert v["above_sma"] == (bars[t][c["decision"]][1] > sum(w) / len(w))
    r1, r2 = evaluate(cohorts, cfg1), evaluate(cohorts, cfg2)
    assert r2["signal_quarters"] <= r1["signal_quarters"]
    assert r2["experiment_id"] == "qr_s1_v2"
    assert r2["p_value_vs_trend_pool_null_INFORMATIONAL"] is not None
    assert r1["p_value_vs_trend_pool_null_INFORMATIONAL"] is None


def test_v2_dip_set_is_exactly_dip_and_above_sma_and_gapped_names_are_excluded():
    from scripts.quarterly_rotation.experiment import ExperimentConfigV2
    cfg1 = ExperimentConfig(min_history=250, min_universe=8, n_draws=20)
    cfg2 = ExperimentConfigV2(min_history=250, min_universe=8, n_draws=20)
    bars, cal = _synthetic(n_names=14, sessions=1500, seed=29)
    base = build_cohorts(bars, cal, cal[-1], cfg2)
    k = 12
    victim = "T3"
    i = cal.index(base[k]["decision"])
    for d in cal[i - 150: i - 120]:                       # 30 missing sessions in the 200 window
        bars[victim].pop(d, None)
    cohorts = build_cohorts(bars, cal, cal[-1], cfg2)
    assert cohorts[k]["names"][victim]["above_sma"] is None
    for c in cohorts:
        want = sorted(t for t, v in c["names"].items() if v["pb"] < 0.20 and v["above_sma"] is True)
        if len(c["names"]) < 8 or not want:
            continue
        got = evaluate([c], cfg2)
        assert got["signal_quarters"] == 1
        assert got["mean_dips_per_signal_quarter"] == len(want)
        assert victim not in want or c is not cohorts[k]
    r1, r2 = evaluate(cohorts, cfg1), evaluate(cohorts, cfg2)
    dips1 = sum(1 for c in cohorts for v in c["names"].values() if v["pb"] < 0.20)
    dips2 = sum(1 for c in cohorts for v in c["names"].values() if v["pb"] < 0.20 and v["above_sma"] is True)
    assert dips2 < dips1                                # the filter actually removes names


V1_SNAPSHOT = (16, 0.036956, 0.099502)   # measured on the pre-v2 code, byte-identical per review


def test_v1_evaluate_snapshot_is_unchanged():
    cfg = ExperimentConfig(min_history=100, n_draws=200)
    bars, cal = _synthetic(sessions=1200, seed=5)
    res = evaluate(build_cohorts(bars, cal, cal[-1], cfg), cfg)
    got = (res["signal_quarters"], round(res["mean_net_excess"], 6), round(res["p_value"], 6))
    assert got == V1_SNAPSHOT
