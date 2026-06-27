"""TDD: Task 5 — dashboard shows forward tier + book-vs-QQQ excess."""
import sys, pathlib, json
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import paper_book_dashboard as dash


def test_render_metrics_block_shows_tier_and_excess():
    metrics = {"forward": {"cumulative_excess": -150.0, "book_equity_end": 4850.0,
                           "qqq_buy_hold_end": 5000.0, "n_independent_cohorts": 12, "weeks": 4.0},
               "tier": {"status": "inconclusive", "reason": "insufficient_sample"}}
    html = dash.render_metrics_block(metrics)
    assert "inconclusive" in html
    assert "-150" in html or "−150" in html


def test_render_metrics_block_unknown_status_uses_fallback_color():
    """Unknown status should not raise and should still render the block."""
    metrics = {"forward": {"cumulative_excess": 0.0, "book_equity_end": 5000.0,
                           "qqq_buy_hold_end": 5000.0, "n_independent_cohorts": 0, "weeks": 0.0},
               "tier": {"status": "unknown_future_tier", "reason": "none"}}
    h = dash.render_metrics_block(metrics)
    assert "unknown_future_tier" in h
    assert "tier" in h.lower()


def test_render_metrics_block_missing_keys_does_not_raise():
    """Partial metrics dict should render without KeyError."""
    metrics = {}
    h = dash.render_metrics_block(metrics)
    assert isinstance(h, str) and len(h) > 0
