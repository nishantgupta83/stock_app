"""M2 — winrate-drift monitor: flag rules whose recent winrate falls far below
lifetime (the news-cohort regime break verified 2026-06-09). Monitor only.
"""
from __future__ import annotations

from pulsecheck.price_agent import classify_winrate_drift


def test_sharp_recent_drop_warns():
    # news_article:neutral:h7d shape: lifetime ~52%, recent ~14%.
    status, _ = classify_winrate_drift(0.525, 0.14, 28)
    assert status == "warning"


def test_stable_rule_ok():
    status, _ = classify_winrate_drift(0.67, 0.64, 40)
    assert status == "ok"


def test_thin_recent_sample_not_flagged():
    # Big gap but only 3 recent closes → not enough to trust → ok (no false alarm).
    status, _ = classify_winrate_drift(0.90, 0.20, 3)
    assert status == "ok"


def test_missing_recent_accuracy_ok():
    status, _ = classify_winrate_drift(0.55, None, 50)
    assert status == "ok"


def test_recent_better_than_lifetime_ok():
    # Improvement is never flagged (we only warn on degradation).
    status, _ = classify_winrate_drift(0.40, 0.80, 30)
    assert status == "ok"
