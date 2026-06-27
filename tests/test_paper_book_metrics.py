import sys, pathlib, datetime as dt
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
