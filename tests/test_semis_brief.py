"""semis_brief — pure-function tests (no network)."""
from __future__ import annotations

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
    assert sb.in_brief_window(datetime(2026, 9, 18, 6, 15, tzinfo=PT))       # PDT
    assert sb.in_brief_window(datetime(2026, 12, 3, 6, 15, tzinfo=PT))       # PST
    assert not sb.in_brief_window(datetime(2026, 9, 18, 6, 30, tzinfo=PT))
    assert not sb.in_brief_window(datetime(2026, 9, 18, 6, 5, tzinfo=PT))


def test_utc_backup_crons_land_in_window_exactly_once_per_dst_state():
    # workflow backups: 13:15 and 14:15 UTC. Summer -> 13:15 hits; winter -> 14:15 hits.
    for d, hit_hour in ((date(2026, 9, 18), 13), (date(2026, 12, 3), 14)):
        hits = [h for h in (13, 14)
                if sb.in_brief_window(datetime(d.year, d.month, d.day, h, 15, tzinfo=timezone.utc).astimezone(PT))]
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
