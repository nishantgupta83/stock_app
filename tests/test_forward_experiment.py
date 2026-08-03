"""Pure-function tests for the forward provisional-long edge harness.

No network, no yfinance, no Supabase. Mocks candidates + bars only. Covers the
correctness properties the instrument exists to guarantee:
  - config_hash determinism + cost-sensitivity + per-experiment separation
  - admission rule (bullish + horizon-tag + tradeable + forward-only)
  - anti-lookahead entry (strictly after created_at)
  - forward/replay split by forward_epoch
  - cohort counting by entry-date
  - dedup by (ticker, entry_session, horizon)
  - store immutability of a frozen closed outcome
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

# Dummy creds BEFORE importing the driver (its deferred agent imports read them).
os.environ.setdefault("SUPABASE_URL", "https://test.invalid")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-key-not-real")

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "scripts", ROOT / "agents"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import forward_experiment as fx          # noqa: E402
import _experiment_store as store        # noqa: E402


# ---------------------------------------------------------------------------
# config_hash
# ---------------------------------------------------------------------------

def test_config_hash_deterministic():
    assert fx.config_hash(1, "h1d") == fx.config_hash(1, "h1d")
    assert fx.config_hash(7, "h7d") == fx.config_hash(7, "h7d")


def test_config_hash_changes_with_cost():
    base = fx.config_hash(1, "h1d")
    assert fx.config_hash(1, "h1d", slippage_per_side=0.002) != base


def test_config_hash_changes_with_stop_or_target():
    base = fx.config_hash(1, "h1d")
    assert fx.config_hash(1, "h1d", stop_pct=0.05) != base
    assert fx.config_hash(1, "h1d", target_pct=0.10) != base


def test_config_hash_distinct_per_experiment():
    assert fx.config_hash(1, "h1d") != fx.config_hash(7, "h7d")


def test_config_hash_excludes_timestamp():
    # Two calls straddling wall-clock time must be identical (no now() in the hash).
    a = fx.config_hash(1, "h1d")
    b = fx.config_hash(1, "h1d")
    assert a == b and len(a) == 64


# ---------------------------------------------------------------------------
# admission
# ---------------------------------------------------------------------------

TRADEABLE = {"NVDA", "AAPL", "QQQ"}
EPOCH = "2026-08-01"


def _cand(**kw):
    base = {
        "id": 1,
        "created_at": "2026-08-02T14:00:00+00:00",
        "ticker": "NVDA",
        "direction": "bullish",
        "rule_keys": ["8k::h1d", "insider::h7d"],
    }
    base.update(kw)
    return base


def test_admit_bullish_horizon_tradeable():
    assert fx.is_admitted(_cand(), horizon_tag="h1d",
                          tradeable_tickers=TRADEABLE, forward_epoch=EPOCH) is True


def test_reject_bearish():
    assert fx.is_admitted(_cand(direction="bearish"), horizon_tag="h1d",
                          tradeable_tickers=TRADEABLE, forward_epoch=EPOCH) is False


def test_reject_inst_placeholder():
    assert fx.is_admitted(_cand(ticker="INST_BLACKROCK"), horizon_tag="h1d",
                          tradeable_tickers=TRADEABLE, forward_epoch=EPOCH) is False


def test_reject_non_tradeable_fund():
    # VTSAX is not in the tradeable set -> excluded (funds price on NAV).
    assert fx.is_admitted(_cand(ticker="VTSAX"), horizon_tag="h1d",
                          tradeable_tickers=TRADEABLE, forward_epoch=EPOCH) is False


def test_reject_wrong_horizon_tag():
    # Candidate carries only ::h7d; the h1d experiment must reject it.
    c = _cand(rule_keys=["8k::h7d", "insider::h7d"])
    assert fx.is_admitted(c, horizon_tag="h1d",
                          tradeable_tickers=TRADEABLE, forward_epoch=EPOCH) is False


def test_h7d_admits_h7d_tag():
    c = _cand(rule_keys=["8k::h7d"])
    assert fx.is_admitted(c, horizon_tag="h7d",
                          tradeable_tickers=TRADEABLE, forward_epoch=EPOCH) is True


def test_admit_real_single_colon_rule_key_format():
    # REGRESSION (2026-08-02): LIVE candidates use `type:subtype:h1d` (SINGLE colon),
    # e.g. `news_article:positive:h1d`. The original `endswith("::h1d")` match hit
    # ONLY empty-subtype keys and missed all of these -> the experiment would have
    # been silently starved. Matching the last colon-segment admits both forms.
    real = _cand(rule_keys=["news_article:positive:h1d", "news_article:positive:h7d",
                            "news_article:positive:h15d", "news_article:positive:h30d"])
    assert fx.is_admitted(real, horizon_tag="h1d",
                          tradeable_tickers=TRADEABLE, forward_epoch=EPOCH) is True
    assert fx.is_admitted(real, horizon_tag="h7d",
                          tradeable_tickers=TRADEABLE, forward_epoch=EPOCH) is True
    # 'h1d' must NOT substring-match 'h15d' (== on the segment, not endswith/contains).
    h15_only = _cand(rule_keys=["news_article:positive:h15d"])
    assert fx.is_admitted(h15_only, horizon_tag="h1d",
                          tradeable_tickers=TRADEABLE, forward_epoch=EPOCH) is False


def test_reject_before_forward_epoch():
    c = _cand(created_at="2026-07-15T10:00:00+00:00")
    assert fx.is_admitted(c, horizon_tag="h1d",
                          tradeable_tickers=TRADEABLE, forward_epoch=EPOCH) is False


def test_no_epoch_disables_forward_gate():
    c = _cand(created_at="2020-01-01T00:00:00+00:00")
    assert fx.is_admitted(c, horizon_tag="h1d",
                          tradeable_tickers=TRADEABLE, forward_epoch=None) is True


# ---------------------------------------------------------------------------
# anti-lookahead entry
# ---------------------------------------------------------------------------

def _bars(*dates):
    return {dt.date.fromisoformat(d): {"open": 100.0, "high": 101.0,
                                       "low": 99.0, "close": 100.5}
            for d in dates}


def test_entry_strictly_after_created_date():
    bars = _bars("2026-07-10", "2026-07-11", "2026-07-13")
    got = fx.entry_after(bars, dt.date(2026, 7, 10))
    assert got is not None
    assert got[0] == dt.date(2026, 7, 11)   # NOT the same-day 07-10 bar


def test_entry_skips_same_day_lands_next_session():
    bars = _bars("2026-07-10", "2026-07-11", "2026-07-13")
    got = fx.entry_after(bars, dt.date(2026, 7, 11))
    assert got[0] == dt.date(2026, 7, 13)   # 07-11 excluded (same day), gap to 07-13


def test_entry_none_when_too_fresh():
    bars = _bars("2026-07-10", "2026-07-11")
    assert fx.entry_after(bars, dt.date(2026, 7, 11)) is None


# ---------------------------------------------------------------------------
# forward / replay split
# ---------------------------------------------------------------------------

def test_split_forward_replay_by_epoch():
    positions = [
        {"candidate_id": 1, "created_at": "2026-07-30T00:00:00+00:00"},  # pre
        {"candidate_id": 2, "created_at": "2026-08-01T00:00:00+00:00"},  # on epoch
        {"candidate_id": 3, "created_at": "2026-08-05T00:00:00+00:00"},  # post
    ]
    fwd, rep = fx.split_forward_replay(positions, "2026-08-01")
    assert {p["candidate_id"] for p in fwd} == {2, 3}   # >= epoch is forward
    assert {p["candidate_id"] for p in rep} == {1}


def test_split_no_epoch_all_forward():
    positions = [{"candidate_id": 1, "created_at": "2000-01-01T00:00:00+00:00"}]
    fwd, rep = fx.split_forward_replay(positions, None)
    assert len(fwd) == 1 and rep == []


# ---------------------------------------------------------------------------
# cohort counting
# ---------------------------------------------------------------------------

def test_count_cohorts_by_entry_date():
    positions = [
        {"entry_session": "2026-07-10"},
        {"entry_session": "2026-07-10"},   # same day -> same cohort
        {"entry_session": "2026-07-11"},
    ]
    assert fx.count_cohorts(positions) == 2


def test_count_cohorts_ignores_ungraded():
    positions = [{"entry_session": None}, {"entry_session": "2026-07-10"}]
    assert fx.count_cohorts(positions) == 1


# ---------------------------------------------------------------------------
# dedup by (ticker, entry_session, horizon)
# ---------------------------------------------------------------------------

def test_dedup_same_ticker_session_horizon():
    positions = [
        {"candidate_id": 9, "ticker": "NVDA", "entry_session": "2026-07-11",
         "horizon_days": 1, "net_return": 0.02, "qqq_return": 0.01, "excess": 0.01},
        {"candidate_id": 5, "ticker": "NVDA", "entry_session": "2026-07-11",
         "horizon_days": 1, "net_return": 0.03, "qqq_return": 0.01, "excess": 0.02},
    ]
    dd = fx.dedup_positions(positions)
    assert len(dd) == 1
    assert dd[0]["candidate_id"] == 5   # lowest candidate_id wins (deterministic)


def test_dedup_keeps_distinct_tickers_same_day():
    positions = [
        {"candidate_id": 1, "ticker": "NVDA", "entry_session": "2026-07-11",
         "horizon_days": 1},
        {"candidate_id": 2, "ticker": "AAPL", "entry_session": "2026-07-11",
         "horizon_days": 1},
    ]
    assert len(fx.dedup_positions(positions)) == 2


def test_dedup_distinct_horizon_not_collapsed():
    positions = [
        {"candidate_id": 1, "ticker": "NVDA", "entry_session": "2026-07-11",
         "horizon_days": 1},
        {"candidate_id": 2, "ticker": "NVDA", "entry_session": "2026-07-11",
         "horizon_days": 7},
    ]
    assert len(fx.dedup_positions(positions)) == 2


def test_dedup_drops_ungraded():
    positions = [{"candidate_id": 1, "ticker": "NVDA", "entry_session": None,
                  "horizon_days": 1}]
    assert fx.dedup_positions(positions) == []


# ---------------------------------------------------------------------------
# store immutability of a frozen closed outcome
# ---------------------------------------------------------------------------

def _save(conn, exp, cid, status, net):
    return store.save_position(
        conn, exp, candidate_id=cid, ticker="NVDA", direction="long",
        horizon_days=1, created_at="2026-08-02T00:00:00+00:00",
        entry_session="2026-08-03", entry_px=100.0,
        exit_session="2026-08-04" if status == "closed" else None,
        exit_px=105.0 if status == "closed" else None,
        exit_reason="horizon" if status == "closed" else None,
        net_return=net if status == "closed" else None,
        qqq_return=0.01 if status == "closed" else None,
        excess=(net - 0.01) if status == "closed" else None,
        status=status)


def test_closed_position_is_immutable(tmp_path):
    conn = store.connect(tmp_path / "exp.db")
    store.init(conn)
    exp = "fwd_prov_long_h1d"
    # Open first -> can update.
    assert _save(conn, exp, 1, "open", None) is True
    # Freeze closed.
    assert _save(conn, exp, 1, "closed", 0.04) is True
    frozen = store.get_position(conn, exp, 1)
    assert frozen["status"] == "closed" and abs(frozen["net_return"] - 0.04) < 1e-9
    # Attempt to overwrite the frozen row -> no-op, value unchanged.
    assert _save(conn, exp, 1, "closed", 0.99) is False
    still = store.get_position(conn, exp, 1)
    assert abs(still["net_return"] - 0.04) < 1e-9


def test_experiments_isolated_in_shared_db(tmp_path):
    conn = store.connect(tmp_path / "exp.db")
    store.init(conn)
    _save(conn, "fwd_prov_long_h1d", 1, "closed", 0.04)
    _save(conn, "fwd_prov_long_h7d", 1, "closed", 0.07)
    assert len(store.all_positions(conn, "fwd_prov_long_h1d")) == 1
    assert len(store.all_positions(conn, "fwd_prov_long_h7d")) == 1
    # Same candidate_id, different experiment -> independent rows.
    assert store.get_position(conn, "fwd_prov_long_h1d", 1)["net_return"] != \
        store.get_position(conn, "fwd_prov_long_h7d", 1)["net_return"]


def test_config_hash_drift_raises(tmp_path):
    conn = store.connect(tmp_path / "exp.db")
    store.init(conn)
    exp = "fwd_prov_long_h1d"
    store.check_config_hash(conn, exp, "hash_A")   # first run sets it
    store.check_config_hash(conn, exp, "hash_A")   # same -> ok
    try:
        store.check_config_hash(conn, exp, "hash_B")
        assert False, "expected config_hash drift to raise"
    except ValueError:
        pass


def test_state_roundtrip(tmp_path):
    conn = store.connect(tmp_path / "exp.db")
    store.init(conn)
    exp = "fwd_prov_long_h1d"
    store.check_config_hash(conn, exp, "hash_A")
    store.set_forward_epoch(conn, exp, "2026-08-02")
    store.set_cursor(conn, exp, "2026-08-02T00:00:00+00:00")
    store.ingest_candidate(conn, exp, candidate_id=1,
                           created_at="2026-08-02T00:00:00+00:00", ticker="NVDA",
                           direction="bullish", score=55.0, rule_keys="[]",
                           source_agents="[]", raw="{}")
    _save(conn, exp, 1, "closed", 0.04)
    snap = store.export_state(conn, exp)

    conn2 = store.connect(tmp_path / "exp2.db")
    store.init(conn2)
    store.import_state(conn2, snap)
    assert store.get_state(conn2, exp)["forward_epoch"] == "2026-08-02"
    assert store.get_state(conn2, exp)["config_hash"] == "hash_A"
    assert len(store.all_candidates(conn2, exp)) == 1
    assert store.get_position(conn2, exp, 1)["status"] == "closed"


# ---------------------------------------------------------------------------
# classify_tier — the go/no-go verdict (was zero-covered)
# ---------------------------------------------------------------------------

def _fwd(**kw):
    base = dict(n_cohorts=40, weeks=10.0, mean_cohort_excess=0.01, max_drawdown=0.05,
                top_cohort_excess_share=0.3, profit_factor=1.6, subperiods_positive=2)
    base.update(kw)
    return base


def test_tier_sync_failed_withholds():
    assert fx.classify_tier(_fwd(), sync_ok=False)["status"] == "inconclusive"


def test_tier_insufficient_sample_inconclusive():
    assert fx.classify_tier(_fwd(n_cohorts=10))["status"] == "inconclusive"
    assert fx.classify_tier(_fwd(weeks=3.0))["status"] == "inconclusive"


def test_tier_drawdown_breach_fails():
    r = fx.classify_tier(_fwd(max_drawdown=0.25))
    assert r["status"] == "fail" and "drawdown" in r["reason"]


def test_tier_clear_negative_excess_fails():
    r = fx.classify_tier(_fwd(mean_cohort_excess=-0.02))   # -2% << -0.5% margin
    assert r["status"] == "fail" and r["reason"] == "clear_negative_excess"


def test_tier_marginal_negative_is_inconclusive_not_fail():
    # -0.1% is inside the noise band → keep running, do NOT kill on noise.
    r = fx.classify_tier(_fwd(mean_cohort_excess=-0.001))
    assert r["status"] == "inconclusive" and r["reason"] == "marginal_negative_excess"


def test_tier_single_cohort_dominates_inconclusive():
    assert fx.classify_tier(_fwd(top_cohort_excess_share=1.0))["status"] == "inconclusive"


def test_tier_positive_sufficient_continues():
    assert fx.classify_tier(_fwd())["status"] == "continue"


def test_tier_scale_paper_when_tier2_met():
    r = fx.classify_tier(_fwd(n_cohorts=60, weeks=14.0, mean_cohort_excess=0.02,
                              profit_factor=1.6, subperiods_positive=2))
    assert r["status"] == "scale_paper"


def test_tier_scale_blocked_by_low_pf():
    # Tier-② sample met but PF below 1.4 → stays 'continue', not 'scale_paper'.
    r = fx.classify_tier(_fwd(n_cohorts=60, weeks=14.0, profit_factor=1.1))
    assert r["status"] == "continue"
