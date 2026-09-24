from scripts.quarterly_rotation.methodology import (
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
    assert 0.70 < pb < 0.80


def test_sma_requires_full_window():
    assert sma([1, 2, 3], 4) is None
    assert sma([1, 2, 3, 4], 4) == 2.5


def test_hold_gate_requires_independent_history():
    cfg = RotationConfig()
    stats = hold_stats([100.0] * 2016, horizon=504, min_independent_windows=4)
    assert stats.independent_windows == 4
    assert stats.positive_rate == 0.0
    assert not hold_gate(stats, cfg)


def test_quarterly_dates_uses_last_observation_per_quarter():
    dates = ["2025-01-02", "2025-03-28", "2025-04-01", "2025-06-30", "2025-09-29"]
    assert quarterly_dates(dates) == ["2025-03-28", "2025-06-30", "2025-09-29"]


def test_next_open_is_strictly_after_decision_date():
    sessions = ["2025-03-28", "2025-03-31", "2025-04-01"]
    assert next_open_entry("2025-03-28", sessions) == "2025-03-31"
    assert next_open_entry("2025-04-01", sessions) is None
