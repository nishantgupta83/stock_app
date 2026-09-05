import sys, pathlib, datetime as dt, math
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "agents"))
import _paper_book_metrics as m

D = dt.date.fromisoformat


def test_independent_cohorts_counts_entry_dates():
    pos = [{"opened_at": "2026-06-21T00:00:00+00:00"},
           {"opened_at": "2026-06-21T00:00:00+00:00"},   # same day -> still 1 cohort
           {"opened_at": "2026-06-23T00:00:00+00:00"}]
    assert m.independent_cohorts(pos) == 2


def test_book_equity_and_excess():
    days = [D("2026-06-21"), D("2026-06-22")]
    pos = [{"opened_at": "2026-06-20T00:00:00+00:00", "closed_at": "2026-06-22T00:00:00+00:00",
            "status": "closed", "notional": 1000.0, "realized_pnl": 100.0}]
    book = m.book_equity_curve(pos, days, capital=5000.0, rf_annual=0.0)
    assert book[D("2026-06-21")] == 5000.0     # still open, no pnl booked
    assert book[D("2026-06-22")] == 5100.0     # closed -> +100
    qqq_daily = {D("2026-06-21"): 100.0, D("2026-06-22"): 105.0}
    qqq = m.qqq_buy_hold_curve(qqq_daily, days, capital=5000.0, epoch=D("2026-06-21"))
    assert qqq[D("2026-06-22")] == 5250.0       # +5%
    assert m.cumulative_excess(book, qqq) == round(5100.0 - 5250.0, 2)  # book lost to QQQ


def test_max_drawdown():
    curve = {D("2026-06-21"): 100.0, D("2026-06-22"): 120.0, D("2026-06-23"): 90.0}
    assert m.max_drawdown(curve) == 0.25       # (120-90)/120


def test_profit_factor():
    closed = [{"realized_pnl": 200}, {"realized_pnl": -100}, {"realized_pnl": 0}]
    assert m.profit_factor(closed) == 2.0
    assert m.profit_factor([{"realized_pnl": 50}]) == float("inf")
    assert m.profit_factor([]) == 0.0


def test_top_cohort_excess_share():
    pos = [
        {"status": "closed", "opened_at": "2026-06-21T00:00:00+00:00", "realized_pnl": 300},
        {"status": "closed", "opened_at": "2026-06-22T00:00:00+00:00", "realized_pnl": 100},
    ]
    assert m.top_cohort_excess_share(pos) == round(300 / 400, 4)


def test_top_cohort_excess_share_mixed_sign():
    # signed max is the WINNER (+300); abs-max would wrongly pick the loss (-500)
    pos = [
        {"status": "closed", "opened_at": "2026-06-21T00:00:00+00:00", "realized_pnl": -500},
        {"status": "closed", "opened_at": "2026-06-22T00:00:00+00:00", "realized_pnl": 300},
    ]
    assert m.top_cohort_excess_share(pos) == round(300 / -200, 4)  # 300/(-500+300)


def test_classify_tier_withholds_on_sync_failure():
    out = m.classify_tier({"n_independent_cohorts": 99, "weeks": 99, "cumulative_excess": 999,
                           "max_drawdown": 0.0, "top_cohort_excess_share": 0.1}, sync_ok=False)
    assert out["status"] == "inconclusive" and out["reason"] == "sync_failed"


def test_classify_tier_insufficient_then_fail_then_alive():
    base = {"max_drawdown": 0.0, "top_cohort_excess_share": 0.1, "profit_factor": 2.0,
            "subperiods_positive": 2}
    thin = dict(base, n_independent_cohorts=5, weeks=2, cumulative_excess=10.0)
    assert m.classify_tier(thin)["status"] == "inconclusive"
    bad = dict(base, n_independent_cohorts=40, weeks=10, cumulative_excess=-50.0)
    assert m.classify_tier(bad)["status"] == "fail"
    ok = dict(base, n_independent_cohorts=40, weeks=10, cumulative_excess=25.0)
    assert m.classify_tier(ok)["status"] in ("alive", "edge")


def test_compute_metrics_splits_forward_and_replay():
    pos = [
        {"opened_at": "2026-06-10T00:00:00+00:00", "closed_at": "2026-06-12T00:00:00+00:00",
         "status": "closed", "notional": 1000.0, "realized_pnl": 50.0},   # replay (pre-epoch)
        {"opened_at": "2026-06-21T00:00:00+00:00", "closed_at": "2026-06-23T00:00:00+00:00",
         "status": "closed", "notional": 1000.0, "realized_pnl": -20.0},  # forward
    ]
    qqq = {D("2026-06-10"): 100.0, D("2026-06-12"): 101.0, D("2026-06-21"): 102.0,
           D("2026-06-23"): 103.0}
    out = m.compute_metrics(pos, qqq, forward_epoch="2026-06-19", capital=5000.0, sync_ok=True)
    assert out["replay"]["n_raw_trades"] == 1
    assert out["forward"]["n_raw_trades"] == 1
    assert out["tier"]["status"] == "inconclusive"   # only 1 forward cohort


def test_compute_metrics_withholds_when_benchmark_unavailable():
    pos = [{"opened_at": "2026-06-21T00:00:00+00:00", "closed_at": "2026-06-23T00:00:00+00:00",
            "status": "closed", "notional": 1000.0, "realized_pnl": 50.0}]
    out = m.compute_metrics(pos, {}, forward_epoch="2026-06-19", capital=5000.0, sync_ok=True)
    assert out["tier"]["status"] == "inconclusive"
    assert out["tier"]["reason"] == "benchmark_unavailable"


# --- non-finite benchmark prices (yfinance gaps) must never reach the metrics ---
# `bool(float("nan")) is True`, so a falsy guard does NOT catch a NaN price: it
# flows through `capital * nan / base` into qqq_buy_hold_end and cumulative_excess
# and out to the dashboard as "$nan" (observed live 2026-09-04).
NAN = float("nan")


def test_qqq_curve_skips_nan_priced_days():
    days = [D("2026-06-21"), D("2026-06-22")]
    qqq_daily = {D("2026-06-21"): 100.0, D("2026-06-22"): NAN}
    curve = m.qqq_buy_hold_curve(qqq_daily, days, capital=5000.0, epoch=D("2026-06-21"))
    assert D("2026-06-22") not in curve          # missing bar == missing day
    assert curve[D("2026-06-21")] == 5000.0
    assert all(math.isfinite(v) for v in curve.values())


def test_qqq_curve_nan_epoch_falls_back_to_next_finite_base():
    days = [D("2026-06-21"), D("2026-06-22")]
    qqq_daily = {D("2026-06-21"): NAN, D("2026-06-22"): 105.0}
    curve = m.qqq_buy_hold_curve(qqq_daily, days, capital=5000.0, epoch=D("2026-06-21"))
    assert all(math.isfinite(v) for v in curve.values())
    assert curve[D("2026-06-22")] == 5000.0      # rebased on the first finite bar


def test_cumulative_excess_is_never_nan():
    book = {D("2026-06-21"): 5000.0, D("2026-06-22"): 5151.77}
    qqq = m.qqq_buy_hold_curve({D("2026-06-21"): 100.0, D("2026-06-22"): NAN},
                               [D("2026-06-21"), D("2026-06-22")],
                               capital=5000.0, epoch=D("2026-06-21"))
    assert math.isfinite(m.cumulative_excess(book, qqq))


def test_compute_metrics_withholds_when_every_benchmark_price_is_nan():
    pos = [{"opened_at": "2026-06-21T00:00:00+00:00", "closed_at": "2026-06-23T00:00:00+00:00",
            "status": "closed", "notional": 1000.0, "realized_pnl": 50.0}]
    qqq = {D("2026-06-21"): NAN, D("2026-06-23"): NAN}
    out = m.compute_metrics(qqq_daily=qqq, positions=pos, forward_epoch="2026-06-19",
                            capital=5000.0, sync_ok=True)
    # an all-NaN benchmark is an ABSENT benchmark, not a usable one
    assert out["tier"]["reason"] == "benchmark_unavailable"
    # Absent is reported as None (honest), never as NaN (a number-shaped hole).
    qend = out["forward"]["qqq_buy_hold_end"]
    assert qend is None or math.isfinite(qend)
    assert math.isfinite(out["forward"]["cumulative_excess"])


def test_reported_endpoints_are_coherent_with_excess_when_benchmark_has_a_gap():
    # Dropping a NaN benchmark bar can leave the curves ending on different days.
    # The three reported numbers must still satisfy book - qqq == excess, or the
    # dashboard prints a comparison that does not subtract.
    pos = [{"opened_at": "2026-08-04T00:00:00+00:00", "closed_at": "2026-08-05T00:00:00+00:00",
            "status": "closed", "notional": 1000.0, "realized_pnl": 151.77}]
    qqq = {D("2026-08-03"): 500.0, D("2026-08-04"): 505.0, D("2026-08-05"): NAN}
    f = m.compute_metrics(positions=pos, qqq_daily=qqq, forward_epoch="2026-08-03",
                          capital=5000.0, sync_ok=True)["forward"]
    assert math.isclose(f["book_equity_end"] - f["qqq_buy_hold_end"],
                        f["cumulative_excess"], abs_tol=0.01)


def _nan_forward_book():
    """Finite benchmark bars BEFORE the epoch, all-NaN in the FORWARD window —
    the live yfinance failure shape. 40 cohorts / 11.1 weeks clears the sample
    gate, so a verdict WOULD be issued."""
    qqq = {D("2026-06-20") + dt.timedelta(days=i): 500.0 + i for i in range(2)}
    qqq.update({D("2026-07-01") + dt.timedelta(days=i): NAN for i in range(160)})
    pos = []
    for i in range(40):
        o = D("2026-07-01") + dt.timedelta(days=i * 2)
        pos.append({"opened_at": o.isoformat() + "T00:00:00+00:00",
                    "closed_at": (o + dt.timedelta(days=5)).isoformat() + "T00:00:00+00:00",
                    "status": "closed", "notional": 1000.0, "realized_pnl": 3.5})
    return pos, qqq


def test_no_usable_forward_benchmark_is_never_graded_alive():
    # Regression: the first fix checked `any(_finite(...))` across the WHOLE
    # qqq_daily dict (replay + forward). Finite pre-epoch bars satisfied it, the
    # forward curve came back empty, cumulative_excess defaulted to 0.0 — which
    # is finite, so the non-finite guard passed and `0.0 < 0` is False. Result:
    # 'alive' on a book with zero usable forward benchmark bars.
    pos, qqq = _nan_forward_book()
    out = m.compute_metrics(positions=pos, qqq_daily=qqq, forward_epoch="2026-07-01",
                            capital=5000.0, sync_ok=True)
    assert out["forward"]["benchmark_days"] == 0
    assert out["tier"]["status"] != "alive"
    assert out["tier"]["reason"] == "benchmark_unavailable"


def test_a_single_usable_benchmark_bar_is_still_not_a_benchmark():
    # Review C1: with exactly ONE finite forward bar (the epoch day) both curves
    # pin to it, excess is 0.0 by construction, `0.0 < 0` is False, and the
    # zero-bar guard did not fire -> 'alive'. One bar cannot express a return.
    pos, qqq = _nan_forward_book()
    qqq[D("2026-07-01")] = 500.0            # the epoch bar is finite; the rest NaN
    out = m.compute_metrics(positions=pos, qqq_daily=qqq, forward_epoch="2026-07-01",
                            capital=5000.0, sync_ok=True)
    assert out["forward"]["benchmark_days"] == 1
    assert out["tier"]["status"] != "alive"
    assert out["tier"]["reason"] == "benchmark_unavailable"


def test_endpoints_stay_coherent_when_there_is_no_benchmark_overlap_at_all():
    # The partial-gap case was covered; the TOTAL-gap case returned
    # (book_last, capital) while excess was 0.0 — so book - qqq != excess, the
    # exact incoherence the sibling test exists to prevent.
    pos, qqq = _nan_forward_book()
    f = m.compute_metrics(positions=pos, qqq_daily=qqq, forward_epoch="2026-07-01",
                          capital=5000.0, sync_ok=True)["forward"]
    # With no benchmark at all we report no benchmark, rather than a number that
    # does not subtract to the stated excess.
    assert f["qqq_buy_hold_end"] is None
    assert f["cumulative_excess"] == 0.0


def test_classify_tier_never_reports_alive_on_non_finite_excess():
    # `NaN < 0` is False, so a NaN excess slips past the `fail` check at :148 and
    # would be reported as a passing tier once the sample gates are met.
    fwd = {"n_independent_cohorts": 40, "weeks": 10.0, "cumulative_excess": NAN,
           "max_drawdown": 0.01, "top_cohort_excess_share": 0.3, "profit_factor": 2.0,
           "subperiods_positive": 2}
    tier = m.classify_tier(fwd)
    assert tier["status"] == "inconclusive"
    assert tier["reason"] == "benchmark_unavailable"
