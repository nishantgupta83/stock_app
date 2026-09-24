"""Pure-function tests for the AI/humanoid screen. No network, no clock."""
from datetime import date, timedelta
from pathlib import Path
import json
import math
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import ai_humanoid_screen as ah  # noqa: E402
import ai_humanoid_render as ar  # noqa: E402
import semis_brief as sb  # noqa: E402


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


# ------------------------------------------------------------ SOXX movers / trend

def _tdays(n, end="2026-09-23"):
    """The last n real trading days ending at `end`. The trend axis is the MARKET CALENDAR, so
    synthetic bars on weekends would (correctly) fall off it."""
    d, out = date.fromisoformat(end), []
    while len(out) < n:
        if sb.is_trading_day(d):
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return out[::-1]


def _series(n, start=100.0, step=0.5):
    return [{"date": dt, "open": start + i * step, "high": start + i * step + 1,
             "low": start + i * step - 1, "close": start + i * step, "volume": 1e6}
            for i, dt in enumerate(_tdays(n))]


def _top10_bars(n=30, **overrides):
    bars = {t: _series(n) for t in ah.SOXX_WEIGHTS}
    bars.update(overrides)
    return bars


def test_soxx_weights_and_universe_agree():
    """The weights table and the universe's soxx_top10 group are two lists of the same ten
    names; drift between them would silently drop a holding from the attribution."""
    assert set(ah.SOXX_WEIGHTS) == set(ah.AI_HUMANOID["soxx_top10"])
    assert all(0 < w < 20 for w in ah.SOXX_WEIGHTS.values())


def test_soxx_trend_needs_most_of_the_top_ten():
    few = {t: _series(30) for t in list(ah.SOXX_WEIGHTS)[:5]}
    assert ah.soxx_trend(few, None) is None
    assert ah.soxx_trend({}, None) is None


def test_soxx_trend_composite_is_the_weighted_return():
    t = ah.soxx_trend(_top10_bars(), None)
    assert t and t["n"] == 10 and len(t["composite"]) == ah.TREND_DAYS
    # every name rises 0.5/day off ~100-115, so the composite must be positive every day
    assert all(x > 0 for x in t["composite"]) and all(b == 1.0 for b in t["breadth_up"])
    assert t["above_20d_weight"] == pytest.approx(1.0)


def test_soxx_trend_flags_a_name_below_its_own_20d_and_names_the_leader():
    bars = _top10_bars()
    falling = _series(30, start=120.0, step=-1.5)         # AVGO collapsing
    bars["AVGO"] = falling
    t = ah.soxx_trend(bars, None)
    assert t["latest"]["AVGO"]["above_20d"] is False
    assert t["latest"]["NVDA"]["above_20d"] is True
    assert 0 < t["above_20d_weight"] < 1
    assert t["leader"] == "AVGO"                           # biggest |weight x move| today
    assert t["breadth_up"][-1] < 1.0


def test_soxx_trend_never_reads_past_the_reference_session():
    bars = _top10_bars(n=30)
    ref = bars["NVDA"][-3]["date"]                         # pretend two sessions are not yet published
    t = ah.soxx_trend(bars, ref)
    assert t["dates"][-1] == ref


def test_soxx_trend_is_finite_when_prices_are_flat():
    flat = {t: [dict(b, close=100.0, open=100.0) for b in _series(30)] for t in ah.SOXX_WEIGHTS}
    t = ah.soxx_trend(flat, None)
    assert all(math.isfinite(x) for x in t["composite"])
    assert t["leader_share"] is None or math.isfinite(t["leader_share"])


def test_spark_and_composite_bars_never_divide_by_zero():
    assert "—" in ar._spark([1.0, 2.0])                    # too short -> placeholder
    assert "<svg" in ar._spark([5.0] * 21)                 # flat: hi == lo
    assert "sp-up" in ar._spark([1.0, 2.0, 3.0]) and "sp-dn" in ar._spark([3.0, 2.0, 1.0])
    assert ar._composite_bars([], []) == ""
    assert "<svg" in ar._composite_bars(["d1", "d2"], [0.0, 0.0])      # all-zero max guard


def _trend_data(**over):
    bars = _top10_bars()
    bars["AVGO"] = _series(30, start=120.0, step=-1.5)
    data = _data([_full(ticker="SOXX", tags=["semis_etf"], pct_b=1.01)])
    data["soxx_trend"] = ah.soxx_trend(bars, None)
    data.update(over)
    return data


def test_movers_section_shows_every_holding_and_the_below_20d_flag():
    html = ar._soxx_section(_trend_data())
    for tk in ah.SOXX_WEIGHTS:
        assert 'data-tk="%s"' % tk in html
    assert "BELOW 20d" in html and "above 20d" in html
    assert "refresh-soxx" in html and "cbars" in html


def test_movers_section_states_that_breadth_is_not_a_signal():
    """The section exists to explain a move, not to call SOXL vs SOXS. The measured numbers
    that justify that must be on the page, or the table reads as a signal."""
    html = ar._soxx_section(_trend_data())
    assert "+0.04 pts" in html and "context, not a trigger" in html
    assert "soxx_breadth_study.py" in html                 # every quoted number is reproducible
    assert "−0.09 pts</b>." not in html                    # the retracted claim must not return


def test_movers_read_follows_soxx_own_pct_b():
    assert "stall zone" in ar._soxx_section(_trend_data())
    low = _data([_full(ticker="SOXX", tags=["semis_etf"], pct_b=0.10)])
    low["soxx_trend"] = _trend_data()["soxx_trend"]
    assert "dip band" in ar._soxx_section(low)
    mid = _data([_full(ticker="SOXX", tags=["semis_etf"], pct_b=0.55)])
    mid["soxx_trend"] = _trend_data()["soxx_trend"]
    assert "middle" in ar._soxx_section(mid)


def test_movers_section_degrades_without_data():
    d = _data([])
    d["soxx_trend"] = None
    assert "Not enough" in ar._soxx_section(d)
    assert "What is moving SOXX" in ar.render(d)           # and the page still renders


def test_movers_sits_between_the_semis_and_megacap_sections():
    html = ar.render(_trend_data())
    assert html.index("Semis —") < html.index("What is moving SOXX") < html.index("Mega caps")


# ---- regressions from the SOXX movers review (each one reproduced against live data first)

def _drop(bars, date):
    return [b for b in bars if b["date"] != date]


def test_a_ticker_missing_a_session_is_not_misdated_into_the_composite():
    """C1. KLAC had no 2026-09-22 bar; lined up by list position, its '1d +2.11%' was really a
    two-session move and every earlier composite bar averaged its PREVIOUS session under
    NVDA's date. Returns must be keyed on calendar dates."""
    bars = _top10_bars(n=30)
    gap = bars["KLAC"][-2]["date"]                       # KLAC lacks the second-to-last session
    bars["KLAC"] = _drop(bars["KLAC"], gap)
    t = ah.soxx_trend(bars, None)
    i = t["dates"].index(gap)
    assert t["series"]["KLAC"]["rets"][i] is None                    # no return on the missing day
    assert t["series"]["KLAC"]["rets"][i + 1] is None                # ...nor across the gap
    assert all(t["series"][k]["rets"][i] is not None for k in t["series"] if k != "KLAC")
    # that day's composite is averaged over the NINE names that have it, not nine plus a misdated tenth
    others = [k for k in t["series"] if k != "KLAC"]
    ws = sum(ah.SOXX_WEIGHTS[k] for k in others)
    expect = sum(ah.SOXX_WEIGHTS[k] * t["series"][k]["rets"][i] for k in others) / ws
    assert t["composite"][i] == pytest.approx(expect, abs=1e-5)


def test_a_stale_ticker_has_no_fake_today_and_is_left_out_of_the_tiles():
    """C1b. A ticker whose last bar is older than the reference session must not have its
    PREVIOUS session's return reported as today's."""
    bars = _top10_bars(n=30)
    bars["MU"] = bars["MU"][:-1]                          # MU has no bar for the latest session
    t = ah.soxx_trend(bars, None)
    assert t["latest"]["MU"]["fresh"] is False and t["latest"]["MU"]["ret1"] is None
    assert "MU" in t["stale_names"] and t["n_today"] == t["n"] - 1
    assert t["fund_weight_today"] == pytest.approx(sum(ah.SOXX_WEIGHTS.values()) - ah.SOXX_WEIGHTS["MU"])
    html = ar._soxx_section(dict(_trend_data(), soxx_trend=t))
    assert 'class="pill p-st"' in html and "MU (no bar for" in html.replace("\n", " ")


def test_leader_share_is_a_share_of_gross_movement_never_above_100_percent():
    """C2. Nine names +1% and NVDA -4.2% gave a signed total of +0.13%, and the page printed
    'NVDA carried 320% of the move' for a name that moved AGAINST the day."""
    bars = {t: _series(30, start=100.0, step=1.0) for t in ah.SOXX_WEIGHTS}
    # prior close 128, last close 124 (-3.1%): big enough to lead, small enough that the nine
    # risers keep the SIGNED total positive -- the case where the old ratio blew past 100%
    bars["NVDA"][-1] = dict(bars["NVDA"][-1], close=124.0)
    t = ah.soxx_trend(bars, None)
    assert t["leader"] == "NVDA" and t["leader_against"] is True
    assert 0.0 <= t["leader_share"] <= 1.0
    txt = ar._leader_text(t["leader"], t["leader_contrib"], t["leader_share"], t["leader_against"])
    assert "against the day" in txt and "%" in txt
    assert not any(int(x) > 100 for x in __import__("re").findall(r"(\d+)% of", txt))


def test_leader_text_with_a_same_direction_leader_reports_gross_share():
    txt = ar._leader_text("MU", -0.00197, 0.24, False)
    assert txt.startswith("MU -0.20%") and "24% of gross movement" in txt
    assert ar._leader_text(None, None, None, False) == "—"


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _js_fn(name):
    """Pull a TOP-LEVEL function out of the shipped page script, verbatim."""
    import re
    m = re.search(r"^function %s\(.*?^}\n" % name, ar.SCRIPT, re.S | re.M)
    assert m, "function %s is no longer a top-level pure function in the page script" % name
    return m.group(0)


def _node(names, expr):
    src = "\n".join(_js_fn(n) for n in names) + "\nconsole.log(JSON.stringify(%s));" % expr
    r = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def _rec(tk, pair="2026-09-23>2026-09-24"):
    return {"tk": tk, "asof": pair.split(">")[1], "pair": pair}


@needs_node
def test_refresh_lets_a_complete_same_session_fetch_update_the_tiles():
    got = [_rec(t) for t in ah.SOXX_WEIGHTS]
    v = _node(["refreshVerdict"], "refreshVerdict(%s, 10)" % json.dumps(got))
    assert v["complete"] is True and v["kind"] == "ok"


@needs_node
def test_refresh_refuses_when_one_ticker_is_missing_its_previous_bar():
    """F2. KLAC has today's bar but not yesterday's: its as_of equals its neighbours', so a
    last-date-only guard passes it, while its 1d is really a two-session move. The guard must
    compare the (previous, latest) PAIR."""
    got = [_rec(t) for t in ah.SOXX_WEIGHTS]
    got[-1] = _rec("KLAC", "2026-09-22>2026-09-24")          # same latest date, different previous
    assert len({g["asof"] for g in got}) == 1                # the old guard could not see this
    v = _node(["refreshVerdict"], "refreshVerdict(%s, 10)" % json.dumps(got))
    assert v["complete"] is False and v["kind"] == "mixed" and len(v["pairs"]) == 2


@needs_node
def test_refresh_refuses_partial_mixed_and_empty_fetches():
    ten = [_rec(t) for t in ah.SOXX_WEIGHTS]
    run = lambda got: _node(["refreshVerdict"], "refreshVerdict(%s, 10)" % json.dumps(got))
    assert run(ten[:9])["kind"] == "partial" and run(ten[:9])["complete"] is False
    assert run([])["kind"] == "none"
    mixed = ten[:9] + [_rec("KLAC", "2026-09-23>2026-09-25")]
    assert run(mixed)["kind"] == "mixed"


@needs_node
def test_a_complete_refresh_then_a_partial_one_is_not_treated_as_complete():
    """F4. After a complete refresh a partial second click must NOT be complete, which is the
    branch that restores the nightly tiles instead of leaving click 1's numbers under a note
    that says 'nightly'. The verdict half is executed under node; the restore half is only a
    STRUCTURAL check (the DOM behaviour was verified by hand under jsdom during review, and no
    DOM harness lives in the repo)."""
    ten = [_rec(t) for t in ah.SOXX_WEIGHTS]
    first = _node(["refreshVerdict"], "refreshVerdict(%s, 10)" % json.dumps(ten))
    second = _node(["refreshVerdict"], "refreshVerdict(%s, 10)" % json.dumps(ten[:8]))
    assert first["complete"] and not second["complete"]
    html = ar.render(_trend_data())
    # structural: the restore writes BOTH the text and the class (pos/neg colour) back
    assert "restoreTiles()" in html and "el.textContent = saved[id].t; el.className = saved[id].c" in html


@needs_node
def test_js_and_python_leader_text_agree():
    """The same sentence is produced twice (server-side and in the refresh). They must match."""
    cases = [("MU", -0.00197, 0.24, False), ("NVDA", -0.0039, 0.42, True),
             ("KLAC", 0.00087, 0.31, False), ("AVGO", 0.0, None, False)]
    for lead, contrib, share, against in cases:
        # reconstruct the JS inputs from the same facts: gross from share, total from `against`
        gross = (abs(contrib) / share) if share else 0
        total = (-contrib if against else contrib)
        if against and total == 0:
            continue
        js = _node(["leaderText"], "leaderText(%s, %r, %r, %r)" % (json.dumps(lead), contrib, gross, total))
        assert js == ar._leader_text(lead, contrib, share, against), (js, lead)


def test_soxs_is_never_presented_as_favoured_by_a_stall():
    """C4. Under a 'SOXL vs SOXS' heading a bold negative number reads as a SOXS cue."""
    html = ar._soxx_section(_trend_data())                # SOXX %B 1.01 -> stall branch
    assert "stall, not a short" in html and "nothing measured here favours SOXS" in html
    assert "not rated on this page" in html


def test_tile_labels_state_their_weighting_basis():
    """P2. The contribution tile uses FUND weights, the bar chart renormalises to 100% — the
    same session shows -0.73% and -1.18% and both are right, so each must say why."""
    html = ar._soxx_section(_trend_data())
    assert "fund weight × its move" in html and "renormalised to 100%" in html


def test_thresholds_in_the_movers_copy_come_from_the_data():
    d = _trend_data()
    d["thresholds"] = dict(d["thresholds"], dip_pct_b=0.15, extended_pct_b=1.20)
    d["rows"] = [_full(ticker="SOXX", tags=["semis_etf"], pct_b=0.17)]     # inside the OLD dip band only
    html = ar._soxx_section(d)
    assert "middle" in html and "dip band" not in html


def test_the_vs20_cell_holds_exactly_one_pill_so_it_cannot_push_the_table_off_a_phone():
    """A 'no bar today' pill inside the LAST column widened it enough to scroll the whole
    column out of view at 430px -- hiding AVGO's BELOW 20d flag, the most useful cell in the
    table. Nothing but a screenshot noticed. Markers belong in the ticker cell."""
    import re
    bars = _top10_bars()
    bars["AVGO"] = _series(30, start=120.0, step=-1.5)
    bars["MU"] = bars["MU"][:-1]                                # a stale name too
    html = ar._soxx_section(dict(_trend_data(), soxx_trend=ah.soxx_trend(bars, None)))
    cells = re.findall(r'<td class="c-vs20">(.*?)</td>', html)
    assert len(cells) == 10
    assert all(c.count('class="pill') == 1 for c in cells), cells
    stale_row = re.search(r'<tr data-tk="MU".*?</tr>', html, re.S).group(0)
    assert 'p-st' in stale_row.split('c-vs20')[0]               # marker sits before that column


# ---- regressions from the SECOND review: the axis itself must not skip a session

def _dropped(bars, date_):
    return [b for b in bars if b["date"] != date_]


def test_a_thinly_covered_session_stays_on_the_axis_so_no_1d_becomes_a_two_session_move():
    """F1. Yahoo fills a session's bars in over hours: 2026-09-22 existed for 33% of the
    universe while 2026-09-23 had 100%. Deriving the axis from 'dates >= 80% of holdings have'
    dropped that session, so every name's '1d' silently spanned two sessions and all ten still
    counted as fresh with no warning."""
    D = _tdays(30)
    bars = {t: _series(30) for t in ah.SOXX_WEIGHTS}
    thin = list(ah.SOXX_WEIGHTS)[:7]                         # 7 of 10 lack the second-to-last day
    for t in thin:
        bars[t] = _dropped(bars[t], D[-2])
    t = ah.soxx_trend(bars, None)
    assert D[-2] in t["dates"]                              # the calendar, not the data, defines the axis
    # the seven that lack D[-2] have NO 1d today (it would span D[-3] -> D[-1])
    for name in thin:
        assert t["latest"][name]["ret1"] is None and name in t["stale_names"]
        assert "span a gap" in t["stale_reasons"][name]
    assert t["n_today"] == 3                                # only the three with both sessions
    real = [n for n in ah.SOXX_WEIGHTS if n not in thin]
    for name in real:
        assert t["latest"][name]["ret1"] == pytest.approx(
            bars[name][-1]["close"] / bars[name][-2]["close"] - 1)
    # 3 of 10 is a sliver of the fund: the tiles must not pretend otherwise
    assert t["tiles_ok"] is False and t["total_contrib"] is None and t["leader"] is None


def test_a_name_that_lacks_the_latest_session_is_the_stale_one_not_the_others():
    """F3. With the reference session ahead of the holdings, `fresh` was defined by 'is my
    newest bar the axis end', so the seven names that DID have the latest bar were listed as
    stale and the tiles were built from the three laggards."""
    D = _tdays(30)
    bars = {t: _series(30) for t in ah.SOXX_WEIGHTS}
    lag = list(ah.SOXX_WEIGHTS)[:3]
    for t in lag:
        bars[t] = bars[t][:-1]                              # NVDA, MU, AMD lack the latest session
    t = ah.soxx_trend(bars, D[-1])
    assert sorted(t["stale_names"]) == sorted(lag)          # exactly the three that lack it
    assert t["n_today"] == 7
    assert all("no bar for %s" % D[-1] in t["stale_reasons"][n] for n in lag)
    assert t["fund_weight_today"] == pytest.approx(
        sum(w for k, w in ah.SOXX_WEIGHTS.items() if k not in lag))
    # the breadth tile and the contribution tile are over the SAME set of names
    assert t["breadth_up"][-1] is not None and t["total_contrib"] is not None


def test_a_day_with_no_data_is_a_gap_in_the_chart_not_a_zero_bar():
    D = _tdays(30)
    bars = {t: _dropped(_series(30), D[-5]) for t in ah.SOXX_WEIGHTS}     # nobody has D[-5]
    t = ah.soxx_trend(bars, None)
    i = t["dates"].index(D[-5])
    assert t["composite"][i] is None and t["breadth_up"][i] is None
    svg = ar._composite_bars(t["dates"], t["composite"])
    assert svg.count("<rect") == len(t["dates"]) - 2        # that day and the next have no return
    html = ar._soxx_section(dict(_trend_data(), soxx_trend=t))     # and the section still renders
    assert "What is moving SOXX" in html and "nan" not in html.lower()


def test_the_trading_axis_is_the_market_calendar():
    ax = ah._trading_axis("2026-09-23", 6)
    assert ax == ["2026-09-16", "2026-09-17", "2026-09-18", "2026-09-21", "2026-09-22", "2026-09-23"]
    assert all(sb.is_trading_day(date.fromisoformat(d)) for d in ax)
    # a holiday and a weekend are skipped: 2026-09-07 is Labor Day
    assert "2026-09-07" not in ah._trading_axis("2026-09-10", 8)


# ---- regressions from the THIRD review

def test_a_missing_holding_is_accounted_for_not_silently_dropped():
    """With NVDA and MU unresolved the section showed 8 rows, no warning, still said 'top-10',
    and the two largest holdings (18.3% of the fund) were simply absent."""
    bars = {t: _series(30) for t in ah.SOXX_WEIGHTS}
    del bars["NVDA"], bars["MU"]
    t = ah.soxx_trend(bars, None)
    assert t["n"] == 10 and t["n_resolved"] == 8                  # the section is ABOUT ten
    assert {"NVDA", "MU"} <= set(t["stale_names"])
    assert "not resolved" in t["stale_reasons"]["NVDA"]
    assert t["fund_weight_today"] == pytest.approx(
        sum(ah.SOXX_WEIGHTS.values()) - ah.SOXX_WEIGHTS["NVDA"] - ah.SOXX_WEIGHTS["MU"])
    html = ar._soxx_section(dict(_trend_data(), soxx_trend=t))
    assert html.count("<tr data-tk=") == 10                       # every holding still has a row
    assert "NVDA (not resolved" in html and "no data" in html
    assert "of the top-10 fund weight" in html                    # the coverage is stated


def test_a_too_thin_holding_is_reported_with_its_bar_count():
    bars = {t: _series(30) for t in ah.SOXX_WEIGHTS}
    bars["AVGO"] = bars["AVGO"][-10:]
    t = ah.soxx_trend(bars, None)
    assert "AVGO" in t["stale_names"] and "only 10 usable bars" in t["stale_reasons"]["AVGO"]


def test_outside_the_holiday_calendar_the_section_fails_loud():
    """agents/_market_calendar.ALL_HOLIDAYS ends at 2027. Beyond it every weekday looks like a
    session, so MLK Day 2028 became a phantom axis day, every name 'lacked' it, and the section
    blanked out blaming the data for a market closure."""
    d = date(2028, 1, 18)
    bars = {t: [{"date": (d - timedelta(days=i)).isoformat(), "open": 100.0, "high": 101.0,
                 "low": 99.0, "close": 100.0 + i, "volume": 1e6}
                for i in range(45, -1, -1) if (d - timedelta(days=i)).weekday() < 5]
            for t in ah.SOXX_WEIGHTS}
    t = ah.soxx_trend(bars, None)
    assert t and "error" in t and "does not cover 2028" in t["error"]
    html = ar._soxx_section(dict(_trend_data(), soxx_trend=t))
    assert "does not cover 2028" in html and "Extend ALL_HOLIDAYS" in html
    assert "<tr data-tk=" not in html                               # no half-built table


def test_build_no_longer_writes_list_position_contribution_fields():
    """soxx_weight / soxx_contrib were derived from a row's list-position `chg`, read by
    nothing, and could disagree with the section. Dead data that can lie is worse than none."""
    rows = {"AAA": _series(40)}
    data = ah.build({"AAA": ["ndx100"]}, rows, social=False)
    assert all("soxx_contrib" not in r and "soxx_weight" not in r for r in data["rows"])


@needs_node
def test_quote_usable_rejects_null_and_nan_because_isfinite_null_is_true():
    """isFinite(null) is TRUE in JavaScript, so a one-bar quote (chg: null) passed the guard and
    rendered a 1d of dash, a contribution of +0.000%, and BELOW 20d, with a green live stamp."""
    ok = {"as_of": "2026-09-24"}
    run = lambda a: _node(["quoteUsable"], "quoteUsable(%s, %s)" % (json.dumps(a), json.dumps(ok)))
    assert run({"chg": 0.01, "vs20": 0.02}) is True
    assert run({"chg": None, "vs20": 0.02}) is False
    assert run({"chg": 0.01, "vs20": None}) is False
    assert _node(["quoteUsable"], "quoteUsable({chg: NaN, vs20: 0.02}, {as_of: 'x'})") is False
    assert _node(["quoteUsable"], "quoteUsable({chg: 0.01, vs20: 0.02}, {})") is False


@needs_node
def test_live_pair_is_built_from_the_same_bars_analyse_uses():
    """A zero close on the previous bar is dropped by analyse(), so chg becomes a TWO-session
    move; a pair built from the unfiltered bars still read '09-23>09-24' and slipped through as
    a clean complete refresh."""
    clean = [{"d": "2026-09-22", "c": 100.0}, {"d": "2026-09-23", "c": 101.0}, {"d": "2026-09-24", "c": 102.0}]
    assert _node(["livePair"], "livePair(%s)" % json.dumps(clean)) == "2026-09-23>2026-09-24"
    zero = [dict(b) for b in clean]
    zero[1]["c"] = 0.0                                         # previous bar is bad
    pair = _node(["livePair"], "livePair(%s)" % json.dumps(zero))
    assert pair == "2026-09-22>2026-09-24"                     # spans the gap, and SAYS so
    v = _node(["refreshVerdict"], "refreshVerdict(%s, 2)" % json.dumps(
        [{"tk": "A", "pair": "2026-09-23>2026-09-24"}, {"tk": "B", "pair": pair}]))
    assert v["complete"] is False and v["kind"] == "mixed"     # so the tiles are protected
    assert _node(["livePair"], "livePair([{d:'2026-09-24', c: 5}])") == "2026-09-24"
    assert _node(["livePair"], "livePair([])") == ""
