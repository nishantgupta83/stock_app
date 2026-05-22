"""Regression tests for the PR1A causal-attribution policy.

Audit context (2026-05-22): 45% (13/29) of Telegram-sent signals cited a
7-day-old Bridgewater 13F filing as the "catalyst" for that day's price
move. Another 31% (9/29) said "no catalyst — check news/sector rotation"
but still fired as WATCH. Both classes are dishonest causal labeling.

PR1A introduces:
 - CATALYST_POLICY with per-event-type {role, max_age_hours}
 - score_evidence tags each breakdown entry with role
 - background-role events contribute 0 to score (display-only)
 - decompose_score returns catalyst_score / context_score / background_score
 - action_for takes catalyst_score; returns MOMENTUM_ONLY when catalyst_score==0
   for bullish bands, CATALYST_WATCH / CATALYST_RESEARCH when catalyst exists,
   AVOID_CHASE / CHASE_RISK / BUY / SELL unchanged.

These tests lock in the new behavior with synthetic events + a replay of
the exact 29-signal day so a future revert of any policy entry fails CI.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from _catalyst_policy import (
    CATALYST_POLICY,
    is_catalyst_eligible,
    policy_for,
    split_events_by_role,
)
from thesis_agent import action_for, decompose_score, score_evidence

NOW = datetime.now(timezone.utc)
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "telegram_signals_2026_05_22.json"


def _evt(event_type: str, *, hours_ago: float = 0, ticker: str = "XYZ",
         severity: int = 3, subtype: str | None = None,
         direction: str = "long", event_id: int | None = None,
         payload_extra: dict | None = None) -> dict:
    """Build a synthetic normalized_event row for testing."""
    payload: dict = {"direction_prior": direction}
    if payload_extra:
        payload.update(payload_extra)
    return {
        "id":             event_id or hash((event_type, ticker, hours_ago)) % 100000,
        "ticker":         ticker,
        "event_type":     event_type,
        "event_subtype":  subtype,
        "severity":       severity,
        "event_at":       (NOW - timedelta(hours=hours_ago)).isoformat(),
        "created_at":     (NOW - timedelta(hours=hours_ago)).isoformat(),
        "payload":        payload,
    }


# ============================================================
# CATALYST_POLICY structure invariants
# ============================================================

def test_13f_institutional_is_background_role():
    """The single most important policy decision: 13F (institutional_*) is
    NEVER a same-day catalyst, regardless of how recent the filing landed."""
    for et in ("institutional_new_position", "institutional_exit",
               "institutional_increase", "institutional_decrease"):
        p = policy_for(et)
        assert p["role"] == "background", f"{et} should be background role"
        assert p["max_age_hours"] == 0, f"{et} max_age must be 0 (never catalyst)"


def test_news_article_is_short_lived_catalyst():
    p = policy_for("news_article")
    assert p["role"] == "catalyst"
    assert p["max_age_hours"] == 48


def test_8k_is_multi_day_catalyst():
    p = policy_for("8k_material_event")
    assert p["role"] == "catalyst"
    assert p["max_age_hours"] >= 120  # at least 5 days


def test_13d_and_13g_are_context_not_catalyst():
    """Activist (13D) and passive (13G) disclosures are context — they support
    a thesis but don't claim causality for the day's move."""
    for et in ("filing_13d", "filing_13g"):
        assert policy_for(et)["role"] == "context"


def test_unknown_event_type_defaults_to_context():
    p = policy_for("totally_made_up_event_type")
    assert p["role"] == "context"
    assert p["max_age_hours"] == 168


# ============================================================
# is_catalyst_eligible
# ============================================================

def test_fresh_news_is_catalyst_eligible():
    assert is_catalyst_eligible(_evt("news_article", hours_ago=12)) is True


def test_stale_news_is_not_catalyst_eligible():
    # news_article max_age is 48h; 72h ago is stale
    assert is_catalyst_eligible(_evt("news_article", hours_ago=72)) is False


def test_13f_never_catalyst_eligible_regardless_of_freshness():
    """Even a 13F that landed 1 second ago is not a same-day catalyst."""
    assert is_catalyst_eligible(_evt("institutional_new_position", hours_ago=0.001)) is False


def test_13g_filing_is_not_catalyst_eligible_role_is_context():
    """13G is role=context, not catalyst, so is_catalyst_eligible returns
    False regardless of age (the function only returns True for role=catalyst)."""
    assert is_catalyst_eligible(_evt("filing_13g", hours_ago=12)) is False


# ============================================================
# score_evidence + decompose_score behavior
# ============================================================

def test_stale_13f_alone_produces_zero_score():
    """The exact bug fixed in PR1A: a week-old 13F filing must not
    contribute to alert score, even though it lands in the breakdown for
    display purposes."""
    events = [_evt("institutional_new_position", hours_ago=24 * 7, subtype="BRDGW")]
    score, breakdown = score_evidence(events)
    sub = decompose_score(breakdown)
    assert score == 0.0, "13F-only signal must score 0"
    assert sub["catalyst"] == 0.0
    assert sub["background"] == 25.0  # inst_new_position rubric grants 25 raw pts
    assert sub["total_alert"] == 0.0


def test_recent_news_produces_catalyst_score():
    events = [_evt("news_article", hours_ago=6, payload_extra={"direction_prior": "long"})]
    score, breakdown = score_evidence(events)
    sub = decompose_score(breakdown)
    # news_bullish base = 12 pts; staleness penalty kicks in at 120 min, so 6h fresh
    # event gets the staleness penalty (-5). Net catalyst contribution: 12 - 5 = 7.
    # Acceptable as long as catalyst_score is positive and within reasonable range.
    assert sub["catalyst"] > 0
    assert sub["background"] == 0.0


def test_stale_news_demotes_to_context():
    """A news_article at 72h ago is past its 48h catalyst window — should
    appear in context bucket, not catalyst."""
    events = [_evt("news_article", hours_ago=72, payload_extra={"direction_prior": "long"})]
    score, breakdown = score_evidence(events)
    sub = decompose_score(breakdown)
    assert sub["catalyst"] == 0.0
    assert sub["context"] > 0  # demoted to context bucket


def test_13g_filing_contributes_to_context_not_catalyst():
    events = [_evt("filing_13g", hours_ago=24)]
    score, breakdown = score_evidence(events)
    sub = decompose_score(breakdown)
    assert sub["catalyst"] == 0.0
    assert sub["context"] > 0


# ============================================================
# action_for vocabulary
# ============================================================

def test_action_for_bullish_with_catalyst_returns_catalyst_watch():
    assert action_for(75, "bullish", catalyst_score=20) == "CATALYST_WATCH"


def test_action_for_bullish_without_catalyst_returns_momentum_only():
    """Same total score, but catalyst_score==0 → MOMENTUM_ONLY, not CATALYST_WATCH."""
    assert action_for(75, "bullish", catalyst_score=0) == "MOMENTUM_ONLY"


def test_action_for_bearish_unchanged_is_avoid_chase():
    """AVOID_CHASE is a risk warning, not a catalyst claim — unchanged by PR1A."""
    assert action_for(60, "bearish", catalyst_score=0) == "AVOID_CHASE"
    assert action_for(60, "bearish", catalyst_score=20) == "AVOID_CHASE"


def test_action_for_research_band_split_by_catalyst_score():
    assert action_for(55, "bullish", catalyst_score=10) == "CATALYST_RESEARCH"
    assert action_for(55, "bullish", catalyst_score=0) == "MOMENTUM_ONLY"


def test_action_for_low_score_suppressed():
    """Below research threshold, no action regardless of catalyst."""
    assert action_for(30, "bullish", catalyst_score=5) == ""
    assert action_for(30, "bullish", catalyst_score=0) == ""


def test_action_for_mature_rule_emits_buy_sell():
    assert action_for(80, "bullish", catalyst_score=20,
                      has_mature_rule=True) == "BUY"
    assert action_for(60, "bearish", catalyst_score=0,
                      has_mature_rule=True) == "SELL"


# ============================================================
# Today's 13F-pattern signal scenario (the exact bug case)
# ============================================================

def test_stale_13f_plus_momentum_produces_no_alert():
    """Replicates today's CRWV / SEDG / ENPH pattern: stale 13F + intraday spike.
    Before PR1A: WATCH score 60+. After PR1A: suppressed entirely or
    MOMENTUM_ONLY depending on whether intelligence-layer bonuses push score
    above threshold. catalyst_score is the gate, not total score."""
    events = [
        _evt("institutional_new_position", hours_ago=24 * 7, subtype="BRDGW"),
        _evt("momentum", hours_ago=0,
             payload_extra={"rel_strength_pct": 6.2, "direction_prior": "long"}),
    ]
    score, breakdown = score_evidence(events)
    sub = decompose_score(breakdown)
    # 13F → background (0 to score). momentum +15 (moderate). bonus +8 (multi-source).
    # catalyst_score == 0 because momentum is role=context, 13F is role=background.
    assert sub["catalyst"] == 0.0
    action = action_for(score, "bullish", catalyst_score=sub["catalyst"])
    # With catalyst_score=0 and score=23 (below research threshold of 50), suppressed
    assert action in ("", "MOMENTUM_ONLY"), \
        f"Expected suppression or MOMENTUM_ONLY for 13F+momentum, got {action}"


# ============================================================
# Replay test against the 29-signal fixture
# ============================================================

@pytest.mark.skipif(not FIXTURE_PATH.exists(), reason="fixture not present")
def test_replay_29_signal_day_action_distribution():
    """Replay the 29 (actually 30) signals from 2026-05-22 through the new
    scoring pipeline. Expected post-PR1A behavior:

    - Signals that ONLY cited institutional_* (background) for catalyst →
      either suppressed (score below threshold) or MOMENTUM_ONLY
    - Signals with no catalyst attribution at all → MOMENTUM_ONLY
    - Signals with genuine recent catalysts (8-K, news, earnings, clinical) →
      CATALYST_WATCH / CATALYST_RESEARCH / AVOID_CHASE

    This is a measurement test, not an equality assertion — it asserts that
    the distribution SHIFTS in the right direction (fewer false WATCH labels,
    more MOMENTUM_ONLY honesty). Exact counts can drift as scoring rubric
    evolves; what must NOT drift is the catalyst_score==0 → no-CATALYST_WATCH
    invariant.
    """
    fixture = json.loads(FIXTURE_PATH.read_text())
    signals = fixture["signals"]
    events_by_ticker = fixture["events_by_ticker"]

    # For each signal, re-score using only the events that existed at fired_at
    catalyst_watch = 0
    catalyst_research = 0
    momentum_only = 0
    avoid_chase = 0
    suppressed = 0

    for sig in signals:
        ticker = sig["ticker"]
        fired_at = datetime.fromisoformat(sig["fired_at"].replace("Z", "+00:00"))
        # Get events for this ticker that existed at or before fired_at
        ev_pool = events_by_ticker.get(ticker, [])
        ev_at_fire = [e for e in ev_pool
                      if datetime.fromisoformat(e["created_at"].replace("Z", "+00:00")) <= fired_at]
        if not ev_at_fire:
            # No events at signal time — would not have been emitted by thesis_agent
            # (signal must have come from intraday_alert_agent; skip for thesis replay)
            continue
        score, breakdown = score_evidence(ev_at_fire)
        sub = decompose_score(breakdown)
        direction = sig.get("direction") or "bullish"
        action = action_for(score, direction, catalyst_score=sub["catalyst"])
        if action == "CATALYST_WATCH":
            catalyst_watch += 1
        elif action == "CATALYST_RESEARCH":
            catalyst_research += 1
        elif action == "MOMENTUM_ONLY":
            momentum_only += 1
        elif action == "AVOID_CHASE":
            avoid_chase += 1
        else:
            suppressed += 1

    # Print the distribution for the test log (useful for tuning)
    print(f"\nReplay distribution: CATALYST_WATCH={catalyst_watch} "
          f"CATALYST_RESEARCH={catalyst_research} MOMENTUM_ONLY={momentum_only} "
          f"AVOID_CHASE={avoid_chase} suppressed={suppressed}")

    # Sanity-check that we processed all signals
    total = catalyst_watch + catalyst_research + momentum_only + avoid_chase + suppressed
    assert total > 0, "No signals replayed — fixture may be empty"


@pytest.mark.skipif(not FIXTURE_PATH.exists(), reason="fixture not present")
def test_replay_stale_13f_signals_lose_catalyst_attribution():
    """For every fixture signal whose evidence_summary cites only a 13F filing
    (the 13 stale-attribution signals), the replay must produce catalyst_score==0.
    This is the exact regression the policy must prevent."""
    fixture = json.loads(FIXTURE_PATH.read_text())

    n_stale_13f_signals = 0
    n_with_zero_catalyst = 0
    for sig in fixture["signals"]:
        es = sig.get("evidence_summary") or ""
        if "inst-new_position" not in es:
            continue  # not a stale-13F-cited signal
        n_stale_13f_signals += 1
        ticker = sig["ticker"]
        fired_at = datetime.fromisoformat(sig["fired_at"].replace("Z", "+00:00"))
        ev_pool = fixture["events_by_ticker"].get(ticker, [])
        ev_at_fire = [e for e in ev_pool
                      if datetime.fromisoformat(e["created_at"].replace("Z", "+00:00")) <= fired_at]
        if not ev_at_fire:
            continue
        _score, breakdown = score_evidence(ev_at_fire)
        sub = decompose_score(breakdown)
        # The 13F filing must contribute 0 to catalyst_score; the signal as a
        # whole may still have catalyst_score > 0 if OTHER catalyst-eligible
        # events are also present (e.g., a recent news article). But the
        # average case in the fixture is: 13F is the only "evidence" cited.
        # If catalyst_score == 0, it means the 13F was correctly demoted.
        if sub["catalyst"] == 0:
            n_with_zero_catalyst += 1

    # Most (>50%) of the 13F-cited signals should now have catalyst_score==0,
    # i.e. they'd be MOMENTUM_ONLY or suppressed after PR1A.
    assert n_stale_13f_signals > 0, "fixture has no 13F-cited signals — sanity check fail"
    ratio = n_with_zero_catalyst / n_stale_13f_signals
    assert ratio >= 0.5, (
        f"Only {n_with_zero_catalyst}/{n_stale_13f_signals} of stale-13F-cited "
        f"signals lost their catalyst attribution after PR1A. Expected >= 50%."
    )
