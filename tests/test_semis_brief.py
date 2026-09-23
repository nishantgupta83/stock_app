"""semis_brief — pure-function tests (no network)."""
from __future__ import annotations

import math
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import semis_brief as sb  # noqa: E402

ET, PT = sb.ET, sb.PT
D = date(2026, 9, 18)            # a Friday, trading day


def et(h, m, d=D):
    return datetime(d.year, d.month, d.day, h, m, tzinfo=ET)


# --- window / DST -----------------------------------------------------------------

def test_window_pdt_and_pst():
    assert sb.in_brief_window(datetime(2026, 9, 18, 6, 0, tzinfo=PT))        # PDT
    assert sb.in_brief_window(datetime(2026, 12, 3, 6, 0, tzinfo=PT))        # PST
    assert not sb.in_brief_window(datetime(2026, 9, 18, 6, 25, tzinfo=PT))
    assert not sb.in_brief_window(datetime(2026, 9, 18, 5, 50, tzinfo=PT))


def test_utc_backup_crons_land_in_window_exactly_once_per_dst_state():
    # workflow backups: 13:00 and 14:00 UTC. Summer -> 13:00 hits; winter -> 14:00 hits.
    for d, hit_hour in ((date(2026, 9, 18), 13), (date(2026, 12, 3), 14)):
        hits = [h for h in (13, 14)
                if sb.in_brief_window(datetime(d.year, d.month, d.day, h, 0, tzinfo=timezone.utc).astimezone(PT))]
        assert hits == [hit_hour]


def test_calendar_guard():
    assert sb.calendar_covered(date(2027, 12, 31))
    assert not sb.calendar_covered(date(2028, 1, 3))


# --- snapshot / premarket ---------------------------------------------------------

def test_cutoff_is_capped_at_the_open():
    late = et(10, 40).astimezone(timezone.utc)
    assert sb.snapshot_cutoff(late, D) == et(9, 30).astimezone(timezone.utc)
    early = et(9, 15).astimezone(timezone.utc)
    assert sb.snapshot_cutoff(early, D) == early


def test_premarket_last_excludes_prior_day_regular_and_future_bars():
    bars = [(et(15, 55, date(2026, 9, 17)), 100.0),    # prior day
            (et(3, 55), 101.0),                          # before 04:00
            (et(9, 10), 102.0), (et(9, 15), 103.0),
            (et(9, 30), 999.0)]                          # regular session
    cutoff = et(9, 20).astimezone(timezone.utc)
    p, t, stale = sb.premarket_last(bars, D, cutoff)
    assert p == 103.0 and not stale


def test_premarket_stale_and_nan():
    bars = [(et(8, 0), 101.0), (et(9, 0), float("nan"))]
    p, t, stale = sb.premarket_last(bars, D, et(9, 20).astimezone(timezone.utc))
    assert p == 101.0 and stale                       # NaN skipped; 80 min old -> stale
    assert sb.premarket_last([], D, et(9, 20).astimezone(timezone.utc)) is None


# --- implied open / bias ------------------------------------------------------------

def test_implied_open_renormalises_over_present_names():
    w = {"A": 0.10, "B": 0.05, "C": 0.05}
    r = sb.implied_open(w, {"A": 0.02, "B": -0.01, "C": None})
    assert r["n_used"] == 2 and r["n_missing"] == 1
    assert r["implied_soxx"] == pytest.approx((0.10 * 0.02 + 0.05 * -0.01) / 0.15)
    assert r["coverage"] == pytest.approx(0.75)
    assert sb.implied_open(w, {"A": None})["implied_soxx"] is None


def test_bias_thresholds():
    assert sb.bias_from(0.0025) == "lean_soxl"
    assert sb.bias_from(-0.0025) == "lean_soxs"
    assert sb.bias_from(0.0024) == "no_edge"
    assert sb.bias_from(None) == "no_edge"


# --- news ---------------------------------------------------------------------------

def test_news_window_dedupe_and_tags():
    since, until = et(16, 0, date(2026, 9, 17)).astimezone(timezone.utc), et(9, 15).astimezone(timezone.utc)
    items = [
        {"ticker": "MU", "title": "Micron beats estimates, raises guidance", "link": "", "published": et(16, 30, date(2026, 9, 17))},
        {"ticker": "MU", "title": "Micron BEATS estimates, raises guidance!", "link": "", "published": et(17, 0, date(2026, 9, 17))},
        {"ticker": "INTC", "title": "Intel downgraded at Citi", "link": "", "published": et(7, 0)},
        {"ticker": "NVDA", "title": "Old story", "link": "", "published": et(12, 0, date(2026, 9, 17))},
        {"ticker": "AMD", "title": "Future story", "link": "", "published": et(9, 40)},
    ]
    out = sb.filter_news(items, since, until)
    assert [o["ticker"] for o in out] == ["INTC", "MU"]
    assert {o["ticker"]: o["tag"] for o in out} == {"INTC": -1, "MU": 1}


# --- levels -------------------------------------------------------------------------

def test_gap_fill_rate_sign_aware():
    daily = [{"open": 100, "high": 101, "low": 99, "close": 100},
             {"open": 101.5, "high": 103, "low": 99.5, "close": 102},     # gap up 1.5%, filled
             {"open": 103.5, "high": 105, "low": 103, "close": 104},      # gap up ~1.5%, not filled
             {"open": 102.5, "high": 104.5, "low": 102, "close": 103}]    # gap down ~1.4%, filled
    r = sb.gap_fill_rate(daily, 0.015)
    assert r["bucket"] == "up 1-2%" and r["n"] == 2 and r["fill_rate"] == 0.5
    assert sb.gap_fill_rate(daily, -0.015)["fill_rate"] == 1.0


# --- lookahead guard (known-BAD fixture: must fail) -------------------------------

def test_validate_call_rejects_a_snapshot_at_or_after_the_open():
    good = {"date": D.isoformat(), "premarket": {"NVDA": {"ts": et(9, 25).astimezone(timezone.utc).isoformat()}}}
    sb.validate_call(good)
    bad = {"date": D.isoformat(), "premarket": {"NVDA": {"ts": et(9, 30).astimezone(timezone.utc).isoformat()}}}
    with pytest.raises(ValueError, match="lookahead"):
        sb.validate_call(bad)


# --- grading ------------------------------------------------------------------------

def _call(bias, implied, comps=None):
    return {"date": D.isoformat(), "bias": bias, "implied_soxx": implied,
            "implied_soxl": 3 * implied, "levels": {"soxl_prior_close": 100.0},
            "components": comps or {}}


def test_grade_hit_miss_and_nan_is_pending():
    soxx_up = {"open": 300, "close": 303}
    soxl = {"open": 102, "high": 106, "low": 99, "close": 105}
    g = sb.grade_day(_call("lean_soxl", 0.006, {"news_net": -2}), soxx_up, soxl, {"open": 20, "close": 19}, 301.5)
    assert g["status"] == "graded" and g["bias_hit"] is True
    assert g["component_hits"] == {"news_net": False}
    assert g["gap_filled"] is True                     # gap up, low 99 <= prior close 100
    assert g["soxl_open_error"] == pytest.approx(0.02 - 0.018)
    assert g["fade_gap_hit"] is False
    nan = sb.grade_day(_call("lean_soxl", 0.006), {"open": 300, "close": float("nan")}, soxl, {}, None)
    assert nan["status"] == "pending"


def test_grade_ties_and_no_edge_are_excluded_not_missed():
    g = sb.grade_day(_call("lean_soxs", -0.01), {"open": 300, "close": 300}, {}, {}, None)
    assert g["bias_hit"] is None
    g2 = sb.grade_day(_call("no_edge", 0.001), {"open": 300, "close": 310}, {}, {}, None)
    assert g2["bias_hit"] is None
    # fade-the-gap is scored on no_edge days too (small gap up, day closed up -> fade missed)
    assert g2["fade_gap_hit"] is False


def test_outage_day_is_no_data_not_a_no_edge_decision():
    g = sb.grade_day(_call("no_edge", 0.0) | {"implied_soxx": None, "implied_soxl": None},
                     {"open": 300, "close": 310}, {}, {}, None)
    assert g["status"] == "no_data"


def test_fade_null_is_not_the_mirror_of_the_bias():
    # called day: bias hit; plus a no_edge day where fading a small gap won.
    called = sb.grade_day(_call("lean_soxl", 0.006), {"open": 300, "close": 303}, {}, {}, None)
    quiet = sb.grade_day(_call("no_edge", 0.001), {"open": 300, "close": 297}, {}, {}, None)
    s = sb.scorecard([called, quiet], sent_dates={D.isoformat()})
    # both grades share date D, so scorecard sees both via sent_dates
    assert s["all"]["bias"]["hit_rate"] == 1.0
    assert s["fade_gap_null_all_days"] == {"n": 2, "hits": 1, "hit_rate": 0.5}   # != 1 - bias


def _no_channels(monkeypatch):
    for k in ("GMAIL_USER", "GMAIL_APP_PASSWORD", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(k, raising=False)


def test_prepare_refuses_before_writing_when_no_channel_is_configured(monkeypatch, tmp_path):
    _no_channels(monkeypatch)
    monkeypatch.setattr(sb, "STATE", tmp_path)
    monkeypatch.setattr(sb, "is_trading_day", lambda d: True)
    monkeypatch.setattr(sb, "in_brief_window", lambda now: True)
    called = []
    monkeypatch.setattr(sb, "build_call", lambda ctx: called.append(1))
    assert sb.cmd_prepare(force=False) == 1
    assert called == [] and not any(tmp_path.iterdir())


def test_telegram_alone_is_enough_to_proceed(monkeypatch):
    _no_channels(monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t"); monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    assert sb.channels() == ["telegram"]
    monkeypatch.setenv("GMAIL_USER", "u"); monkeypatch.setenv("GMAIL_APP_PASSWORD", "p")
    assert sb.channels() == ["email", "telegram"]


def test_send_succeeds_if_any_channel_delivers_and_records_which(monkeypatch, tmp_path):
    _no_channels(monkeypatch)
    for k, v in (("GMAIL_USER", "u"), ("GMAIL_APP_PASSWORD", "p"),
                 ("TELEGRAM_BOT_TOKEN", "t"), ("TELEGRAM_CHAT_ID", "c")):
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(sb, "STATE", tmp_path); monkeypatch.setattr(sb, "REPO", tmp_path)
    (tmp_path / "c.json").write_text('{"date": "2026-09-18", "bias": "no_edge"}')
    monkeypatch.setattr(sb, "render", lambda call, score, last: ("S", "T", "H"))
    def boom(*a):
        raise OSError("smtp down")
    sent = []
    monkeypatch.setattr(sb, "send_email", boom)
    monkeypatch.setattr(sb, "send_telegram", lambda *a: sent.append(1) or "telegram")
    assert sb.cmd_send("c.json") == 0 and sent == [1]
    import json as _j
    assert _j.loads((tmp_path / "sent" / "2026-09-18.json").read_text())["channels"] == ["telegram"]
    monkeypatch.setattr(sb, "send_telegram", boom)
    (tmp_path / "sent" / "2026-09-18.json").unlink()
    assert sb.cmd_send("c.json") == 1 and not (tmp_path / "sent" / "2026-09-18.json").exists()


def test_telegram_chunks_respect_the_limit_and_keep_everything():
    text = "\n".join(f"line {i} " + "x" * 50 for i in range(300)) + "\n" + "y" * 9000
    parts = sb.telegram_chunks(text, limit=4000)
    assert all(parts) and all(len(p) <= 4000 for p in parts)
    for exact in ("x" * 10, "x" * 20, "a\n" + "x" * 10):
        got = sb.telegram_chunks(exact, limit=10)
        assert all(got) and all(len(p) <= 10 for p in got), got
    assert "".join(p.replace("\n", "") for p in parts) == text.replace("\n", "")


def test_prepare_skips_when_the_call_already_exists_on_origin(monkeypatch, tmp_path):
    _no_channels(monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t"); monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    monkeypatch.setattr(sb, "is_trading_day", lambda d: True)
    monkeypatch.setattr(sb, "in_brief_window", lambda now: True)
    monkeypatch.setattr(sb, "already_sent_on_origin", lambda rel: True)
    called = []
    monkeypatch.setattr(sb, "build_call", lambda ctx: called.append(1))
    monkeypatch.setattr(sb, "grade_pending", lambda today: called.append("g"))
    assert sb.cmd_prepare(force=False) == 0
    assert called == []                 # no grading, no build, no send


def test_scorecard_counts_only_sent_calls_and_reports_nulls():
    g1 = {"date": "2026-09-16", "status": "graded", "bias": "lean_soxl", "outcome_sign": 1,
          "bias_hit": True, "fade_gap_hit": False, "component_hits": {"news_net": True},
          "soxl_open_to_close": 0.03}
    g2 = {"date": "2026-09-17", "status": "graded", "bias": "lean_soxs", "outcome_sign": 1,
          "bias_hit": False, "fade_gap_hit": True, "component_hits": {"news_net": None},
          "soxs_open_to_close": -0.03}
    g3 = {"date": "2026-09-18", "status": "graded", "bias": "lean_soxl", "outcome_sign": -1,
          "bias_hit": False, "fade_gap_hit": True, "component_hits": {}}
    s = sb.scorecard([g1, g2, g3], sent_dates={"2026-09-16", "2026-09-17"})
    assert s["n_graded"] == 2 and s["all"]["bias"]["hit_rate"] == 0.5
    assert s["fade_gap_null_all_days"]["hit_rate"] == 0.5
    assert s["all"]["up_day_base_rate"]["hit_rate"] == 1.0
    assert s["components"]["news_net"] == {"n": 1, "hits": 1, "hit_rate": 1.0}


def test_render_has_no_nan_and_carries_tags(monkeypatch, tmp_path):
    monkeypatch.setattr(sb, "STATE", tmp_path)
    call = {"date": D.isoformat(), "bias": "lean_soxl", "implied_soxx": 0.006, "implied_soxl": 0.018,
            "implied_soxs": -0.018, "calendar_verified": False, "manual": True,
            "weights": {"NVDA": 0.1}, "premarket": {"NVDA": {"move": 0.01}},
            "implied": {"contrib": {"NVDA": 0.006}, "coverage": 1.0, "n_up": 1, "n_down": 0, "dispersion": 0.0},
            "components": {"overnight_asia_eu": None, "nasdaq_futures": 0.002, "news_net": None, "social_skew": 3},
            "overnight": {}, "nasdaq_futures": 0.002, "news": [], "filings": None, "truth_social": None,
            "social": {"SOXL": None}, "levels": {"soxl_prior_close": 100, "soxl_prior_high": None,
            "soxl_prior_low": None, "soxl_pm_high": None, "soxl_pm_low": None, "soxl_gap": None, "gap_fill": None},
            "catalysts": {"earnings_next_7d": [], "macro_today": None}}
    subj, text, htm = sb.render(call, {}, None)
    assert subj.startswith("[MANUAL] [CALENDAR UNVERIFIED]")
    assert "nan" not in text.lower() and "nan" not in htm.lower()


def test_strategy_returns_follow_vs_fade_compounded():
    rows = [{"bias": "lean_soxl", "soxl_open_to_close": 0.05, "soxs_open_to_close": -0.05},
            {"bias": "lean_soxs", "soxl_open_to_close": 0.03, "soxs_open_to_close": -0.03},
            {"bias": "no_edge", "soxl_open_to_close": 0.10, "soxs_open_to_close": -0.10}]
    r = sb.strategy_returns(rows)
    assert r["follow_n"] == 2 and r["fade_n"] == 2
    assert r["follow_compounded"] == pytest.approx((1.049) * (0.969) - 1)
    assert r["fade_compounded"] == pytest.approx((0.949) * (1.029) - 1)


def test_subject_shows_both_calls(monkeypatch, tmp_path):
    monkeypatch.setattr(sb, "STATE", tmp_path)
    call = {"date": D.isoformat(), "bias": "lean_soxl", "implied_soxx": 0.006, "implied_soxl": 0.018,
            "implied_soxs": -0.018, "calendar_verified": True,
            "weights": {"NVDA": 0.1}, "premarket": {"NVDA": {"move": 0.01}},
            "implied": {"contrib": {"NVDA": 0.006}, "coverage": 1.0, "n_up": 1, "n_down": 0, "dispersion": 0.0},
            "components": {"overnight_asia_eu": None, "nasdaq_futures": None, "news_net": None, "social_skew": None},
            "overnight": {}, "nasdaq_futures": None, "news": [], "filings": None, "truth_social": None,
            "social": {}, "levels": {"soxl_prior_close": 100, "soxl_prior_high": None, "soxl_prior_low": None,
            "soxl_pm_high": None, "soxl_pm_low": None, "soxl_gap": None, "gap_fill": None},
            "catalysts": {"earnings_next_7d": [], "macro_today": None}}
    subj, text, _ = sb.render(call, {}, None)
    assert "gap up" in subj and "follow: SOXL" in subj and "fade: SOXS" in subj


def test_send_telegram_partial_and_first_chunk_failure(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t"); monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    calls = []
    def post(token, chat, part):
        calls.append(part)
        if len(calls) == 2:
            raise OSError("down")
    monkeypatch.setattr(sb, "_telegram_post", post)
    long = "\n".join("y" * 100 for _ in range(100))          # > 4000 chars -> several chunks
    assert sb.send_telegram("S", long, "H") == "telegram_partial"
    def fail_first(token, chat, part):
        raise OSError("down")
    monkeypatch.setattr(sb, "_telegram_post", fail_first)
    with pytest.raises(OSError):
        sb.send_telegram("S", "short", "H")


def test_outage_day_is_labelled_no_data_not_flat_open(monkeypatch, tmp_path):
    monkeypatch.setattr(sb, "STATE", tmp_path)                # no backfill file -> no line
    call = {"date": D.isoformat(), "bias": "no_edge", "implied_soxx": None, "implied_soxl": None,
            "implied_soxs": None, "calendar_verified": True, "weights": {"NVDA": 0.1},
            "premarket": {"NVDA": {"move": None, "why_missing": "no bar"}},
            "implied": {"contrib": {}, "coverage": 0.0},
            "components": {"overnight_asia_eu": None, "nasdaq_futures": None, "news_net": None, "social_skew": None},
            "overnight": {}, "nasdaq_futures": None, "news": [], "filings": None, "truth_social": None,
            "social": {}, "levels": {"soxl_prior_close": None, "soxl_prior_high": None, "soxl_prior_low": None,
            "soxl_pm_high": None, "soxl_pm_low": None, "soxl_gap": None, "gap_fill": None},
            "catalysts": {"earnings_next_7d": [], "macro_today": None}}
    subj, text, htm = sb.render(call, {}, None)
    assert "no data" in subj and "flat open" not in subj and "Backfill" not in text


# --- daily Bollinger bands --------------------------------------------------------

def _bars(closes, opens=None):
    opens = opens or closes
    return [{"date": (date(2026, 1, 1) + timedelta(days=i)).isoformat(), "open": o, "close": c}
            for i, (o, c) in enumerate(zip(opens, closes))]


def test_bollinger_matches_population_std():
    closes = [float(x) for x in range(1, 21)]
    m, u, lo = sb.bollinger(closes)[-1]
    import statistics as st
    assert m == pytest.approx(10.5)
    assert u - m == pytest.approx(2 * st.pstdev(closes))
    assert sb.bollinger(closes[:19])[-1] is None


def test_band_state_zones():
    flat = [100.0] * 19
    assert sb.band_state(_bars(flat + [100.0]))["zone"] == "inside"
    assert sb.band_state(_bars([100.0 + (i % 2) for i in range(19)] + [120.0]))["zone"] == "above_upper"
    assert sb.band_state(_bars([100.0 + (i % 2) for i in range(19)] + [80.0]))["zone"] == "below_lower"


def test_band_trade_enters_next_open_and_exits_on_close_above_middle():
    base = [100.0 + (i % 2) for i in range(20)]
    closes = base + [80.0, 85.0, 99.0, 104.0]          # 80 closes below lower -> signal
    opens = base + [80.0, 82.0, 90.0, 100.0]
    trades, op = sb.band_trades(_bars(closes, opens))
    assert len(trades) == 1 and op is None
    t = trades[0]
    assert t["entry"] == 82.0                          # NEXT session's open, not the signal close
    assert t["exit"] == 99.0                           # FIRST close above that day's middle (98.65)
    assert t["exit_date"] == "2026-01-23" and t["hold_days"] == 2   # the 99.0 bar, not the 104.0 bar
    assert t["ret"] == pytest.approx(99.0 / 82.0 - 1 - 2 * sb.BAND_COST)


def test_band_trade_open_and_pending_signal():
    base = [100.0 + (i % 2) for i in range(20)]
    _, op = sb.band_trades(_bars(base + [80.0, 81.0], base + [80.0, 82.0]))
    assert op["entry"] == 82.0 and op["unrealized"] == pytest.approx(81.0 / 82.0 - 1 - sb.BAND_COST)
    _, pend = sb.band_trades(_bars(base + [80.0]))
    assert pend["pending_entry"] is True and pend["entry"] is None


def test_band_record_since_filters_by_entry_date_not_signal_date():
    # A signal from the 09-22 close is BOUGHT on 09-23, so it is live even though its
    # signal_date predates the cutover. Filtering on signal_date would drop it.
    trades = [{"signal_date": "2026-09-01", "entry_date": "2026-09-02", "ret": 0.05},
              {"signal_date": "2026-09-22", "entry_date": "2026-09-23", "ret": -0.02}]
    assert sb.band_record(trades)["n"] == 2
    live = sb.band_record(trades, date(2026, 9, 23))
    assert live["n"] == 1 and live["win_rate"] == 0.0


def test_destitch_removes_an_unadjusted_reverse_split():
    # Real shape: SOXS 2026-05-22 close 1159.50 -> 2026-05-26 close 62.90, present in the
    # yfinance series with auto_adjust both False and True, matching no date in .splits.
    bars = [{"date": f"2026-05-{d:02d}", "open": o, "close": c} for d, o, c in
            [(20, 1290.0, 1281.0), (21, 1275.0, 1243.5), (22, 1250.0, 1159.5),
             (26, 63.5, 62.9), (27, 62.0, 65.3)]]
    out = sb.destitch(bars)
    rets = [out[i]["close"] / out[i - 1]["close"] - 1 for i in range(1, len(out))]
    assert all(abs(r) < 0.20 for r in rets), rets            # the -94.6% artifact is gone
    assert out[-1]["close"] == 65.3 and out[-1]["open"] == 62.0   # present scale untouched
    assert out[0]["close"] / out[0]["open"] == pytest.approx(1281.0 / 1290.0)  # ratios preserved


def test_destitch_keeps_a_real_leveraged_etf_crash():
    # SOXS did -56.0% on 2025-04-09 (the tariff-pause rally). That is genuine, keep it.
    bars = [{"date": "2025-04-08", "open": 140000.0, "close": 141060.0},
            {"date": "2025-04-09", "open": 130000.0, "close": 62100.0}]
    assert sb.destitch(bars) == bars


def test_band_signal_on_a_fund_where_the_rule_lost_is_not_called_a_buy(monkeypatch, tmp_path):
    monkeypatch.setattr(sb, "STATE", tmp_path)
    st = {"zone": "below_lower", "close": 35.0, "upper": 55.0, "middle": 47.0, "lower": 39.0,
          "pct_b": -0.2, "bandwidth": 0.4}
    pend = {"pending_entry": True, "entry": None}
    call = {"date": D.isoformat(), "bias": "no_edge", "implied_soxx": 0.001, "implied_soxl": 0.003,
            "implied_soxs": -0.003, "calendar_verified": True, "weights": {}, "premarket": {},
            "implied": {"contrib": {}, "coverage": 1.0},
            "components": {"overnight_asia_eu": None, "nasdaq_futures": None, "news_net": None, "social_skew": None},
            "overnight": {}, "nasdaq_futures": None, "news": [], "filings": None, "truth_social": None, "social": {},
            "levels": {"soxl_prior_close": None, "soxl_prior_high": None, "soxl_prior_low": None,
                       "soxl_pm_high": None, "soxl_pm_low": None, "soxl_gap": None, "gap_fill": None},
            "catalysts": {"earnings_next_7d": [], "macro_today": None},
            "bands": {"SOXS": {"state": st, "open_trade": pend, "record_5y": {"n": 20, "win_rate": 0.5, "mean": -0.063},
                               "record_live": {"n": 0}},
                      "SOXL": {"state": st, "open_trade": pend, "record_5y": {"n": 24, "win_rate": 0.71, "mean": 0.075},
                               "record_live": {"n": 0}}}}
    _, text, _ = sb.render(call, {}, None)
    soxs = [l for l in text.splitlines() if l.strip().startswith("SOXS:")][0]
    soxl = [l for l in text.splitlines() if l.strip().startswith("SOXL:")][0]
    assert "LOST on this fund" in soxs and "BUY SIGNAL" not in soxs
    assert "BUY SIGNAL" in soxl


# ------------------------------------------------------------ portfolio watch section

def _pbars(closes, vols=None):
    vols = vols or [1_000_000.0] * len(closes)
    return [{"date": f"d{i}", "open": c, "close": c, "volume": v}
            for i, (c, v) in enumerate(zip(closes, vols))]


def test_sma_and_range_position():
    assert sb.sma([1.0] * 25, 20) == pytest.approx(1.0)
    assert sb.sma([1.0] * 5, 20) is None
    closes = [float(i) for i in range(1, 101)]          # 1..100, last is the high
    assert sb.range_position(closes) == (pytest.approx(1.0), 100)
    assert sb.range_position(list(reversed(closes)))[0] == pytest.approx(0.0)
    assert sb.range_position([5.0] * 100) is None        # flat: no range to sit in
    assert sb.range_position([1.0, 2.0]) is None         # too short
    # the window length comes back so the label can be honest
    assert sb.range_position([float(i) for i in range(1, 401)])[1] == 252


def test_portfolio_row_needs_a_full_band_window():
    r = sb.portfolio_row("X", "theme", "note", _pbars([10.0] * (sb.BAND_N - 1)))
    assert r["close"] is None
    assert sb.portfolio_line(r).startswith("X     n/a")


def test_portfolio_row_flags_a_thin_book():
    thin = sb.portfolio_row("THIN", "watch", "n", _pbars([40.0] * 60, [2_800.0] * 60))
    assert thin["thin"] is True
    assert "THIN" in sb.portfolio_line(thin) and "limit orders only" in sb.portfolio_line(thin)
    # HSYDF's real shape: ~2,839 shares at ~$41 = ~$118k/day, well under the threshold
    assert 2_800 * 40.0 < sb.THIN_DOLLAR_VOLUME
    fat = sb.portfolio_row("FAT", "core", "n", _pbars([40.0] * 60, [1_000_000.0] * 60))
    assert fat["thin"] is False and "THIN" not in sb.portfolio_line(fat)


def test_portfolio_row_is_nan_safe():
    # non-flat closes, with the NaN INSIDE the trailing 20-bar band window, so every
    # derived field is actually exercised. An unguarded version leaves these NaN.
    closes = [100.0 + (i % 7) for i in range(80)]
    bars = _pbars(closes)
    bars[-5]["close"] = float("nan")       # NaN is truthy; must not reach the math
    bars[-3]["volume"] = float("nan")
    r = sb.portfolio_row("X", "core", "n", bars)
    for k in ("close", "chg", "pct_b", "vs_sma20", "range_pos", "dollar_volume"):
        assert r[k] is not None and math.isfinite(r[k]), k
    assert "nan" not in sb.portfolio_line(r)


def test_a_zero_close_cannot_kill_the_brief():
    # one 0.0 close used to raise ZeroDivisionError out of build_call -> no brief at all
    bars = _pbars([10.0] * 60)
    bars[-2]["close"] = 0.0
    r = sb.portfolio_row("X", "core", "n", bars)
    assert r["close"] == pytest.approx(10.0)
    assert sb.portfolio_line(r)


def test_unknown_volume_flags_thin_rather_than_passing_as_liquid():
    bars = _pbars([40.0] * 60)
    for b in bars:
        b["volume"] = None
    r = sb.portfolio_row("UNK", "watch", "n", bars)
    assert r["dollar_volume"] is None and r["thin"] is True
    assert "volume unknown" in sb.portfolio_line(r)


def test_zero_volume_sessions_count_toward_the_median():
    # trades 20 of the last 60 sessions; true median dollar volume is 0
    vols = [0.0] * 40 + [80_000.0] * 20
    r = sb.portfolio_row("GAPPY", "watch", "n", _pbars([41.0] * 60, vols))
    assert r["dollar_volume"] == pytest.approx(0.0)
    assert r["thin"] is True


def test_stale_daily_bar_is_flagged():
    # yfinance drops whole trading days: it is currently missing 2026-09-22 for all 8 names
    r = sb.portfolio_row("X", "core", "n", _pbars([10.0 + i * 0.1 for i in range(60)]))
    r["as_of"] = "2026-09-21"
    assert "STALE" in sb.portfolio_line(r, "2026-09-22")
    assert "STALE" not in sb.portfolio_line(r, "2026-09-21")
    assert "STALE" not in sb.portfolio_line(r, None)


def test_range_label_says_52w_only_when_it_has_52_weeks():
    short = sb.portfolio_row("NEW", "theme", "n", _pbars([10.0 + i * 0.1 for i in range(61)]))
    assert "61d" in sb.portfolio_line(short) and "52w" not in sb.portfolio_line(short)
    long_ = sb.portfolio_row("OLD", "core", "n", _pbars([10.0 + i * 0.05 for i in range(300)]))
    assert "52w" in sb.portfolio_line(long_)


def test_portfolio_row_computes_the_stated_distances():
    closes = [100.0] * 199 + [110.0]
    r = sb.portfolio_row("X", "core", "n", _pbars(closes))
    assert r["vs_sma200"] == pytest.approx(110.0 / ((100.0 * 199 + 110.0) / 200) - 1)
    assert r["vs_sma20"] == pytest.approx(110.0 / ((100.0 * 19 + 110.0) / 20) - 1)
    assert r["chg"] == pytest.approx(0.10)


def test_portfolio_covers_every_configured_ticker_exactly_once():
    ticks = [t for t, _, _ in sb.PORTFOLIO]
    assert len(ticks) == len(set(ticks))
    assert {r for _, r, _ in sb.PORTFOLIO} <= {"core", "theme", "watch"}
    assert "VTI" in ticks and any(r == "core" for _, r, _ in sb.PORTFOLIO)
