import random

from scripts.quarterly_rotation import robustness as rb


def _series(seed, n=900):
    rng = random.Random(seed)
    px, c = 100.0, []
    for _ in range(n):
        px *= 1 + rng.gauss(0.0004, 0.02)
        c.append(px)
    return {"date": [f"{2015 + i // 250}-01-01" for i in range(n)], "open": c[:], "close": c,
            "pb": rb.pct_b_series(c)}


def test_grid_is_complete_and_deterministic():
    s = {"A": _series(1), "B": _series(2)}
    a, b = rb.run(s), rb.run(s)
    assert a == b
    assert len(a["grid"]) == len(rb.THRESHOLDS) * len(rb.HOLDS) * len(rb.ENTRIES)


def test_cost_variant_is_exactly_cost_lower():
    s = {"A": _series(3)}
    g = rb.run(s)["grid"]
    x, y = g["0.20|60|next_open"], g["0.20|60|next_open_cost"]
    assert abs((x["mean"] - y["mean"]) - rb.COST) < 1e-12
    assert abs(x["edge"] - y["edge"]) < 1e-12        # null pays the same cost


def test_null_is_every_session_so_threshold_1e9_has_zero_edge():
    s = {"A": _series(4)}
    c = rb.cell(s, 1e9, 20, "next_open")
    assert abs(c["edge"]) < 1e-12


def test_verdict_demotes_isolated_spike():
    grid = {k: {"edge": 0.01} for k in ("0.20|60|next_open", "0.15|60|next_open", "0.25|60|next_open",
                                        "0.20|40|next_open", "0.20|90|next_open")}
    assert not rb.verdict(grid)["demoted"]
    grid["0.15|60|next_open"]["edge"] = -0.001
    assert not rb.verdict(grid)["demoted"]
    grid["0.20|90|next_open"]["edge"] = None
    assert rb.verdict(grid)["demoted"]
    grid = {k: {"edge": 0.01} for k in grid}
    grid["0.20|60|next_open"]["edge"] = 0.0
    assert rb.verdict(grid)["demoted"]


def test_no_lookahead_in_forward_return():
    s = _series(5, 200)
    assert rb._fwd(s["open"], s["close"], 150, 60, "next_open") is None
    r = rb._fwd(s["open"], s["close"], 100, 60, "next_open")
    assert abs(r - (s["close"][160] / s["open"][101] - 1)) < 1e-12


def test_thin_neighbour_counts_against_default():
    keys = ("0.20|60|next_open", "0.15|60|next_open", "0.25|60|next_open",
            "0.20|40|next_open", "0.20|90|next_open")
    grid = {k: {"edge": 0.01, "episodes": 50} for k in keys}
    grid["0.15|60|next_open"]["episodes"] = 3
    grid["0.25|60|next_open"]["episodes"] = 3
    assert rb.verdict(grid)["demoted"]


def test_by_regime_skips_sessions_without_a_200_day_reading():
    s = {"A": _series(6, 400)}
    s["A"]["date"] = dates = [f"d{i:04d}" for i in range(400)]
    known = set(dates[250:])
    up = set(dates[250:320])
    r = rb.run(s, up, known)["by_regime"]
    total = sum(v["n"] for v in r.values())
    unrestricted = rb.run(s, up, None)["by_regime"]
    early = sum(1 for i, pb in enumerate(s["A"]["pb"][:250]) if pb is not None and pb < 0.20)
    assert early > 0
    assert total == sum(v["n"] for v in unrestricted.values()) - early
    assert set(r) <= {"qqq_above_200", "qqq_below_200"}
