"""Pure-function tests for the AI/humanoid screen. No network, no clock."""
from pathlib import Path
import math
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import ai_humanoid_screen as ah  # noqa: E402
import ai_humanoid_render as ar  # noqa: E402


def _bars(closes, vols=None, hl=0.01):
    vols = vols or [1_000_000.0] * len(closes)
    return [{"date": f"2026-01-{i % 28 + 1:02d}", "open": c, "close": c,
             "high": c * (1 + hl), "low": c * (1 - hl), "volume": v}
            for i, (c, v) in enumerate(zip(closes, vols))]


# ------------------------------------------------------------------ universe

def test_universe_tags_and_has_no_duplicates():
    u = ah.universe()
    assert 120 <= len(u) <= 200
    for t, tags in u.items():
        assert tags and len(tags) == len(set(tags)), t
    # a name in both the index and the theme keeps both tags
    assert "ndx100" in u["NVDA"] and "ai_compute" in u["NVDA"]
    assert "ndx100" in u["META"] and "ai_platform" in u["META"]
    # theme-only names are present even though they are not in the index
    assert "ndx100" not in u["RRX"] and "humanoid_disclosed" in u["RRX"]
    assert "VTI" in u and "benchmark" in u["VTI"]


def test_ndx_list_has_no_duplicates():
    assert len(ah.NDX) == len(set(ah.NDX))


# ------------------------------------------------------------------ metrics

def test_pct_b_and_sma():
    flat = [100.0] * 25
    pb, band = ah.pct_b(flat)
    assert pb is None and band is not None       # sd == 0 -> undefined, not a crash
    rising = [100.0 + i for i in range(25)]
    pb, (m, u, lo) = ah.pct_b(rising)
    assert 0 < pb <= 1.2 and lo < m < u
    assert ah.sma(rising, 20) == pytest.approx(sum(rising[-20:]) / 20)
    assert ah.sma(rising[:5], 20) is None


def test_atr_pct_needs_enough_bars_and_is_scale_free():
    b = _bars([100.0] * 30, hl=0.02)
    a = ah.atr_pct(b)
    assert a == pytest.approx(0.04, abs=0.005)   # high-low spans 4% of price
    assert ah.atr_pct(_bars([100.0] * 5)) is None
    # same shape at a different price level -> same percentage
    assert ah.atr_pct(_bars([10.0] * 30, hl=0.02)) == pytest.approx(a, abs=1e-6)


def test_hold_period_reports_positive_and_p10():
    up = [100.0 * (1.0004 ** i) for i in range(700)]
    h = ah.hold_period(up, horizon=100)
    assert h["n"] == 600 and h["positive"] == 1.0 and h["p10"] > 0
    assert h["n_independent"] == 7
    assert ah.hold_period([100.0] * 50, horizon=504)["n"] == 0   # too short, no fake number


def test_hold_period_refuses_too_few_independent_windows():
    """GEV reported 100% positive with a 10th percentile of +394% off 119 overlapping
    windows carved from one uptrend — one observation wearing a distribution's clothes."""
    one_cycle = [100.0 * (1.003 ** i) for i in range(623)]      # ~1.2 x a 504-day horizon
    assert ah.hold_period(one_cycle)["n"] == 0                  # refused, not reported
    enough = [100.0 * (1.0005 ** i) for i in range(504 * ah.MIN_INDEPENDENT_WINDOWS + 10)]
    h = ah.hold_period(enough)
    assert h["n"] > 0 and h["n_independent"] >= ah.MIN_INDEPENDENT_WINDOWS


def test_spike_needs_both_move_and_volume():
    base = [100.0] * 25
    # big move, ordinary volume -> not a spike
    assert ah.spike(_bars(base + [110.0])) is None
    # big move AND 3x volume -> spike
    s = ah.spike(_bars(base + [110.0], [1e6] * 25 + [3e6]))
    assert s and s["move"] == pytest.approx(0.10) and s["vol_mult"] == pytest.approx(3.0)
    # ordinary move on huge volume -> not a spike
    assert ah.spike(_bars(base + [100.5], [1e6] * 25 + [9e6])) is None
    # works in both directions
    d = ah.spike(_bars(base + [90.0], [1e6] * 25 + [3e6]))
    assert d and d["move"] < 0


def test_spike_is_nan_safe():
    # NaN is truthy and statistics.mean over a NaN returns NaN, which would make
    # `mult >= SPIKE_VOL` silently False and swallow a real spike. finite() filters the
    # bad bar out instead, so the multiple is computed over the clean ones.
    clean = ah.spike(_bars([100.0] * 25 + [110.0], [1e6] * 25 + [3e6]))
    b = _bars([100.0] * 25 + [110.0], [1e6] * 25 + [3e6])
    b[5]["volume"] = float("nan")
    got = ah.spike(b)
    assert got is not None, "a NaN in the lookback window swallowed a real spike"
    assert math.isfinite(got["vol_mult"])
    assert got["vol_mult"] == pytest.approx(clean["vol_mult"], rel=0.01)


# ------------------------------------------------------------------ classify

def _row(**kw):
    base = {"dollar_volume": 5e8, "pct_b": 0.5, "vs_sma200": 0.05,
            "hold": {"positive": 0.9, "p10": -0.02, "n": 1000}}
    base.update(kw)
    return base


def test_liquidity_is_the_first_gate():
    v, why = ah.classify(_row(dollar_volume=1000.0, pct_b=0.01))
    assert v == "illiquid" and "liquidity gate" in why[0]
    # unknown volume is not liquid either
    assert ah.classify(_row(dollar_volume=None, pct_b=0.01))[0] == "illiquid"


def test_buy_zone_requires_the_hold_gate():
    assert ah.classify(_row(pct_b=0.05))[0] == "buy_zone"
    v, why = ah.classify(_row(pct_b=0.05, hold={"positive": 0.44, "p10": -0.6, "n": 900}))
    assert v == "dip_but_fails_hold" and any("FAILS" in w for w in why)


def test_unmeasurable_hold_is_not_a_failed_hold():
    """A young listing has not FAILED the gate — it has not been measured. Saying
    'FAILS the 1-3 year hold gate' about ALAB/KOID/CCXI was a false reason string."""
    v, why = ah.classify(_row(pct_b=0.05, hold=ah.hold_period([100.0] * 300)))
    assert v == "no_hold_data"
    assert "not measurable" in why[0] and "FAILS" not in why[0]


def test_gates_fail_closed_on_nan():
    # nan < MIN is False, so an unguarded liquidity gate lets NaN through as "liquid"
    assert ah.classify(_row(dollar_volume=float("nan"), pct_b=0.05))[0] == "illiquid"
    assert ah.classify(_row(pct_b=float("nan")))[0] == "no_data"
    assert ah.classify(_row(pct_b=0.05, vs_sma200=float("nan")))[0] == "buy_zone"


def test_an_unadjusted_split_is_rejected_not_ranked_as_the_best_dip():
    # SPLIT_RATIO_RANGE is (0.25, 4.0): a missed 2:1 shows up as a -50% day with %B far
    # below zero, which would sort to the very top of a %B-ascending page.
    v, why = ah.classify(_row(pct_b=-0.59, chg=-0.50, vs_sma200=-0.4))
    assert v == "no_data" and "split" in why[0]
    assert ah.classify(_row(pct_b=-0.30, chg=-0.04))[0] == "buy_zone"   # a real deep dip still passes


def test_downtrend_dip_is_called_out_as_the_best_bucket():
    _, why = ah.classify(_row(pct_b=0.05, vs_sma200=-0.12))
    assert any("200-day" in w and "7.49" in w for w in why)
    _, why_up = ah.classify(_row(pct_b=0.05, vs_sma200=+0.12))
    assert not any("200-day" in w for w in why_up)


def test_extended_and_neutral():
    assert ah.classify(_row(pct_b=1.2))[0] == "extended"
    assert ah.classify(_row(pct_b=0.5))[0] == "neutral"
    assert ah.classify(_row(pct_b=None))[0] == "no_data"


def test_thresholds_match_the_documented_measurements():
    # these constants are quoted in the page copy and the docstring; drift would make the
    # page cite an edge it no longer applies
    assert ah.DIP_PCT_B == 0.20 and ah.EXTENDED_PCT_B == 1.00
    assert ah.SPIKE_MOVE == 0.05 and ah.SPIKE_VOL == 2.0
    assert ah.HOLD_MIN_POSITIVE == 0.80 and ah.HOLD_MIN_P10 == -0.10


# ------------------------------------------------------------------ render

def _data(rows):
    return {"generated_at": "2026-09-23T22:40:00+00:00", "as_of": "2026-09-22",
            "n_universe": 145, "n_resolved": 144, "n_stale": 0,
            "thresholds": {"dip_pct_b": 0.2, "extended_pct_b": 1.0, "spike_move": 0.05,
                           "spike_vol": 2.0, "min_dollar_volume": 2e6,
                           "hold_min_positive": 0.8, "hold_min_p10": -0.1},
            "rows": rows}


def _full(**kw):
    r = {"ticker": "TEST", "tags": ["ndx100", "ai_compute"], "as_of": "2026-09-22",
         "close": 100.0, "chg": 0.01, "pct_b": 0.1, "band": {"mid": 99, "upper": 105, "lower": 95},
         "vs_sma20": 0.01, "vs_sma200": -0.05, "range_pos": 0.4, "range_n": 252,
         "atr_pct": 0.03, "dollar_volume": 5e8,
         "hold": {"positive": 0.9, "p10": -0.02, "n": 1000, "n_independent": 4},
         "spike": None, "stale": False, "verdict": "buy_zone", "why": []}
    r.update(kw)
    return r


def test_render_produces_a_page_and_escapes_content():
    html = ar.render(_data([_full(), _full(ticker="X<script>", verdict="extended", pct_b=1.3)]))
    assert html.startswith("<!doctype html>") and html.rstrip().endswith("</html>")
    assert "<script>" not in html.split("<style>")[0]
    assert "X&lt;script&gt;" in html
    assert "AI &amp; Humanoid Screen" in html


def test_render_survives_every_field_being_none():
    r = _full(close=None, chg=None, pct_b=None, vs_sma20=None, vs_sma200=None,
              range_pos=None, range_n=None, atr_pct=None, dollar_volume=None,
              hold={}, verdict="no_data")
    html = ar.render(_data([r]))
    assert "—" in html and "nan" not in html.lower()


def test_render_tags_a_stale_row_with_its_own_date():
    """98 of 144 rows were a session older than the header on the first live run."""
    html = ar.render(_data([_full(stale=True, as_of="2026-09-21"),
                            _full(ticker="FRESH", stale=False)]))
    assert "2026-09-21" in html and 'class="stale"' in html


def test_render_marks_a_short_range_window_honestly():
    html = ar.render(_data([_full(range_pos=0.9, range_n=70)]))
    assert "(70d)" in html          # not presented as a 52-week reading


def test_render_shows_the_spike_and_the_meta_evidence():
    html = ar.render(_data([_full(spike={"date": "2026-09-23", "move": 0.071, "vol_mult": 3.0})]))
    assert "SPIKE" in html and "3.0x" in html
    assert "Muse" in html and "+35.4%" in html and "+0.4%" in html


def test_render_empty_groups_do_not_break():
    html = ar.render(_data([]))
    assert "Nothing in this group today." in html


def test_leveraged_and_inverse_products_never_get_a_dip_verdict():
    """SOXL/SOXS are daily-reset products. The +3.47/+7.49 pt figures are long-only cash-
    equity statistics, and on SOXS a LOW %B means the underlying is strong -- the opposite
    of 'on sale'. classify() must refuse to rate them rather than rely on a caption."""
    lev = ah.classify(_row(pct_b=0.05, vs_sma200=-0.3), tags=["semis_etf", "leveraged"])
    assert lev[0] == "leveraged" and "do not transfer" in lev[1][0]
    inv = ah.classify(_row(pct_b=-0.16), tags=["semis_etf", "leveraged", "inverse"])
    assert inv[0] == "leveraged" and "INVERSE" in inv[1][0]
    # and the long-only reasons must not appear
    assert "3.47" not in " ".join(lev[1]) and "7.49" not in " ".join(lev[1])
    # an ordinary name is unaffected
    assert ah.classify(_row(pct_b=0.05), tags=["ndx100"])[0] == "buy_zone"


def test_every_leveraged_member_is_tagged():
    u = ah.universe()
    for t in ah.AI_HUMANOID["leveraged"]:
        assert "leveraged" in u[t], t
    assert "inverse" in u["SOXS"] and "inverse" not in u.get("SOXL", [])


def test_pinned_member_that_fails_to_resolve_still_renders():
    """These sections promise 'tracked every day whether or not they signal' -- a silently
    missing ticker reads as 'no signal' when the feed actually dropped it."""
    rows = [_full(ticker="SOXX", tags=["semis_etf"])]          # SOXL and SOXS absent
    got = ar._by_tag(rows, "semis_etf")
    assert [r["ticker"] for r in got] == ["SOXX", "SOXL", "SOXS"]
    assert got[1]["close"] is None and got[1]["why"] == ["did not resolve"]
    html = ar.render(_data(rows))
    assert "SOXL" in html and "SOXS" in html


def test_reference_session_skips_a_thinly_covered_day():
    """Yahoo fills a session's bars in over the following hours. On 2026-09-23 at 17:00 PT,
    2026-09-22 existed for 33% of the universe and 2026-09-23 for 100% -- anchoring on the
    newest date ANY ticker reached flagged 98 of 144 rows stale for no useful reason."""
    bars = {"A": [{"date": "2026-09-21"}, {"date": "2026-09-22"}, {"date": "2026-09-23"}]}
    for k in "BCDE":
        bars[k] = [{"date": "2026-09-21"}, {"date": "2026-09-23"}]   # no 09-22
    ref, seen = ah.reference_session(bars)
    assert seen["2026-09-22"] == 1 and seen["2026-09-23"] == 5
    assert ref == "2026-09-23"                       # 09-22 is 20%, below the 80% bar


def test_reference_session_steps_back_when_the_newest_is_thin():
    bars = {k: [{"date": "2026-09-21"}, {"date": "2026-09-22"}] for k in "ABCDE"}
    bars["A"].append({"date": "2026-09-23"})          # only 1 of 5 has the newest day
    assert ah.reference_session(bars)[0] == "2026-09-22"


def test_reference_session_handles_empty_and_all_thin():
    assert ah.reference_session({})[0] is None
    # every day thin -> fall back to the newest rather than returning nothing
    bars = {"A": [{"date": "2026-01-02"}], "B": [{"date": "2026-01-01"}]}
    assert ah.reference_session(bars, coverage=0.99)[0] == "2026-01-02"


def test_build_never_renders_a_row_ahead_of_as_of():
    """A ticker with a bar the reference session does not include must be trimmed, not
    shown one session ahead of the page's own header."""
    def bars_for(dates):
        return [{"date": d, "open": 100.0, "high": 101.0, "low": 99.0,
                 "close": 100.0 + i, "volume": 1e6} for i, d in enumerate(dates)]
    common = [f"2026-0{1 + i // 28}-{i % 28 + 1:02d}" for i in range(40)]
    b = {t: bars_for(common) for t in ("AAA", "BBB", "CCC", "DDD", "EEE")}
    b["AAA"] = bars_for(common + ["2026-03-01"])       # one ticker runs a session ahead
    data = ah.build({t: ["ndx100"] for t in b}, b, social=False)
    assert data["as_of"] == common[-1]
    assert all(r["as_of"] == data["as_of"] for r in data["rows"])
    assert data["n_stale"] == 0
