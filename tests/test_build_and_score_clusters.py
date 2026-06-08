"""Characterization tests for the extracted Layer-2 scoring core.

PR-B0 extracts thesis_agent.main()'s inline cluster-scoring loop into
score_cluster() (the per-cluster scoring unit) + build_and_score_clusters()
(the live event_at-bucket grouping wrapper). main() and the historical
cluster-replay both call score_cluster(), so the score that decides
'>= recall floor' + the rule_keys cannot drift between them.

These lock in: (1) grouping by (ticker, CLUSTER_WINDOW_MIN bucket); (2) the
scored-dict contract; (3) the injectable clock — an 8-K judged as-of a much
later run_at loses catalyst eligibility (the lookahead Codex flagged); (4)
news_fetch=None disables PR1B promotion (the replay's no-lookahead mode).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from thesis_agent import score_cluster, build_and_score_clusters, CLUSTER_WINDOW_MIN

NOW = datetime(2026, 6, 1, 15, 0, tzinfo=timezone.utc)


def _evt(event_type="8k_material_event", *, event_id=1, ticker="ETN",
         hours_ago=1.0, subtype="", severity=3):
    at = (NOW - timedelta(hours=hours_ago)).isoformat()
    return {
        "id": event_id,
        "event_type": event_type,
        "event_subtype": subtype,
        "ticker": ticker,
        "event_at": at,
        "created_at": at,
        "severity": severity,
        "source_table": "test",
        "parser_confidence": 1.0,
        "payload": {"direction_prior": "long"},
    }


class TestScoreCluster:
    def test_returns_scored_contract(self):
        evs = [_evt(event_id=1), _evt(event_id=2)]
        s = score_cluster(evs, rule_calibration={}, now=NOW)
        for key in ("ticker", "score", "catalyst_score", "action",
                    "direction", "cluster_ok", "breakdown", "events"):
            assert key in s, f"missing key {key}"
        assert s["ticker"] == "ETN"
        assert s["score"] >= 0

    def test_recent_8k_is_catalyst(self):
        # An 8-K an hour before run_at is within catalyst max-age → catalyst>0.
        s = score_cluster([_evt(hours_ago=1.0)], rule_calibration={}, now=NOW)
        assert s["catalyst_score"] > 0

    def test_clock_injection_ages_out_catalyst(self):
        # Same 8-K, but scored as-of a run 30 days later → stale → catalyst==0.
        # This is the lookahead guard: real `now` on an old event understates it.
        s = score_cluster([_evt(hours_ago=1.0)], rule_calibration={},
                          now=NOW + timedelta(days=30))
        assert s["catalyst_score"] == 0

    def test_news_fetch_none_disables_promotion(self):
        # A lone generic news event has no catalyst; with news_fetch=None the
        # PR1B raw-news promotion must NOT fire (replay no-lookahead mode).
        s = score_cluster([_evt(event_type="news_article", subtype="")],
                          rule_calibration={}, now=NOW, news_fetch=None)
        assert s.get("news_causal_promoted") is False


class TestBuildAndScoreClusters:
    def test_same_bucket_same_ticker_is_one_cluster(self):
        # Two events within the same CLUSTER_WINDOW_MIN bucket, same ticker.
        evs = [_evt(event_id=1, hours_ago=1.0),
               _evt(event_id=2, hours_ago=1.0)]
        out = build_and_score_clusters(evs, rule_calibration={}, now_fn=lambda _e: NOW)
        assert len(out) == 1
        assert len(out[0]["events"]) == 2

    def test_different_tickers_are_separate_clusters(self):
        evs = [_evt(event_id=1, ticker="ETN", hours_ago=1.0),
               _evt(event_id=2, ticker="NVDA", hours_ago=1.0)]
        out = build_and_score_clusters(evs, rule_calibration={}, now_fn=lambda _e: NOW)
        assert len(out) == 2
