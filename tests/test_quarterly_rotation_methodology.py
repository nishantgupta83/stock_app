from scripts.quarterly_rotation.methodology import (
    HoldStats,
    RotationConfig,
    bollinger_pct_b,
    hold_gate,
    hold_stats,
    next_open_entry,
    quarterly_dates,
    sma,
)


def test_bollinger_pct_b_is_population_std():
    # Constant series has no usable band.
    assert bollinger_pct_b([10.0] * 20) is None
    values = list(range(1, 21))
    pb = bollinger_pct_b(values, 20, 2.0)
    assert pb is not None
    # window 1..20: mean 10.5, population sd 5.766 -> (20 - lower) / (4 * sd) = 0.9119.
    # (The sample-std variant gives 0.9014; 0.70-0.80 was never a value this can take.)
    assert abs(pb - 0.9118772355) < 1e-9


def test_sma_requires_full_window():
    assert sma([1, 2, 3], 4) is None
    assert sma([1, 2, 3, 4], 4) == 2.5


def test_hold_gate_requires_independent_history():
    cfg = RotationConfig()
    stats = hold_stats([100.0] * 2016, horizon=504, min_independent_windows=4)
    assert stats.independent_windows == 4
    assert stats.positive_rate == 0.0
    assert not hold_gate(stats, cfg)


def test_hold_stats_too_short_history_is_unmeasurable():
    # One session short of 4 independent 504-session windows: no stats, gate closed.
    stats = hold_stats([100.0 + i for i in range(2015)], horizon=504, min_independent_windows=4)
    assert stats == HoldStats(0, 0, None, None, None)
    assert not hold_gate(stats, RotationConfig())


def test_hold_gate_passes_on_steady_uptrend():
    closes = [100.0 * 1.0005 ** i for i in range(2100)]
    stats = hold_stats(closes)
    assert stats.independent_windows >= 4
    assert stats.positive_rate == 1.0
    assert hold_gate(stats, RotationConfig())


def test_parity_with_production_screen():
    # "Strategy 0 is the production screen" is only true if the two copies cannot drift.
    from scripts import ai_humanoid_screen as prod
    closes = [100 + 7 * ((i * 37) % 11) / 11 + i * 0.05 for i in range(2600)]
    assert bollinger_pct_b(closes[:60]) is not None
    for end in (25, 60, 400, 2600):
        want, _ = prod.pct_b(closes[:end])
        got = bollinger_pct_b(closes[:end])
        assert (want is None and got is None) or abs(want - got) < 1e-9
    ref, mine = prod.hold_period(closes), hold_stats(closes)
    assert (ref["n"], ref["n_independent"]) == (mine.raw_windows, mine.independent_windows)
    assert (ref["positive"], ref["p10"], ref["median"]) == (mine.positive_rate, mine.p10, mine.median)


def test_quarterly_dates_uses_last_observation_per_quarter():
    dates = ["2025-01-02", "2025-03-28", "2025-04-01", "2025-06-30", "2025-09-29"]
    assert quarterly_dates(dates) == ["2025-03-28", "2025-06-30", "2025-09-29"]


def test_next_open_is_strictly_after_decision_date():
    sessions = ["2025-03-28", "2025-03-31", "2025-04-01"]
    assert next_open_entry("2025-03-28", sessions) == "2025-03-31"
    assert next_open_entry("2025-04-01", sessions) is None
