"""C3 — make swallowed failures loud.

Three sites recorded run status='ok' (or never finished the job row) while
silently losing work. Each now uses the thesis partial+meta pattern: a pure
status helper flips to 'partial' (a CHECK-allowed status pulsecheck reads) when
written < expected, so a lost write/outcome surfaces in stock_job_runs instead
of hiding behind a green run.
"""
from __future__ import annotations

from trade_setup_agent import write_run_status as l3_status
from risk_agent import write_run_status as l4_status
from price_agent import reconcile_run_status


def test_l3_partial_when_rows_lost() -> None:
    assert l3_status(10, 10) == ("ok", None)
    status, err = l3_status(10, 7)
    assert status == "partial"
    assert "3" in err and "10" in err


def test_l4_partial_when_decisions_lost() -> None:
    assert l4_status(5, 5) == ("ok", None)
    status, err = l4_status(5, 2)
    assert status == "partial"
    assert "3" in err and "5" in err


def test_price_reconcile_exception_is_partial_not_ok() -> None:
    """A crashed reconcile must NOT record 'ok' (the learning loop looked
    healthy while calibration silently stopped updating)."""
    assert reconcile_run_status(reconcile_failed=False, n_close_failed=0) == ("ok", None)
    status, err = reconcile_run_status(reconcile_failed=True, n_close_failed=0)
    assert status == "partial" and err and "reconcile" in err.lower()


def test_price_close_patch_failure_is_partial() -> None:
    status, err = reconcile_run_status(reconcile_failed=False, n_close_failed=4)
    assert status == "partial" and "4" in err


def test_price_signal_write_failure_is_partial() -> None:
    """Outcome computed but its persistence (audit/close) failed → partial."""
    assert reconcile_run_status(False, 0, 0) == ("ok", None)
    status, err = reconcile_run_status(False, 0, n_signal_write_failed=2)
    assert status == "partial" and "2" in err
