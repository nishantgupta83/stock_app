"""Pure-function tests for the intraday checkpoint pings. No network, no clock."""
from datetime import date, datetime, time, timezone
from pathlib import Path
import re
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import semis_intraday as si  # noqa: E402
import semis_brief as sb  # noqa: E402

D = date(2026, 9, 22)


def _bar(hhmm, o, h, lo, c, v=1000.0):
    hh, mm = hhmm
    return {"ts": datetime(2026, 9, 22, hh, mm, tzinfo=si.ET),
            "open": o, "high": h, "low": lo, "close": c, "volume": v}


# ------------------------------------------------------------------ slot routing

def test_slot_tolerates_a_late_cron_and_a_slightly_early_pinger():
    assert si.slot_for(datetime(2026, 9, 22, 6, 35, tzinfo=si.PT)) == "open"
    assert si.slot_for(datetime(2026, 9, 22, 6, 41, tzinfo=si.PT)) == "open"    # late cron
    assert si.slot_for(datetime(2026, 9, 22, 6, 34, tzinfo=si.PT)) == "open"    # early pinger
    assert si.slot_for(datetime(2026, 9, 22, 6, 32, tzinfo=si.PT)) is None      # too early
    assert si.slot_for(datetime(2026, 9, 22, 6, 48, tzinfo=si.PT)) is None      # too late
    assert si.slot_for(datetime(2026, 9, 22, 7, 0, tzinfo=si.PT)) == "t1000et"
    assert si.slot_for(datetime(2026, 9, 22, 8, 0, tzinfo=si.PT)) == "t1100et"


def test_slot_windows_never_overlap():
    # nominal times are >= 25 min apart; the window is [-2, +12]
    for a in si.SLOTS.values():
        for b in si.SLOTS.values():
            if a != b:
                gap = abs((a.hour * 60 + a.minute) - (b.hour * 60 + b.minute))
                assert gap > si.SLOT_TOLERANCE_MIN - si.SLOT_EARLY_MIN


def test_slot_rejects_weekends_and_market_holidays():
    assert datetime(2026, 9, 26, 7, 0).weekday() == 5
    assert si.slot_for(datetime(2026, 9, 26, 7, 0, tzinfo=si.PT)) is None
    assert si.slot_for(datetime(2026, 9, 27, 7, 0, tzinfo=si.PT)) is None
    # Thanksgiving 2026 is a Thursday -- passes the weekday check, must still not ping
    assert datetime(2026, 11, 26, 7, 0).weekday() == 3
    assert sb.is_trading_day(date(2026, 11, 26)) is False
    assert si.slot_for(datetime(2026, 11, 26, 7, 0, tzinfo=si.PT)) is None


def test_every_utc_cron_maps_to_exactly_one_slot_in_both_dst_states():
    """The five workflow crons must cover all three slots exactly once per day, in PDT and
    in PST, with no cron serving two slots. 15:00Z deliberately serves 08:00 PDT / 07:00 PST."""
    # Parsed with a regex, not PyYAML: this test guards a real invariant and must RUN in
    # CI. Importing yaml here made it ModuleNotFoundError on every CI run from 2026-09-22
    # onward while passing locally, because requirements-dev.txt has no pyyaml.
    wf = (Path(__file__).resolve().parent.parent
          / ".github/workflows/semis_intraday.yml").read_text()
    crons = re.findall(r'^\s*-\s*cron:\s*"([^"]+)"', wf, re.M)
    assert len(crons) == 5
    for day, label in ((date(2026, 9, 24), "PDT"), (date(2026, 12, 3), "PST")):
        hit = []
        for c in crons:
            m, h = c.split()[0], c.split()[1]
            utc = datetime(day.year, day.month, day.day, int(h), int(m), tzinfo=timezone.utc)
            s = si.slot_for(utc.astimezone(si.PT))
            if s:
                hit.append(s)
        assert sorted(hit) == sorted(si.SLOTS), f"{label}: got {hit}"


def test_opening_range_break_is_silent_until_the_range_closes():
    bars = [_bar((9, 30), 100, 105, 99, 104), _bar((9, 35), 104, 108, 103, 108)]
    a = si.assess("SOXL", bars, [], 25)
    assert a["opening_range"]["complete"] is False
    assert a["or_break"] is None        # a bar cannot break a range it defines
    bars.append(_bar((10, 5), 108, 120, 108, 119))
    a = si.assess("SOXL", bars, [], 25)
    assert a["opening_range"]["complete"] is True and a["or_break"] == "above"


# ------------------------------------------------------------------ session / vwap

def test_session_bars_drops_premarket_and_other_days():
    bars = [_bar((7, 0), 1, 1, 1, 1),        # premarket
            _bar((9, 30), 1, 1, 1, 1),
            _bar((15, 55), 1, 1, 1, 1),
            {"ts": datetime(2026, 9, 21, 10, 0, tzinfo=si.ET),
             "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}]
    got = si.session_bars(bars, D)
    assert [b["ts"].hour for b in got] == [9, 15]


def test_running_vwap_is_volume_weighted_typical_price():
    bars = [_bar((9, 30), 100, 102, 98, 100, v=100),
            _bar((9, 35), 100, 110, 100, 110, v=300)]
    vw = si.running_vwap(bars)
    assert vw[0] == pytest.approx(100.0)                     # (102+98+100)/3
    # second bar tp = (110+100+110)/3 = 106.667, weighted 300 vs 100
    assert vw[1] == pytest.approx((100 * 100 + 106.6667 * 300) / 400, rel=1e-4)


def test_running_vwap_survives_a_zero_volume_open():
    vw = si.running_vwap([_bar((9, 30), 100, 100, 100, 100, v=0),
                          _bar((9, 35), 100, 100, 100, 100, v=50)])
    assert vw[0] is None and vw[1] == pytest.approx(100.0)


def test_vwap_break_needs_a_CLOSE_below_not_a_wick():
    # bar 2 dips to 90 intrabar but closes above VWAP -> not a break
    bars = [_bar((9, 30), 100, 100, 100, 100, v=100),
            _bar((9, 35), 100, 101, 90, 101, v=100)]
    ev = si.vwap_events(bars, si.running_vwap(bars))
    assert ev["n_below"] == 0 and ev["first_below"] is None
    bars.append(_bar((9, 40), 100, 100, 95, 95, v=100))
    ev = si.vwap_events(bars, si.running_vwap(bars))
    assert ev["first_below"] == 2


# ------------------------------------------------------------------ opening range

def test_opening_range_is_0930_to_1000_and_knows_when_it_is_complete():
    bars = [_bar((9, 30), 100, 105, 99, 104), _bar((9, 55), 104, 107, 103, 106)]
    orng = si.opening_range(bars)
    assert orng["high"] == 107 and orng["low"] == 99 and orng["complete"] is False
    bars.append(_bar((10, 0), 106, 120, 106, 119))
    orng = si.opening_range(bars)
    assert orng["high"] == 107 and orng["complete"] is True   # the 10:00 bar is excluded


# ------------------------------------------------------------------ atr / destitch

def test_atr_is_wilder_and_needs_enough_bars():
    bars = [{"date": f"d{i}", "high": 11.0, "low": 9.0, "close": 10.0} for i in range(20)]
    assert si.atr(bars) == pytest.approx(2.0)
    assert si.atr(bars[:5]) is None


def test_destitch_ohlcv_fixes_high_low_and_inverts_volume():
    # 10:1 reverse split between the two bars: price x10, share count /10
    daily = [{"date": "2026-05-22", "open": 100.0, "high": 110.0, "low": 90.0,
              "close": 100.0, "volume": 1_000_000.0},
             {"date": "2026-05-26", "open": 10.0, "high": 11.0, "low": 9.0,
              "close": 10.0, "volume": 100_000.0}]
    out = si.destitch_ohlcv(daily)
    assert out[1] == daily[1]                                  # present scale untouched
    assert out[0]["close"] == pytest.approx(10.0)
    assert out[0]["high"] == pytest.approx(11.0)
    assert out[0]["low"] == pytest.approx(9.0)
    assert out[0]["volume"] == pytest.approx(10_000_000.0)     # volume scales the other way


def test_destitch_ohlcv_kills_the_fake_true_range_at_the_split_bar():
    # enough bars for ATR(14) to actually compute, with the split in the middle
    pre = [{"date": f"p{i}", "open": 100.0, "high": 101.0, "low": 99.0,
            "close": 100.0, "volume": 1e6} for i in range(10)]
    post = [{"date": f"q{i}", "open": 10.0, "high": 10.1, "low": 9.9,
             "close": 10.0, "volume": 1e5} for i in range(10)]
    raw, fixed = pre + post, si.destitch_ohlcv(pre + post)
    # Wilder decays the spike over the 9 bars that follow it, so the inflated ATR is ~4.35,
    # not ~90 -- but it is still ~20x the true value of the continuous series.
    assert si.atr(raw) > 4
    assert si.atr(fixed) < 0.25
    assert si.atr(raw) > 15 * si.atr(fixed)


def test_destitch_ohlcv_leaves_a_clean_series_alone():
    daily = [{"date": f"d{i}", "open": 100.0 + i, "high": 101.0 + i, "low": 99.0 + i,
              "close": 100.0 + i, "volume": 5.0} for i in range(5)]
    assert si.destitch_ohlcv(daily) == daily


def test_anchored_vwap_anchors_on_the_peak_and_the_trough_after_it():
    daily = [{"date": "d0", "open": 10, "high": 10, "low": 10, "close": 10, "volume": 1},
             {"date": "d1", "open": 20, "high": 20, "low": 20, "close": 20, "volume": 1},  # peak
             {"date": "d2", "open": 5, "high": 5, "low": 5, "close": 5, "volume": 1},      # trough
             {"date": "d3", "open": 15, "high": 15, "low": 15, "close": 15, "volume": 1}]
    a = si.anchors(daily)
    assert a["peak"]["date"] == "d1" and a["trough"]["date"] == "d2"
    assert a["peak"]["avwap"] == pytest.approx((20 + 5 + 15) / 3)
    assert a["trough"]["avwap"] == pytest.approx((5 + 15) / 2)


# ------------------------------------------------------------------ triggers

def _state(**kw):
    base = {"above_vwap": True, "or_break": "inside",
            "vwap_events": {"first_below": None, "n_below": 0, "n_bars": 10}}
    base.update(kw)
    return base


def test_vwap_break_fires_once_not_on_every_later_slot():
    broke = _state(above_vwap=False, vwap_events={"first_below": 3, "n_below": 4, "n_bars": 10})
    assert any("VWAP BREAK" in t for t in si.triggers(broke, _state()))
    # the next slot re-derives the same break -> must not re-fire it
    later = _state(above_vwap=False, vwap_events={"first_below": 3, "n_below": 9, "n_bars": 20})
    assert not any("VWAP BREAK" in t for t in si.triggers(later, broke))


def test_recross_above_vwap_fires():
    prev = _state(above_vwap=False, vwap_events={"first_below": 1, "n_below": 2, "n_bars": 5})
    cur = _state(above_vwap=True, vwap_events={"first_below": 1, "n_below": 2, "n_bars": 8})
    assert "BACK ABOVE VWAP" in si.triggers(cur, prev)


def test_no_prior_slot_does_not_invent_a_recross():
    assert si.triggers(_state(), None) == []


def test_opening_range_break_fires_on_the_transition_only():
    assert "OPENING-RANGE BREAK UP" in si.triggers(_state(or_break="above"), _state())
    assert si.triggers(_state(or_break="above"), _state(or_break="above")) == []


# ------------------------------------------------------------------ render

def test_render_scopes_each_trigger_to_its_ticker():
    # SOXS is the inverse fund: a VWAP break on SOXS is not a VWAP break on SOXL, and the
    # banner must never merge them into one undirected alarm.
    per = {"SOXL": {"price": 150.0, "vwap": 145.0, "vs_vwap": 0.034, "above_vwap": True,
                    "from_open": 0.10, "vwap_events": {"n_below": 0, "n_bars": 78},
                    "opening_range": {"high": 145.0, "low": 137.0, "complete": True},
                    "or_break": "above", "atr": 12.68, "atr_pct": 0.084,
                    "noise_dollars": 317.0, "anchors": {}},
           "SOXS": {"price": 32.0, "vwap": 34.0, "vs_vwap": -0.059, "above_vwap": False,
                    "from_open": -0.09, "vwap_events": {"n_below": 78, "n_bars": 78},
                    "opening_range": {"high": 36.0, "low": 34.0, "complete": True},
                    "or_break": "below", "atr": 4.84, "atr_pct": 0.149,
                    "noise_dollars": 121.0, "anchors": {}}}
    fired = {"SOXL": ["OPENING-RANGE BREAK UP"],
             "SOXS": ["VWAP BREAK — first 5-min close below session VWAP"]}
    subj, text = si.render("t1000et", datetime(2026, 9, 22, 7, 0, tzinfo=si.PT),
                           None, per, fired, 25, 0)
    head = text.splitlines()[0]
    assert "SOXL: OPENING-RANGE BREAK UP" in head
    assert "SOXS: VWAP BREAK" in head
    assert len(text) < sb.TELEGRAM_LIMIT


def test_render_without_triggers_uses_the_slot_label_and_still_sends():
    per = {t: {"price": None} for t in si.TICKERS}
    subj, text = si.render("open", datetime(2026, 9, 22, 6, 35, tzinfo=si.PT),
                           None, per, {}, 25, 0)
    assert si.SLOT_LABEL["open"] in text.splitlines()[0]
    assert "no bars" in text


def test_render_flags_a_late_run():
    per = {t: {"price": None} for t in si.TICKERS}
    _, text = si.render("open", datetime(2026, 9, 22, 6, 41, tzinfo=si.PT),
                        None, per, {}, 25, 6)
    assert "6 min late" in text


def test_splits_feed_catches_a_3_to_1_that_the_ratio_band_cannot_see():
    # SPLIT_RATIO_RANGE is (0.25, 4.0), so a 3:1 forward split never trips it. Without the
    # splits feed the pre-split bars stay on the old scale and anchors()/atr() report an
    # anchored VWAP against a price three times too high.
    pre = [{"date": f"2026-01-{i+1:02d}", "open": 90.0, "high": 91.0, "low": 89.0,
            "close": 90.0, "volume": 1e6} for i in range(10)]
    post = [{"date": f"2026-02-{i+1:02d}", "open": 30.0, "high": 30.3, "low": 29.7,
             "close": 30.0, "volume": 3e6} for i in range(10)]
    raw = pre + post
    assert si.destitch_ohlcv(raw)[0]["close"] == pytest.approx(90.0)          # slips through
    fixed = si.destitch_ohlcv(raw, {"2026-02-01"})
    assert fixed[0]["close"] == pytest.approx(30.0)                           # caught
    assert fixed[0]["volume"] == pytest.approx(3e6)
    assert fixed[-1] == raw[-1]                                              # present scale kept


def test_a_rerun_of_the_same_slot_does_not_realert(tmp_path, monkeypatch):
    """The defect the reviewer found: `prior` excluded the current slot's own record, so a
    second run of a slot saw prev=None and re-fired every trigger."""
    monkeypatch.setattr(si, "STATE", tmp_path)
    broke = {"above_vwap": False, "or_break": "inside",
             "vwap_events": {"first_below": 3, "n_below": 4, "n_bars": 10}}
    order = list(si.SLOTS)
    slot = "t1000et"
    book = {"date": "2026-09-22", "slots": {slot: {"per": {"SOXL": broke}}},
            "fired": ["SOXL:VWAP BREAK — first 5-min close below session VWAP"]}
    # the current slot's own record is now in scope
    prior = [book["slots"][s] for s in order[:order.index(slot) + 1] if s in book["slots"]]
    assert prior and prior[-1]["per"]["SOXL"] == broke
    assert si.triggers(broke, prior[-1]["per"]["SOXL"]) == []
    # and even with prev lost entirely, the day ledger suppresses the repeat
    already = set(book["fired"])
    assert [x for x in si.triggers(broke, None) if f"SOXL:{x}" not in already] == []
