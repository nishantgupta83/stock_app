"""
Thesis agent.

Reads recent stock_normalized_events, applies §17.7 100-point rubric and
§15.3 cluster rule, produces stock_signals, and hands off to the Telegram
dispatcher (in-process).

Run via .github/workflows/thesis_agent.yml on */5 cron.

v1 active rubric rows:
  +25 new 8-K (operating company)
  +20 new SC 13D
  +10 new SC 13G
  +15 Truth Social mapping
  +0..+20 filing severity uplift  (severity 1 → +0, 4 → +20)
  +5..+40 earnings beat/miss magnitude (direction handled separately)
  -10 staleness for short-lived social/news events

Cluster rule:
  Need ≥2 distinct source agents within a 5-min window.
  Single-source exceptions: SC 13D, or 8-K with severity == 4.

Alert-fatigue governor:
  Max 5 alerts/day. Dedupe 60 min per (ticker, event_type).
  Confidence floor 0.65 (here we use score ≥ 50 → at-least-RESEARCH; score ≥ 70 → WATCH).
"""
from __future__ import annotations

import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

import _rule_key   # agents/ is on sys.path at runtime; canonical rule_key

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

HEADERS_SB = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
    "Prefer":        "resolution=ignore-duplicates,return=minimal",
}

# Alpha-decay TTL per event type. write_signal computes valid_until as
# fired_at + max(SIGNAL_TTL_HOURS[event_type]) across the cluster, so a
# signal stays valid as long as any of its evidence remains fresh. Values
# reflect typical informational half-life: news decays in hours, filings
# in days, binary catalysts (FDA/clinical/DoD) over a fortnight.
SIGNAL_TTL_HOURS: dict[str, float] = {
    # Fast-decay sentiment
    "news_article":            4,
    "truth_social_post":       6,
    # Macro releases — pricing absorbs within a day or two
    "vix_spike":              24,
    "yield_milestone":        48,
    "yield_snapshot":         48,
    "fomc_decision":          48,
    "cpi_release":            48,
    "nfp_release":            48,
    "crypto_macro_move":      48,
    "momentum":               24,
    # Operational events — multi-day reaction window
    "8k_material_event":     120,    # 5d
    "earnings_release":       72,    # 3d
    "consumer_sentiment":    120,
    "traffic_data":          120,
    # Slower-burn position / catalyst events
    "filing_13g":            168,    # 7d
    "filing_4":              168,
    "filing_13d":            336,    # 14d — activist take-up takes time
    "institutional_buy":     336,
    "institutional_sell":    336,
    "activist_initial_position": 336,
    "insider_cluster_buy":   336,
    # Binary catalysts — held to event date
    "fda_pdufa_decision":    336,
    "clinical_readout":      336,
    "nuclear_license_approval": 336,
    "dod_contract_award":    336,
}
DEFAULT_SIGNAL_TTL_HOURS = 72  # 3 days for unknown event types

# Look back this far for "fresh" events. GitHub cron can be delayed or skipped;
# dedupe_key prevents duplicate signals, so a wider replay window is safer than
# missing a valid cluster.
FRESHNESS_WINDOW_MIN = 180
# Cluster window widened from 5→30 min after AMD's 2026-05-05 earnings missed
# clustering: earnings_release landed at 20:00 UTC, the matching 8-K landed at
# 20:16 UTC, both single-source in different 5-min buckets. Both clearly
# describe the same coordinated information event. 30 min comfortably bundles
# pre-market opens, after-hours releases, and same-press-conference reactions
# from filing_agent / news_agent / earnings_agent. cluster_passes still gates
# on ≥2 distinct AGENTS, so wider window doesn't relax the multi-source rule.
CLUSTER_WINDOW_MIN   = 30
MAX_ALERTS_PER_DAY   = 5
# Cluster-passes score override: if a single-source cluster's COMPUTED score
# crosses CLUSTER_SCORE_OVERRIDE_THRESHOLD, defer to the rubric over the
# source-count heuristic. The rubric already encodes "is this alert-worthy"
# via its scoring rules; cluster_passes is a coarser pre-rubric heuristic.
# When both gates apply, the more conservative wins — but when score>=50 the
# rubric has explicit conviction, so cluster_passes should not double-gate.
# Confirmed by 2026-06-02 rejection audit: 100% of thesis silence traced to
# single_source_no_exception; 9 of those clusters had score>=50 and would
# have legitimately emitted MOMENTUM_ONLY signals (paper-tier, BUY/SELL
# still maturity-gated). Feature flag (default off — flip via secret).
CLUSTER_SCORE_OVERRIDE_THRESHOLD = 50.0

# ─── TEMPORARY SCAFFOLDING (2026-06-04) — NOT the final design ──────────────
# The emit floor (RESEARCH tier) was calibrated against the PRE-PR1A score
# scale. PR1A (2026-05-22) zeroed background-role 13F inflation, ~halving the
# scale (verified: PFE same biotech catalyst 91.51 → 44.17), but this floor was
# never re-tuned → Layer 2 emitted 0 signals for 13 days while Layer 5 kept
# learning (6 rules matured, PF 2.8–9.3). Dropping the recall floor 50→30
# unblocks emission and resumes feeding the learning loop. 30 is the empirically
# defensible recall floor: it admits the weakest MATURE rule (a lone 8-K = 25pts,
# 8k_material_event::h15d PF 2.81, acc_30d 0.90) while the noise floor sits at
# avg ~5.6. MAX_ALERTS_PER_DAY=5 + dedup cap any flood.
# THE REAL FIX is the 2.a/2.b meta-labeling funnel (loose recall floor + a
# payoff-aware precision gate keyed on stock_rule_calibration expectancy) — see
# docs/design/layer2-metalabeling-funnel.md. Remove this constant when 2.b lands.
# Env-overridable for fast rollback: `THESIS_RECALL_FLOOR=50` restores old gate.
THESIS_RECALL_FLOOR = float(os.environ.get("THESIS_RECALL_FLOOR", "30"))

# Structural flips: rule_keys with n>=30, PF<1.0, acc<50% per the 2026-06-03
# quarterly consultant review. When STRUCTURAL_FLIP_ENABLED, signals whose
# event-set is dominated by these rule_keys get their direction inverted
# (bullish→bearish, bearish→bullish) at emit time.
#
# IMPORTANT: this only affects the LIVE thesis emit. event_paper_agent +
# price_agent continue to record paper trades under the ORIGINAL direction
# convention, so the calibration loop keeps producing the verdict ("this
# rule loses money in this direction"). The flip is the operator's response
# to that verdict, applied downstream. If a flip turns out to be wrong, the
# calibration data is uncorrupted — disable the flag and we revert cleanly.
#
# Each rule's evidence at flip time (cited in docs/learning/2026Q1_consultant_review.md):
#   filing_13g::h1d           n=95   acc=33.7%  PF=0.34
#   earnings_release:miss:h30d n=124  acc=46.8%  PF=0.57
#   earnings_release:miss:h15d n=145  acc=47.6%  PF=0.65
#   8k_material_event::h1d    n=1168 acc=45.3%  PF=0.67
#   earnings_release:beat:h1d  n=474  acc=43.2%  PF=0.79
#
# We use the LIVE-horizon rule_key (h1d, per horizon_for()) for the lookup.
# Trades on longer horizons still calibrate as usual; the flip only fires
# when the cluster's h1d-projection lands in this set.
STRUCTURAL_FLIP: set[str] = {
    "filing_13g::h1d",
    "earnings_release:miss:h30d",
    "earnings_release:miss:h15d",
    "8k_material_event::h1d",
    "earnings_release:beat:h1d",
}

# Chase-risk threshold: if the price has already moved >5% in the cluster's
# direction since the earliest event, downgrade WATCH→RESEARCH (the move is
# already in the price; we'd be chasing). Bearish AVOID_CHASE doesn't get
# downgraded — late-warning bearish alerts are still useful.
CHASE_RISK_PCT = 0.05
DEDUPE_WINDOW_MIN    = 60

MODEL_VERSION = "rubric-v1.1"

# ============================================================
# Intelligence layer constants
# ============================================================

# Hyperscaler → supplier fan-out. When a hyperscaler files an 8-K mentioning
# capex/infrastructure/AI, suppliers get a +12 score boost. Mapping reflects
# disclosed partnerships and supply chain relationships (Stargate, NVDA $40B
# equity bets, hyperscaler PPAs).
HYPERSCALER_SUPPLIERS: dict[str, list[str]] = {
    "MSFT":  ["NVDA","AMD","AVGO","ANET","VRT","CEG","DELL","SMCI","CRWD","MU","CRWV"],
    "GOOGL": ["AVGO","NVDA","ANET","TSM","BRCM"],
    "GOOG":  ["AVGO","NVDA","ANET","TSM"],
    "META":  ["NVDA","AVGO","ANET","ASML","TSM","ARM"],
    "AMZN":  ["NVDA","AMD","AVGO","ANET","INTC"],
    "ORCL":  ["NVDA","AMD","DELL","CEG"],
}
HYPERSCALER_TICKERS = set(HYPERSCALER_SUPPLIERS.keys())
CAPEX_KEYWORDS = ("capex","capital expenditure","ai infrastructure","data center",
                  "compute capacity","gpu","ai cluster")

# Power utilities — when these file severity-4 8-K (typically a hyperscaler PPA),
# treat as AI demand confirmation. Boost all AI compute + server tickers.
POWER_UTILITIES = {"CEG","VST","TLN","NRG"}
POWER_SCARCITY_BENEFICIARY_LISTS = ("ai_compute","ai_servers","ai_optical")
POWER_SCARCITY_LOOKBACK_HOURS = 7 * 24

# VIX risk-off threshold. Above this, downgrade bullish actions one tier.
VIX_RISKOFF_LEVEL = 25.0

# Severity-4 priority alert: bypass the 5-alert daily cap so we never miss a
# critical event (major 8-K, earnings beat >10% surprise). This is the LITE-style
# spike fix — when something hits at severity 4, you hear about it immediately.
SEV4_PRIORITY_BYPASS_CAP = True


# ============================================================
# Operational logging
# ============================================================

def job_run_start(agent: str) -> int | None:
    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/stock_job_runs",
            headers={**HEADERS_SB, "Prefer": "return=representation"},
            json={"agent": agent}, timeout=10,
        )
        if r.status_code in (200, 201) and r.json():
            return r.json()[0]["id"]
    except Exception as e:  # noqa: BLE001
        print(f"  job_run_start failed: {e}", file=sys.stderr)
    return None


def job_run_finish(run_id: int | None, status: str, rows_in: int, rows_out: int, err: str | None = None) -> None:
    if run_id is None:
        return
    try:
        requests.patch(
            f"{SUPABASE_URL}/rest/v1/stock_job_runs?id=eq.{run_id}",
            headers=HEADERS_SB,
            json={
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "status":      status,
                "rows_in":     rows_in,
                "rows_out":    rows_out,
                "error_text":  err,
            }, timeout=10,
        )
    except Exception as e:  # noqa: BLE001
        print(f"  job_run_finish failed: {e}", file=sys.stderr)


# ============================================================
# Fetch fresh evidence
# ============================================================

# Single source of truth for evidence role + catalyst age policy lives in
# agents/_catalyst_policy.py — imported here as `_evidence_policy_for`
# so the existing call sites remain unchanged. See that module for the
# full rationale and per-type policy entries.
from _catalyst_policy import (
    CATALYST_POLICY as _CATALYST_POLICY,
    policy_for as _evidence_policy_for,
)


def evidence_policy_for(event_type: str) -> dict:
    """Compat alias to _catalyst_policy.policy_for."""
    return _evidence_policy_for(event_type)


# Module-level re-export so existing test imports keep working.
CATALYST_POLICY = _CATALYST_POLICY


# Pre-existing real-world TTL filter for event-fetch (separate from the
# catalyst-role policy). Kept for backward compat with fetch_fresh_events.
EVENT_REAL_TTL_HOURS: dict[str, int] = {
    "earnings_release":           72,
    "8k_material_event":         168,    # 7 days
    "filing_dilution":           168,
    "filing_s-3":                168,
    "filing_s-3/a":              168,
    "filing_13d":                336,    # 14 days — activist windows are slow
    "filing_13g":                336,
    "truth_social_post":          24,
    "news_article":               48,
    "institutional_new_position": 720,   # 30 days — 13F arrives weeks after the print
    "institutional_increase":     720,
    "institutional_exit":         720,
    "institutional_decrease":     720,
    "activist_5pct_crossed":      336,
    "activist_initial_position":  336,
    "insider_cluster_buy":        168,
    "fda_pdufa_decision":         168,
    "clinical_readout":           168,
    "dod_contract_award":         168,
    "nuclear_license_approval":   336,
    "momentum":                    24,
    "crypto_macro_move":           24,
    "price_gap":                   24,
    "volume_anomaly":              24,
    "volatility_spike":            24,
    "vix_spike":                   24,
    "yield_milestone":             48,
    "fomc_decision":              168,
    "cpi_release":                 72,
    "nfp_release":                 72,
}
EVENT_REAL_TTL_DEFAULT_HOURS = 168       # 7 days for any event_type not listed


def _event_within_real_ttl(event: dict, now: datetime) -> bool:
    """True if event_at is recent enough to be relevant per its event_type TTL."""
    event_at = event.get("event_at")
    if not event_at:
        return True   # don't drop on missing field
    try:
        ea = datetime.fromisoformat(event_at.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return True
    ttl_hours = EVENT_REAL_TTL_HOURS.get(event.get("event_type") or "",
                                         EVENT_REAL_TTL_DEFAULT_HOURS)
    return (now - ea) <= timedelta(hours=ttl_hours)


def fetch_fresh_events() -> list[dict]:
    """Pull normalized_events that LANDED in the freshness window, then drop
    rows whose real-world event_at is older than the per-event_type TTL.

    Pre-E2 this filtered on event_at, contradicting CLAUDE.md rule #1
    ("for what landed recently, use created_at"). The intelligence layer
    wants to react to what just arrived, not what just happened in the world
    — a 13F filed today is news even if the underlying position dates from
    last quarter. The per-event-type TTL still protects against junk events
    arriving from a backfill or a long-paused ingester.

    Use params= so requests URL-encodes the +00:00 in the ISO timestamp."""
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(minutes=FRESHNESS_WINDOW_MIN)).isoformat()
    params = [
        ("created_at", f"gte.{cutoff}"),
        ("ticker", "not.is.null"),
        ("select", "id,event_type,event_subtype,ticker,event_at,created_at,severity,source_table,parser_confidence,payload"),
        ("order", "created_at.desc"),
        ("limit", "500"),
    ]
    r = requests.get(f"{SUPABASE_URL}/rest/v1/stock_normalized_events",
                     headers=HEADERS_SB, params=params, timeout=20)
    r.raise_for_status()
    raw = r.json()
    return [e for e in raw if _event_within_real_ttl(e, now)]


def fetch_latest_agent_weights() -> dict[str, float]:
    """Most recent weight per agent from stock_agent_weights, populated by
    price_agent (live signals) and backtester (historical replay).
    Default 1.0 if no row yet (fresh install / new agent)."""
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/stock_agent_weights",
        headers=HEADERS_SB,
        params={"select": "agent,date,weight", "order": "date.desc", "limit": "200"},
        timeout=15,
    )
    if r.status_code != 200:
        return {}
    latest: dict[str, float] = {}
    for row in r.json():
        agent = row.get("agent")
        if agent and agent not in latest:
            try:
                latest[agent] = float(row.get("weight") or 1.0)
            except (TypeError, ValueError):
                continue
    return latest


def fetch_recent_closes(tickers: list[str], days_back: int = 7) -> dict[str, list[dict]]:
    """Up-to-7-day daily closes per ticker from stock_raw_prices, populated by
    historical_ingest + site_generator's self-healing fallback. Returns
    {ticker: [{ts, close}]} ordered by ts asc — latest is the rightmost."""
    if not tickers:
        return {}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).date().isoformat()
    in_list = ",".join(f'"{t}"' for t in tickers)
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/stock_raw_prices",
        headers=HEADERS_SB,
        params={
            "ticker": f"in.({in_list})",
            "ts":     f"gte.{cutoff}",
            "select": "ticker,ts,close",
            "order":  "ts.asc",
            "limit":  "500",
        },
        timeout=15,
    )
    if r.status_code != 200:
        return {}
    by_t: dict[str, list[dict]] = defaultdict(list)
    for row in r.json():
        if row.get("close") is not None:
            by_t[row["ticker"]].append(row)
    return dict(by_t)


def chase_risk_pct(closes: list[dict], earliest_event_iso: str) -> float | None:
    """Return % move of the latest close vs. the close on/before the cluster's
    earliest event date. None if not enough data."""
    if not closes or not earliest_event_iso:
        return None
    try:
        ev_date = earliest_event_iso[:10]   # YYYY-MM-DD
    except Exception:
        return None
    # Find the bar at or just before ev_date
    base = None
    for row in closes:
        if (row.get("ts") or "")[:10] <= ev_date:
            base = row
        else:
            break
    if base is None or not base.get("close"):
        return None
    latest = closes[-1]
    if not latest.get("close"):
        return None
    try:
        return (float(latest["close"]) - float(base["close"])) / float(base["close"])
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def already_signaled_dedupe_keys(keys: list[str]) -> set[str]:
    """Return which of these dedupe_keys already exist in stock_signals."""
    if not keys:
        return set()
    in_list = ",".join(f'"{k}"' for k in keys)
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/stock_signals?dedupe_key=in.({in_list})&select=dedupe_key",
        headers=HEADERS_SB, timeout=15,
    )
    if r.status_code != 200:
        return set()
    return {row["dedupe_key"] for row in r.json()}


def existing_signals_by_dedupe(keys: list[str]) -> dict[str, int]:
    """Return {dedupe_key: signal_id} for existing signals."""
    if not keys:
        return {}
    in_list = ",".join(f'"{k}"' for k in keys)
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/stock_signals?dedupe_key=in.({in_list})&select=id,dedupe_key",
        headers=HEADERS_SB, timeout=15,
    )
    if r.status_code != 200:
        return {}
    return {
        str(row["dedupe_key"]): int(row["id"])
        for row in r.json()
        if row.get("dedupe_key") and row.get("id") is not None
    }


def alerts_sent_today(model_version: str | None = None) -> int:
    """Count signals already sent today (UTC) — for the daily cap.

    With model_version, counts only signals written by that scoring lane.
    This is the per-lane budget pattern: thesis_agent's MAX_ALERTS_PER_DAY
    is a guardrail on rubric-based dispatch volume, not on every signal in
    the system. intraday_alert_agent (model_version='intraday-spike-v1')
    has its own per-run safety cap (ALERT_CAP=25) and was never intended
    to share thesis's daily budget — but the unscoped query meant intraday
    bursts were burning thesis's cap, silently silencing the rubric path.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    params = {
        "fired_at":  f"gte.{today}T00:00:00Z",
        "status_v2": "eq.sent",
        "select":    "id",
    }
    if model_version:
        params["model_version"] = f"eq.{model_version}"
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/stock_signals",
        headers=HEADERS_SB,
        params=params,
        timeout=15,
    )
    if r.status_code != 200:
        return 0
    return len(r.json())


def recently_dispatched(ticker: str, event_type: str) -> bool:
    """Dedupe: was an alert sent for this (ticker, event_type) in the last DEDUPE_WINDOW_MIN?"""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=DEDUPE_WINDOW_MIN)).isoformat()
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/stock_signals",
        headers=HEADERS_SB,
        params={
            "ticker":    f"eq.{ticker}",
            "fired_at":  f"gte.{cutoff}",
            "status_v2": "eq.sent",
            "select":    "id",
            "limit":     "25",
        },
        timeout=10,
    )
    if r.status_code != 200 or not r.json():
        return False
    ids = ",".join(str(row["id"]) for row in r.json() if row.get("id") is not None)
    if not ids:
        return False
    ev = requests.get(
        f"{SUPABASE_URL}/rest/v1/stock_signal_evidence",
        headers=HEADERS_SB,
        params={
            "signal_id": f"in.({ids})",
            "detail":    f"like.{event_type}%",
            "select":    "id",
            "limit":     "1",
        },
        timeout=10,
    )
    return ev.status_code == 200 and len(ev.json()) > 0


# ============================================================
# Scoring (§17.7 active-Phase-1 rules only)
# ============================================================

def source_agent_for(event: dict) -> str:
    """Map normalized event back to its originating agent."""
    et = event["event_type"]
    if et == "truth_social_post":   return "truth_social"
    if et == "news_article":        return "news"
    if et.startswith("filing_"):    return "filing"
    if et == "8k_material_event":   return "filing"
    if et == "position_change":     return "flows"
    if et in ("price_gap", "volume_anomaly", "volatility_spike", "momentum"): return "price"
    if et in ("news_headline",):    return "news"
    if et.startswith("earnings_"):  return "earnings"
    if et in ("vix_spike", "yield_milestone", "fomc_decision", "cpi_release",
              "nfp_release", "yield_snapshot", "consumer_sentiment", "traffic_data"):
        return "macro"
    if et in ("activist_initial_position", "insider_cluster_buy"):
        return "activist"
    if et == "dod_contract_award":
        return "defense"
    if et in ("fda_pdufa_decision", "clinical_readout"):
        return "biotech"
    if et == "nuclear_license_approval":
        return "energy"
    return "unknown"


def score_evidence(events: list[dict],
                   agent_weights: dict[str, float] | None = None,
                   sector_multipliers: dict[tuple[str, str], float] | None = None,
                   ticker_sectors: dict[str, str] | None = None,
                   ) -> tuple[float, list[dict]]:
    """Apply Phase-1 rubric. Returns (total_score, breakdown).

    Per-agent learned weights from stock_agent_weights are applied at the end —
    each event-tied rule's contribution is summed per source-agent, then a single
    weight_adj_<agent> entry per agent shows the delta (raw × (weight − 1)).
    Cluster-level bonuses (multi_source_confirm, tri_source_confirm) carry no
    event_id and stay raw, since they reward independent agreement, not any
    single agent's accuracy.

    sector_multipliers / ticker_sectors are populated only when the
    SECTOR_CALIB_MULT_ENABLED env var is true. When non-empty, each event-tied
    rule's points are scaled by the (rule_key_at_live_horizon, ticker_sector)
    multiplier from the stock_rule_sector_multiplier view. View enforces n>=30
    floor and bounds multiplier to [0.5, 1.3], so impact is contained.
    """
    weights = agent_weights or {}
    sector_mults = sector_multipliers or {}
    sectors = ticker_sectors or {}
    score = 0.0
    breakdown: list[dict] = []
    raw_by_agent: dict[str, float] = defaultdict(float)
    # event_id → source agent map for cheap lookup inside add()
    ev_agent: dict[int, str] = {e["id"]: source_agent_for(e) for e in events if e.get("id") is not None}
    # event_id → role for this scoring batch (so add() can tag each breakdown entry)
    ev_role: dict[int, str] = {}
    ev_catalyst_eligible: dict[int, bool] = {}
    # event_id → event dict (for sector-multiplier lookup keyed by event_type+ticker)
    ev_lookup: dict[int, dict] = {e["id"]: e for e in events if e.get("id") is not None}
    now = datetime.now(timezone.utc)
    for e in events:
        ev_id = e.get("id")
        if ev_id is None:
            continue
        policy = evidence_policy_for(e.get("event_type") or "")
        role = policy["role"]
        ev_role[ev_id] = role
        # An event contributes to catalyst_score only if its role is catalyst
        # AND event_at is within max_age_hours of now.
        catalyst_ok = False
        if role == "catalyst":
            ea_str = e.get("event_at")
            if ea_str:
                try:
                    ea = datetime.fromisoformat(ea_str.replace("Z", "+00:00"))
                    if (now - ea) <= timedelta(hours=policy["max_age_hours"]):
                        catalyst_ok = True
                except (TypeError, ValueError):
                    pass
        ev_catalyst_eligible[ev_id] = catalyst_ok

    def add(rule: str, points: float, ev_id: int | None = None, detail: str = "") -> None:
        """Append a breakdown entry, tag with evidence role, accumulate score.

        Critical: background-role events are recorded in breakdown but EXCLUDED
        from `score` — they're display-only context. This prevents a 13F filing
        from inflating an alert score it shouldn't drive.
        """
        nonlocal score
        # Determine role for this breakdown entry
        if ev_id is not None and ev_id in ev_role:
            role = ev_role[ev_id]
            catalyst_ok = ev_catalyst_eligible.get(ev_id, False)
        else:
            # Cluster-level bonuses (no event_id) — treat as catalyst-tier
            # bonus since they reward cross-source confirmation, not background data.
            role = "bonus"
            catalyst_ok = True

        # Background-role events: record in breakdown but contribute 0 to score
        effective_points = points
        if role == "background":
            effective_points = 0.0

        # Sector-aware calibration multiplier. Only applies to event-tied entries
        # (cluster-level bonuses keep their raw weight). View-derived; floored at
        # n>=30 per cell and bounded [0.5, 1.3], so any single cell's impact is
        # capped. Gated entirely by SECTOR_CALIB_MULT_ENABLED on the consumer
        # side — when disabled, sector_mults is empty and lookup falls through.
        sector_mult = 1.0
        if effective_points and ev_id is not None and sector_mults:
            ev = ev_lookup.get(ev_id) or {}
            et = ev.get("event_type")
            ticker = ev.get("ticker")
            if et and ticker:
                # Live signals are audited at h1d (see horizon_for); use the
                # h1d rule_key so the multiplier reflects the horizon that
                # actually gets feedback in the live calibration loop.
                rk = _rule_key.derive(et, ev.get("event_subtype"), 1)
                sector = sectors.get(ticker, "Unknown")
                sector_mult = sector_mults.get((rk, sector), 1.0)
                if sector_mult != 1.0:
                    effective_points = effective_points * sector_mult

        score += effective_points
        if effective_points and ev_id is not None and ev_id in ev_agent:
            raw_by_agent[ev_agent[ev_id]] += effective_points
        breakdown.append({
            "rule":           rule,
            "points":         effective_points,
            "raw_points":     points,           # what would have been added if role allowed
            "event_id":       ev_id,
            "detail":         detail,
            "role":           role,             # catalyst | context | background | bonus
            "catalyst_ok":    catalyst_ok,      # True if this entry contributes to catalyst_score
            "sector_mult":    sector_mult if sector_mult != 1.0 else None,
        })

    for e in events:
        et = e["event_type"]
        sev = e.get("severity") or 0
        # New 8-K
        if et == "8k_material_event":
            add("new_8k", 25, e["id"], e.get("event_subtype") or "")
            # Severity uplift: sev=1 → +0, sev=4 → +20 (5 points per level above 0)
            if sev > 0:
                add(f"severity_uplift_sev{sev}", min(20, (sev - 1) * 5), e["id"])
        # Activist / passive >5% holders
        elif et == "filing_13d" or e.get("event_subtype") == "13D":
            add("new_sc_13d", 20, e["id"])
        elif et == "filing_13g" or e.get("event_subtype") == "13G":
            add("new_sc_13g", 10, e["id"])
        # Truth Social — score symmetrically. signal_direction() decides whether
        # the cluster routes to WATCH (bullish) or AVOID_CHASE (bearish).
        elif et == "truth_social_post":
            direction = (e.get("payload") or {}).get("direction_prior", "long")
            if direction == "short":
                add("truth_social_bearish", 15, e["id"], e.get("event_subtype") or "")
            else:
                add("truth_social_mapping", 15, e["id"], e.get("event_subtype") or "")
        # News article — symmetric: bearish confirmation adds the same weight as
        # bullish so AVOID_CHASE clusters can reach the 50-pt threshold.
        elif et == "news_article":
            direction = (e.get("payload") or {}).get("direction_prior", "neutral")
            if direction == "long":
                add("news_bullish", 12, e["id"], e.get("event_subtype") or "")
            elif direction == "short":
                add("news_bearish", 12, e["id"], e.get("event_subtype") or "")
            else:
                add("news_neutral", 5, e["id"], e.get("event_subtype") or "")
        # S-3 shelf registration = dilution headwind. Score measures evidence
        # strength; signal_direction() decides whether that evidence is bearish.
        elif et in ("filing_s-3", "filing_s-3/a"):
            add("s3_dilution_risk", 12, e["id"])
        # 8-K-flavored dilution (PIPE, ATM, warrant issuance, registered direct) —
        # filing_agent.looks_like_dilution emits this alongside the primary 8-K.
        # Symmetric magnitude so AVOID_CHASE clusters can reach threshold.
        elif et == "filing_dilution":
            add("dilution_8k", 12, e["id"], (e.get("payload") or {}).get("matched_keyword", ""))
        # Earnings releases are live events now. Beats and misses both add
        # evidence strength; direction is computed separately below.
        elif et == "earnings_release":
            sub = e.get("event_subtype") or "scheduled"
            payload = e.get("payload") or {}
            surprise_pct = payload.get("surprise_pct")
            try:
                surprise_val = float(surprise_pct) if surprise_pct is not None else None
            except (TypeError, ValueError):
                surprise_val = None
            surprise = abs(surprise_val) if surprise_val is not None else 0.0
            if sub == "beat":
                pts = 50 if surprise >= 10 else 35 if surprise >= 3 else 15
                add("earnings_beat", pts, e["id"], f"+{surprise_val:.1f}% vs est" if surprise_val is not None else "")
            elif sub == "miss":
                pts = 50 if surprise >= 10 else 35 if surprise >= 3 else 15
                add("earnings_miss", pts, e["id"], f"{surprise_val:.1f}% vs est" if surprise_val is not None else "")
            elif sub == "inline":
                add("earnings_inline", 5, e["id"])
            else:
                add("earnings_scheduled", 5, e["id"])
        elif et == "momentum":
            payload = e.get("payload") or {}
            try:
                rs = float(payload.get("rel_strength_pct") or 0)
            except (TypeError, ValueError):
                rs = 0.0
            if rs > 10:
                add("momentum_strong_long", 25, e["id"], f"+{rs:.1f}% vs SPY 20d")
            elif rs > 5:
                add("momentum_moderate_long", 15, e["id"], f"+{rs:.1f}% vs SPY 20d")
            elif rs < -10:
                add("momentum_strong_short", 20, e["id"], f"{rs:.1f}% vs SPY 20d")
            elif rs < -5:
                add("momentum_moderate_short", 10, e["id"], f"{rs:.1f}% vs SPY 20d")
        # Phase 8 — institutional flows from flows_agent. Direction priors are
        # carried in payload.direction_prior so signal_direction() routes the
        # cluster correctly. Calibration will refine these per (institution,
        # change_type) over time; the maturity gate decides whether
        # "BRK new_position" eventually graduates to BUY.
        elif et == "institutional_new_position":
            add("inst_new_position", 25, e["id"], (e.get("event_subtype") or ""))
        elif et == "institutional_exit":
            add("inst_exit",         20, e["id"], (e.get("event_subtype") or ""))
        elif et == "institutional_increase":
            add("inst_increase",     15, e["id"], (e.get("event_subtype") or ""))
        elif et == "institutional_decrease":
            add("inst_decrease",     12, e["id"], (e.get("event_subtype") or ""))
        elif et == "activist_5pct_crossed":
            add("activist_5pct",     30, e["id"], (e.get("event_subtype") or ""))
        # Other filings carry only the severity component
        elif et.startswith("filing_") and sev > 0:
            add(f"filing_other_sev{sev}", min(15, sev * 4), e["id"])
        # Macro regime events from macro_rates_agent. These don't bind to a
        # single ticker (ticker='MACRO' sentinel) but they still inform
        # cluster scoring when MACRO events fire alongside ticker-specific
        # ones in the same time bucket — interpret as "regime-tagged" cluster.
        elif et == "fomc_decision":
            sub = (e.get("event_subtype") or "")
            add(f"fomc_{sub}", 25, e["id"], sub)
        elif et == "cpi_release":
            add(f"cpi_{e.get('event_subtype') or 'inline'}",
                15 if sev >= 4 else 8, e["id"])
        elif et == "nfp_release":
            add(f"nfp_{e.get('event_subtype') or 'inline'}",
                15 if sev >= 4 else 8, e["id"])
        elif et == "yield_milestone":
            add(f"yield_{e.get('event_subtype') or 'level'}",
                20 if sev >= 4 else 12, e["id"])
        elif et == "vix_spike":
            add(f"vix_{e.get('event_subtype') or 'stress'}",
                20 if sev >= 4 else 12, e["id"])
        # Activist 13D + insider clusters from activist_insider_agent — strong fundamentals
        elif et == "activist_initial_position":
            add("activist_13d", 30, e["id"], e.get("event_subtype") or "")
        elif et == "insider_cluster_buy":
            add("insider_cluster", 20, e["id"],
                f"{(e.get('payload') or {}).get('filer_count','?')} filers")
        # Defense DoD contract awards
        elif et == "dod_contract_award":
            sub = e.get("event_subtype") or ""
            add(f"dod_{sub}", 25 if sub == "mega" else 12, e["id"],
                f"${(e.get('payload') or {}).get('amount','?')}")
        # Biotech FDA decisions
        elif et == "fda_pdufa_decision":
            sub = e.get("event_subtype") or "approval"
            pts = 35 if sub in ("approval","rejection") else 15
            add(f"fda_{sub}", pts, e["id"])
        elif et == "clinical_readout":
            add(f"clinical_{e.get('event_subtype') or 'update'}", 12, e["id"])
        # Energy transition catalysts
        elif et == "nuclear_license_approval":
            sub = e.get("event_subtype") or "filing"
            pts = 25 if sub == "approval" else 15 if sub == "denial" else 8
            add(f"nuclear_{sub}", pts, e["id"])
        # Consumer health cycle signals
        elif et == "consumer_sentiment":
            add(f"umich_{sev}", 20 if sev >= 4 else 10, e["id"])
        elif et == "traffic_data":
            add("tsa_yoy", 12 if sev >= 3 else 6, e["id"])

        # Staleness penalty — only for short-lived signal types.
        # SEC filings are valid for days; social/news posts expire fast.
        try:
            event_at = datetime.fromisoformat(e["event_at"].replace("Z", "+00:00"))
            age_min  = (now - event_at).total_seconds() / 60
            if et == "truth_social_post" and age_min > 30:
                add("staleness_social", -10, e["id"], f"{int(age_min)}m old")
            elif et == "news_article" and age_min > 120:
                add("staleness_news", -5, e["id"], f"{int(age_min/60):.1f}h old")
            # 8-K / filing_* events: no staleness — valid for the full horizon window
        except Exception:
            pass

    # Multi-source confirmation bonus — independent agents agreeing boosts confidence
    distinct_src = len({source_agent_for(e) for e in events})
    if distinct_src >= 3:
        add("tri_source_confirm", 13, detail=f"{distinct_src} agents")
    elif distinct_src >= 2:
        add("multi_source_confirm", 8, detail=f"{distinct_src} agents")

    # Apply learned per-agent weights — closes the loop with price_agent's
    # EMA. weight=1.0 means no change; <1 dampens chronically-wrong agents,
    # >1 amplifies reliable ones (bounded 0.1..2.0 in the EMA writer).
    weight_adj_total = 0.0
    for agent, raw in raw_by_agent.items():
        w = weights.get(agent, 1.0)
        if w == 1.0 or raw == 0:
            continue
        adj = raw * (w - 1.0)
        weight_adj_total += adj
        breakdown.append({
            "rule":      f"weight_adj_{agent}",
            "points":    round(adj, 2),
            "event_id":  None,
            "detail":    f"raw={raw:.0f} × (weight {w:.2f} − 1.0)",
        })
    score += weight_adj_total

    return score, breakdown


# ============================================================
# Intelligence layer — cross-rule signals
# ============================================================

def fetch_watchlist_map() -> dict[str, set[str]]:
    """{watchlist_name: {tickers}} for sector-cluster lookups.
    Limit set high enough to avoid silent truncation as watchlists grow."""
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/stock_watchlists",
        headers=HEADERS_SB,
        params={"select": "name,ticker", "limit": "5000"},
        timeout=10,
    )
    if r.status_code != 200:
        return {}
    by_list: dict[str, set[str]] = defaultdict(set)
    for row in r.json():
        if row.get("name") and row.get("ticker"):
            by_list[row["name"]].add(row["ticker"])
    return dict(by_list)


def fetch_recent_events_window(hours: int) -> list[dict]:
    """All severity≥2 events in last N hours — used for cross-ticker signals."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/stock_normalized_events",
        headers=HEADERS_SB,
        params=[
            ("created_at", f"gte.{cutoff}"),
            ("severity",   "gte.2"),
            ("ticker",     "not.is.null"),
            ("select",     "ticker,event_type,severity,payload,created_at"),
            ("order",      "created_at.desc"),
            ("limit",      "500"),
        ],
        timeout=15,
    )
    if r.status_code != 200:
        return []
    return r.json()


def sector_cluster_bonus(ticker: str, direction: str, recent_events: list[dict],
                          watchlist_map: dict[str, set[str]]) -> tuple[int, str]:
    """+20 if 2+ other tickers in the same sector watchlist fired same-direction
    events in the last 24h. This catches sector rotations early (AI optical
    melt-up, power-scarcity buying, etc)."""
    if direction == "neutral":
        return 0, ""
    # Find which sector watchlist(s) contain this ticker
    my_lists = [name for name, tickers in watchlist_map.items()
                if ticker in tickers and name.startswith("ai_")]
    if not my_lists:
        return 0, ""
    # Count same-direction events on other tickers in those watchlists
    peers = set()
    for name in my_lists:
        peers.update(watchlist_map.get(name, set()))
    peers.discard(ticker)
    if not peers:
        return 0, ""
    same_dir_peers: set[str] = set()
    for ev in recent_events:
        t = ev.get("ticker")
        if t not in peers:
            continue
        # Crude direction proxy from event payload + type
        d = (ev.get("payload") or {}).get("direction_prior") or ""
        et = ev.get("event_type") or ""
        is_bull = (d == "long" or et in ("8k_material_event","filing_13d",
                                          "institutional_new_position","institutional_increase",
                                          "earnings_release"))
        is_bear = (d == "short" or et in ("filing_s-3","filing_s-3/a","filing_dilution",
                                           "institutional_exit","institutional_decrease"))
        if direction == "bullish" and is_bull and not is_bear:
            same_dir_peers.add(t)
        elif direction == "bearish" and is_bear:
            same_dir_peers.add(t)
    if len(same_dir_peers) >= 2:
        # +20 base + extra +5 per additional confirming peer (cap +35)
        bonus = min(35, 20 + (len(same_dir_peers) - 2) * 5)
        detail = f"{my_lists[0]}: {len(same_dir_peers)} peers ({','.join(sorted(same_dir_peers)[:4])})"
        return bonus, detail
    return 0, ""


def hyperscaler_capex_echo(ticker: str, recent_events: list[dict]) -> tuple[int, str]:
    """+12 if ticker is a known supplier and a hyperscaler fired 8-K with
    capex/AI-infrastructure keywords in the last 24h."""
    # Which hyperscalers can affect this ticker?
    relevant_hs = [hs for hs, suppliers in HYPERSCALER_SUPPLIERS.items()
                   if ticker in suppliers]
    if not relevant_hs:
        return 0, ""
    for ev in recent_events:
        if ev.get("ticker") not in relevant_hs:
            continue
        if ev.get("event_type") != "8k_material_event":
            continue
        payload = ev.get("payload") or {}
        # Scan payload for capex keywords (description, headline, primary_doc_desc)
        haystack = " ".join(str(payload.get(k) or "") for k in
                            ("primary_doc_desc","headline","title","8k_items")).lower()
        if any(kw in haystack for kw in CAPEX_KEYWORDS):
            return 12, f"hyperscaler capex: {ev.get('ticker')} 8-K"
        # Sev-4 hyperscaler 8-Ks count even without keyword match — major
        # filings rarely fail to be AI-related given 2026 capex mix.
        if (ev.get("severity") or 0) >= 4:
            return 10, f"hyperscaler sev4 8-K: {ev.get('ticker')}"
    return 0, ""


def power_scarcity_active(ticker: str, recent_events: list[dict],
                           watchlist_map: dict[str, set[str]]) -> tuple[int, str]:
    """+15 if a power utility fired a severity-4 8-K in last 7 days AND
    this ticker is an AI compute/server/optical beneficiary."""
    # Is this ticker an AI beneficiary?
    is_beneficiary = any(ticker in watchlist_map.get(name, set())
                         for name in POWER_SCARCITY_BENEFICIARY_LISTS)
    if not is_beneficiary:
        return 0, ""
    # Was there a severity-4 utility 8-K recently?
    cutoff = datetime.now(timezone.utc) - timedelta(hours=POWER_SCARCITY_LOOKBACK_HOURS)
    for ev in recent_events:
        if ev.get("ticker") not in POWER_UTILITIES:
            continue
        if ev.get("event_type") != "8k_material_event":
            continue
        if (ev.get("severity") or 0) < 4:
            continue
        try:
            ts = datetime.fromisoformat((ev.get("created_at") or "").replace("Z","+00:00"))
            if ts < cutoff:
                continue
        except Exception:
            continue
        return 15, f"power scarcity: {ev.get('ticker')} 8-K sev4"
    return 0, ""


def is_risk_off() -> bool:
    """True if VIX > VIX_RISKOFF_LEVEL OR a macro_rates regime alert fired
    today (vix_spike, yield_milestone, fomc_decision hike). Falls through to
    False on any data gap — we never want this check to silently SUPPRESS
    bullish alerts based on a missing data source."""
    # 1. VIX check — fast, in our DB
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/stock_raw_prices",
        headers=HEADERS_SB,
        params={"ticker": "eq.VIX", "select": "close,ts", "order": "ts.desc", "limit": "1"},
        timeout=10,
    )
    if r.status_code == 200 and r.json():
        try:
            if float(r.json()[0]["close"]) > VIX_RISKOFF_LEVEL:
                return True
        except (TypeError, ValueError, KeyError):
            pass

    # 2. macro_rates_agent regime events in last 24h
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    r2 = requests.get(
        f"{SUPABASE_URL}/rest/v1/stock_normalized_events",
        headers=HEADERS_SB,
        params=[
            ("ticker",     "eq.MACRO"),
            ("event_type", "in.(vix_spike,yield_milestone,fomc_decision)"),
            ("created_at", f"gte.{cutoff}"),
            ("severity",   "gte.3"),
            ("select",     "event_type,event_subtype,severity,payload"),
            ("limit",      "10"),
        ],
        timeout=10,
    )
    if r2.status_code == 200 and r2.json():
        for ev in r2.json():
            # FOMC hike = risk-off; FOMC cut/hold is NOT risk-off
            if ev.get("event_type") == "fomc_decision":
                if ev.get("event_subtype") == "hike":
                    return True
                continue
            return True
    return False


def cluster_passes(events: list[dict]) -> tuple[bool, str]:
    """§15.3 cluster rule: ≥2 distinct source agents OR a single-source exception.

    Single-source exceptions cover events whose informational quality is
    high enough that two-source confirmation is unnecessary. The base set
    (SC 13D, sev≥3 8-K, sev≥4 earnings) covered SEC/exchange events. The
    domain-agent additions (biotech / defense / energy / activist) cover
    scheduled binary catalysts and academically-validated insider edges
    where a single primary source IS the truth. Audit 2026-05-18: every
    clinical_readout event arrives at sev=3, so without this rule the
    biotech catalyst path is silently dropped.
    """
    agents = {source_agent_for(e) for e in events}
    if len(agents) >= 2:
        return True, f"cluster:{','.join(sorted(agents))}"
    # Single-source exceptions
    for e in events:
        et = e["event_type"]
        sev = e.get("severity") or 0
        # SEC / exchange events
        if et == "filing_13d" or e.get("event_subtype") == "13D":
            return True, "exception:sc_13d"
        if et == "8k_material_event" and sev >= 3:
            return True, "exception:8k_sev3"
        if et == "earnings_release" and sev >= 4:
            return True, "exception:earnings_sev4"
        # Biotech binary catalysts
        if et == "fda_pdufa_decision":
            return True, "exception:fda_pdufa"
        if et == "clinical_readout" and sev >= 3:
            return True, "exception:clinical_sev3"
        # Defense / energy regulatory catalysts
        if et == "dod_contract_award" and sev >= 3:
            return True, "exception:dod_sev3"
        if et == "nuclear_license_approval" and sev >= 3:
            return True, "exception:nuclear_sev3"
        # Insider buying — academically-validated edge (Cohen/Lou 2012,
        # Jeng/Metrick/Zeckhauser 2003): cluster buys with own cash, esp.
        # in small caps with high information asymmetry, generate
        # statistically significant abnormal returns. Insider SELLS are
        # excluded by event_type (activist_insider_agent emits buys only).
        if et == "insider_cluster_buy":
            return True, "exception:insider_cluster_buy"
    return False, "single_source_no_exception"


def signal_direction(events: list[dict]) -> str:
    """Compute net direction of a cluster (bullish / bearish / neutral).

    filing_dilution is emitted alongside its parent 8k_material_event (same
    accession_number) — see filing_agent. Pre-fix this caused a tie (1 bull
    + 1 bear → fell through to bull > 0 → returned "bullish"), routing
    clearly-bearish PIPE/ATM filings to WATCH. Fix: when a dilution event is
    present, suppress the bull contribution from its matching 8-K so the
    bearish read dominates. For direct dilution events with no parent 8-K
    (no accession match), weight filing_dilution heavier (the +12 score in
    score_evidence already reflects the higher conviction; mirror it here).
    """
    dilution_accessions = {
        (e.get("payload") or {}).get("accession_number")
        for e in events
        if e["event_type"] == "filing_dilution"
    }
    dilution_accessions.discard(None)

    bull, bear = 0, 0
    for e in events:
        et = e["event_type"]
        d  = (e.get("payload") or {}).get("direction_prior", "neutral")
        if et == "truth_social_post":
            if d == "short": bear += 1
            elif d == "long": bull += 1
        elif et == "8k_material_event":
            # If a matching filing_dilution is in the cluster, this 8-K is
            # the dilution itself — don't count it bullish.
            acc = (e.get("payload") or {}).get("accession_number")
            if acc not in dilution_accessions:
                bull += 1
        elif et == "filing_13d":
            bull += 1
        elif et == "filing_dilution":
            # Heavier when no parent 8-K is in the cluster (direct dilution).
            # When there IS a parent 8-K, +1 is enough — the suppression above
            # already prevents the cancel-out.
            acc = (e.get("payload") or {}).get("accession_number")
            if acc and any(
                ee["event_type"] == "8k_material_event"
                and (ee.get("payload") or {}).get("accession_number") == acc
                for ee in events
            ):
                bear += 1
            else:
                bear += 2
        elif et in ("filing_s-3", "filing_s-3/a"):
            bear += 1
        elif et == "news_article":
            if d == "short": bear += 1
            elif d == "long": bull += 1
        elif et == "earnings_release":
            sub = e.get("event_subtype") or ""
            if sub == "miss": bear += 1
            elif sub == "beat": bull += 1
        elif et == "momentum":
            try:
                rs = float((e.get("payload") or {}).get("rel_strength_pct") or 0)
            except (TypeError, ValueError):
                rs = 0.0
            if rs < -5: bear += 1
            elif rs > 5: bull += 1
        elif et in ("institutional_new_position", "institutional_increase", "activist_5pct_crossed"):
            bull += 1
        elif et in ("institutional_exit", "institutional_decrease"):
            bear += 1
        elif et == "crypto_macro_move":
            if d == "short": bear += 1
            elif d == "long": bull += 1
    if bear > bull:  return "bearish"
    if bull > 0:     return "bullish"
    return "neutral"


def fetch_recent_news(ticker: str, hours: int = 48, limit: int = 5) -> list[dict]:
    """Pull last-N-hours `stock_raw_news` rows for a ticker.

    Catches the race window where news_agent has ingested an article into
    `stock_raw_news` but hasn't yet emitted a normalized event for it
    (news_agent runs every 5 min; if thesis_agent fires in the 0-5 min
    gap after publication, the article exists in raw_news but not yet in
    normalized_events). Also catches articles news_agent's classifier
    missed entirely.

    Filters by `ticker IS NOT NULL` — only rows where the classifier
    populated the ticker are useful. The PR1A precursor expanded the
    classifier from 22→265 aliases + 30→144 symbols, so most active
    watchlist names should now have ticker populated.
    """
    if not ticker:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/stock_raw_news",
            headers=HEADERS_SB,
            params={
                "ticker":       f"eq.{ticker}",
                "published_at": f"gte.{cutoff}",
                "select":       "headline,url,source,published_at",
                "order":        "published_at.desc",
                "limit":        str(limit),
            },
            timeout=10,
        )
        if r.status_code != 200:
            return []
        return r.json() or []
    except Exception:  # noqa: BLE001
        return []


def decompose_score(breakdown: list[dict]) -> dict:
    """Sum breakdown points by evidence role.

    Returns {catalyst, context, background, bonus, total_alert} where
    total_alert = catalyst + context + bonus (background EXCLUDED). The
    background sum is reported for display only — a 13F filing can never
    push a signal over the alert threshold by itself.
    """
    sums = {"catalyst": 0.0, "context": 0.0, "background": 0.0, "bonus": 0.0}
    for b in breakdown:
        role = b.get("role") or "context"
        if role == "catalyst":
            # Only count points if the event was within its catalyst_age_hours window
            if b.get("catalyst_ok"):
                sums["catalyst"] += b.get("points", 0)
            else:
                # Catalyst-eligible event TYPE but stale → demote to context
                sums["context"] += b.get("points", 0)
        elif role == "background":
            sums["background"] += b.get("raw_points") or b.get("points", 0)
        else:  # context, bonus, or unknown
            sums[role if role in sums else "context"] += b.get("points", 0)
    sums["total_alert"] = sums["catalyst"] + sums["context"] + sums["bonus"]
    return sums


def action_for(score: float, direction: str, has_mature_rule: bool = False,
                risk_off: bool = False, catalyst_score: float = 0.0) -> str:
    """Map (score, direction, maturity, macro, catalyst_score) to action vocabulary.

    Maturity gate: when the cluster includes ≥1 rule whose paper-trade accuracy
    has crossed 0.90 with n≥30 (per stock_rule_calibration.is_mature), the
    bot is allowed to use BUY / SELL — the system has earned the directional
    vocabulary on that rule.

    Catalyst gate (added 2026-05-22 per causal-attribution audit): when
    catalyst_score == 0, the bullish tiers degrade from CATALYST_WATCH /
    CATALYST_RESEARCH to MOMENTUM_ONLY — the bot will admit "price/volume
    moved but no verified catalyst found in last 48h" instead of citing
    stale 13F filings as causal. AVOID_CHASE / CHASE_RISK / BUY / SELL
    keep their original semantics since they describe risk warnings or
    matured-rule actions, not catalyst-attribution claims.

    Risk-off (VIX > 25): bullish thresholds shift up by 10 points.
    """
    bull_buy   = 70 + (10 if risk_off else 0)
    bull_watch = 70 + (10 if risk_off else 0)
    # Recall floor (RESEARCH tier) — temporary stopgap, see THESIS_RECALL_FLOOR.
    bull_res   = THESIS_RECALL_FLOOR + (10 if risk_off else 0)

    # Maturity gate unchanged
    if has_mature_rule:
        if direction == "bearish" and score >= 50:
            return "SELL"
        if direction == "bullish" and score >= bull_buy:
            return "BUY"

    # Bearish AVOID_CHASE unchanged — it's a risk warning, not a catalyst claim
    if direction == "bearish" and score >= 50:
        return "AVOID_CHASE"

    has_catalyst = catalyst_score > 0
    if direction == "bullish" and score >= bull_watch:
        return "CATALYST_WATCH" if has_catalyst else "MOMENTUM_ONLY"
    if direction == "bullish" and score >= bull_res:
        return "CATALYST_RESEARCH" if has_catalyst else "MOMENTUM_ONLY"

    # Non-bullish neutral high-score case (e.g., earnings_release uncertain direction)
    if score >= 70:
        return "CATALYST_WATCH" if has_catalyst else "MOMENTUM_ONLY"
    if score >= THESIS_RECALL_FLOOR:   # recall floor stopgap (was 50)
        return "CATALYST_RESEARCH" if has_catalyst else "MOMENTUM_ONLY"
    return ""  # suppress


def _record_rejected_clusters(thesis_run_id: int | None, scored: list[dict]) -> None:
    """Audit clusters dropped by the candidate filter. See stock_thesis_rejections."""
    rows = []
    for s in scored:
        cluster_ok = s.get("cluster_ok", False)
        action = s.get("action") or ""
        # Skip the happy-path clusters — those become signals, not rejections.
        if cluster_ok and action:
            continue
        # Classify the binding gate. cluster_passes is the first checkpoint;
        # if it failed, that's the reason. If it passed but action is empty,
        # the score gate is the reason.
        if not cluster_ok:
            fail_reason = "cluster_passes"
        else:
            fail_reason = "action_empty_low_score"
        # Compact breakdown sample — top 3 rules by abs(points) for context.
        breakdown = s.get("breakdown") or []
        try:
            top3 = sorted(
                (b for b in breakdown if isinstance(b, dict) and b.get("rule")),
                key=lambda b: abs(float(b.get("points") or 0)),
                reverse=True,
            )[:3]
            breakdown_sample = [
                {"rule": b.get("rule"), "points": b.get("points"),
                 "role": b.get("role"), "event_id": b.get("event_id")}
                for b in top3
            ]
        except Exception:
            breakdown_sample = []
        # Source-agent set from the events in the cluster.
        events = s.get("events") or []
        try:
            source_agents = sorted({source_agent_for(e) for e in events
                                    if source_agent_for(e)})
        except Exception:
            source_agents = []
        rows.append({
            "thesis_run_id":     thesis_run_id,
            "cluster_ticker":    s.get("ticker"),
            "cluster_bucket":    s.get("bucket"),
            "n_events":          len(events),
            "source_agents":     source_agents,
            "direction":         s.get("direction"),
            "score":             float(s.get("score") or 0),
            "catalyst_score":    float(s.get("catalyst_score") or 0),
            "context_score":     float(s.get("context_score") or 0),
            "background_score":  float(s.get("background_score") or 0),
            "fail_reason":       fail_reason,
            "cluster_label":     s.get("cluster_label"),
            "action":            action,
            "breakdown_sample":  breakdown_sample,
        })
    if not rows:
        return
    # Chunked POST — keep payload small even when a market-open run has
    # hundreds of clusters.
    chunk = 100
    for i in range(0, len(rows), chunk):
        try:
            requests.post(
                f"{SUPABASE_URL}/rest/v1/stock_thesis_rejections",
                headers={**HEADERS_SB, "Prefer": "return=minimal"},
                json=rows[i:i + chunk],
                timeout=20,
            )
        except Exception as e:  # noqa: BLE001
            print(f"  rejection batch {i}: {e}", file=sys.stderr)


def fetch_rule_calibration() -> dict[str, dict]:
    """{rule_key → {accuracy, n_observations, is_mature}} from stock_rule_calibration.
    Empty dict on any failure → callers fall through to base scoring (no learned weight)."""
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/stock_rule_calibration",
            headers=HEADERS_SB,
            params={"select": "rule_key,accuracy,n_observations,is_mature", "limit": "500"},
            timeout=10,
        )
        if r.status_code != 200:
            return {}
        return {row["rule_key"]: row for row in r.json() if row.get("rule_key")}
    except Exception as e:  # noqa: BLE001
        print(f"  fetch_rule_calibration failed (using empty): {e}", file=sys.stderr)
        return {}


def _sector_mult_enabled() -> bool:
    """Feature flag for sector-aware calibration multiplier (default OFF)."""
    return os.environ.get("SECTOR_CALIB_MULT_ENABLED", "").lower() in ("1", "true", "yes")


def _cluster_score_override_enabled() -> bool:
    """Feature flag for the score-based cluster_passes override (default OFF).

    When ON, single-source clusters whose computed score crosses
    CLUSTER_SCORE_OVERRIDE_THRESHOLD get cluster_ok=True, bypassing the
    source-count heuristic for proven-conviction clusters. See the rejection
    audit (stock_thesis_rejections) for the data-driven rationale."""
    return os.environ.get("CLUSTER_SCORE_OVERRIDE_ENABLED", "").lower() in ("1", "true", "yes")


def _structural_flip_enabled() -> bool:
    """Feature flag for the rule-key-based direction flip (default OFF).

    Five rules currently in STRUCTURAL_FLIP — each backed by n>=30 evidence
    of negative edge in their current direction. When ON, signals dominated
    by these rule_keys emit the OPPOSITE direction."""
    return os.environ.get("STRUCTURAL_FLIP_ENABLED", "").lower() in ("1", "true", "yes")


def apply_structural_flip(events: list[dict], direction: str,
                          breakdown: list[dict]) -> str:
    """If the cluster's live-horizon rule_keys are dominated by STRUCTURAL_FLIP
    rules, invert the direction. Adds a breakdown entry for traceability.

    Dominance rule: >=50% of the cluster's events have a rule_key (computed
    at h1d, the live emit horizon) in STRUCTURAL_FLIP. Below 50% we don't
    flip — a single flipped event in a multi-source cluster shouldn't override
    the rest.

    Returns the (possibly flipped) direction."""
    if not _structural_flip_enabled() or not STRUCTURAL_FLIP:
        return direction
    if direction not in ("bullish", "bearish"):
        # neutral, mixed, etc. — flip is undefined
        return direction
    flipped = []
    for e in events:
        et = e.get("event_type")
        if not et:
            continue
        rk = _rule_key.derive(et, e.get("event_subtype"), 1)
        if rk in STRUCTURAL_FLIP:
            flipped.append(rk)
    if not flipped or len(flipped) * 2 < len(events):
        return direction
    new_direction = "bearish" if direction == "bullish" else "bullish"
    breakdown.append({
        "rule":       "structural_flip_applied",
        "points":     0,
        "raw_points": 0,
        "event_id":   None,
        "detail":     f"{direction}→{new_direction} based on {len(flipped)}/{len(events)} "
                      f"flipped rule_keys: {sorted(set(flipped))[:3]}",
        "role":       "bonus",
        "catalyst_ok": True,
    })
    return new_direction


def fetch_sector_multipliers() -> dict[tuple[str, str], float]:
    """{(rule_key, sector) → multiplier} from stock_rule_sector_multiplier view.

    The view enforces n>=30 per cell and bounds multiplier to [0.5, 1.3]. Returns
    empty dict when the feature flag is off or the fetch fails — score_evidence
    treats missing cells as multiplier=1.0, so disabling the flag fully bypasses
    this without code changes.
    """
    if not _sector_mult_enabled():
        return {}
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/stock_rule_sector_multiplier",
            headers=HEADERS_SB,
            # Skip rows where multiplier=1.0 — they're no-ops and we want a small payload.
            params={"select": "rule_key,sector,multiplier", "multiplier": "neq.1.0", "limit": "2000"},
            timeout=10,
        )
        if r.status_code != 200:
            print(f"  fetch_sector_multipliers HTTP {r.status_code} — disabling for this run",
                  file=sys.stderr)
            return {}
        out: dict[tuple[str, str], float] = {}
        for row in r.json():
            rk = row.get("rule_key")
            sec = row.get("sector")
            m = row.get("multiplier")
            if rk and sec and m is not None:
                out[(rk, sec)] = float(m)
        return out
    except Exception as e:  # noqa: BLE001
        print(f"  fetch_sector_multipliers failed (using empty): {e}", file=sys.stderr)
        return {}


def fetch_ticker_sectors() -> dict[str, str]:
    """{ticker → sector} from stock_symbols. Empty dict when feature flag is off."""
    if not _sector_mult_enabled():
        return {}
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/stock_symbols",
            headers=HEADERS_SB,
            params={"select": "ticker,sector", "limit": "2000"},
            timeout=10,
        )
        if r.status_code != 200:
            return {}
        return {row["ticker"]: (row.get("sector") or "Unknown")
                for row in r.json() if row.get("ticker")}
    except Exception as e:  # noqa: BLE001
        print(f"  fetch_ticker_sectors failed (using empty): {e}", file=sys.stderr)
        return {}


def cluster_has_mature_rule(events: list[dict], calibration: dict[str, dict]) -> bool:
    """True if any event in the cluster maps to an is_mature rule_key.

    Uses the canonical _rule_key.derive — same format event_paper_agent writes
    to stock_rule_calibration. The legacy multi-candidate fallback (which tried
    subtype-less and unsuffixed variants) was a workaround for the inconsistent
    keying that A2 eliminated.
    """
    HORIZONS = (1, 7, 15, 30)
    for e in events:
        et = e["event_type"]
        sub = e.get("event_subtype")
        for h in HORIZONS:
            key = _rule_key.derive(et, sub, h)
            if calibration.get(key, {}).get("is_mature"):
                return True
    return False


def horizon_for(events: list[dict]) -> str:
    # Daily closes are the only audited live price source in v1, so every live
    # signal uses a 1d paper-trading horizon until intraday prices are added.
    if any(e["event_type"].startswith("filing_") or e["event_type"] == "8k_material_event" for e in events):
        return "1d"
    return "1d"


def evidence_summary(events: list[dict]) -> str:
    """≤80 char human-readable summary."""
    by_type = defaultdict(int)
    sample_subtype = ""
    for e in events:
        by_type[e["event_type"]] += 1
        sample_subtype = sample_subtype or (e.get("event_subtype") or "")
    parts = []
    if by_type.get("8k_material_event"):
        parts.append(f"new 8-K{' '+sample_subtype if sample_subtype else ''}")
    if by_type.get("truth_social_post"):
        parts.append("Trump post")
    if by_type.get("news_article"):
        parts.append(f"news ({by_type['news_article']})")
    if by_type.get("earnings_release"):
        parts.append("earnings")
    if by_type.get("momentum"):
        parts.append("momentum")
    if by_type.get("filing_4"):
        parts.append(f"{by_type['filing_4']}× Form 4")
    for f in ("filing_13d", "filing_13g"):
        if by_type.get(f):
            parts.append(f.split('_')[1].upper())
    summary = "; ".join(parts) or "evidence cluster"
    return summary[:80]


# ============================================================
# Signal write + dispatch
# ============================================================

def write_signal_evidence(signal_id: int, events: list[dict]) -> None:
    """Upsert signal-to-event evidence so reruns can heal partial writes."""
    ev_rows = [{
        "signal_id": signal_id,
        "agent":     source_agent_for(e),
        "event_id":  e["id"],
        "strength":  1.0,
        "detail":    f"{e['event_type']}{':'+e.get('event_subtype') if e.get('event_subtype') else ''}",
    } for e in events]
    if ev_rows:
        requests.post(
            f"{SUPABASE_URL}/rest/v1/stock_signal_evidence?on_conflict=signal_id,event_id",
            headers={**HEADERS_SB, "Prefer": "resolution=merge-duplicates,return=minimal"},
            json=ev_rows,
            timeout=15,
        )


def decompose_breakdown(breakdown: list[dict], *, risk_off: bool = False,
                         has_mature_rule: bool = False) -> dict:
    """Group breakdown items into named explainability channels.

    The flat `items` list is preserved for backward compatibility (existing
    dashboard rendering reads it). Named channels make it easy for the
    digest routine, future risk_agent, and human reviewers to see WHY a
    score reached its final value rather than parsing a 10-item flat list.
    """
    base = 0.0
    weight_mod = 0.0
    sector_b = 0.0
    hyper_b = 0.0
    power_b = 0.0
    for item in breakdown:
        rule = item.get("rule") or ""
        try:
            pts = float(item.get("points") or 0)
        except (TypeError, ValueError):
            pts = 0.0
        if rule == "sector_cluster_bonus":
            sector_b += pts
        elif rule == "hyperscaler_capex_echo":
            hyper_b += pts
        elif rule == "power_scarcity_boost":
            power_b += pts
        elif rule.startswith("weight_adj_"):
            weight_mod += pts
        else:
            base += pts
    intel_bonus = sector_b + hyper_b + power_b
    return {
        "items":                  breakdown,
        "base_event_score":       round(base, 2),
        "agent_weight_modifier":  round(weight_mod, 2),
        "sector_bonus":           round(sector_b, 2),
        "hyperscaler_bonus":      round(hyper_b, 2),
        "power_bonus":            round(power_b, 2),
        "intelligence_bonus_total": round(intel_bonus, 2),
        "regime_active":          bool(risk_off),
        "has_mature_rule":        bool(has_mature_rule),
    }


def compute_valid_until(events: list[dict], fired_at: datetime) -> str:
    """Alpha-decay TTL: fired_at + max(SIGNAL_TTL_HOURS) across cluster events.

    Anchored on fired_at (not event_at) so signals fired today off old events
    still get the full decay window. Returns ISO-8601 UTC string.
    """
    from datetime import timedelta as _td
    ttls = [SIGNAL_TTL_HOURS.get(e.get("event_type") or "", DEFAULT_SIGNAL_TTL_HOURS)
            for e in events]
    hours = max(ttls) if ttls else DEFAULT_SIGNAL_TTL_HOURS
    return (fired_at + _td(hours=hours)).isoformat()


def write_signal(ticker: str, score: float, action: str, direction: str,
                 breakdown: list[dict], events: list[dict], dedupe_key: str,
                 agent_weights: dict[str, float] | None = None,
                 fallback_action: str | None = None,
                 risk_off: bool = False,
                 has_mature_rule: bool = False) -> int | None:
    weights = agent_weights or {}
    cluster_agents = list({source_agent_for(e) for e in events})
    event_types = sorted({e.get("event_type") for e in events if e.get("event_type")})
    # Primary event_type for downstream calibration lookup is the
    # alphabetically-first (matches derive_primary_event_type in
    # trade_setup_agent). Pull the subtype of the first event of that type so
    # trade_setup_agent can compute the exact rule_key event_paper_agent wrote.
    primary_et = event_types[0] if event_types else None
    primary_subtype = next(
        ((e.get("event_subtype") or "").strip()
         for e in events if e.get("event_type") == primary_et),
        "",
    )
    fired_at = datetime.now(timezone.utc)
    payload = {
        "ticker":           ticker,
        "fired_at":         fired_at.isoformat(),
        "valid_until":      compute_valid_until(events, fired_at),
        "direction":        direction,
        "confidence":       round(min(max(score, 0), 100) / 100, 4),
        "horizon_days":     1 if horizon_for(events) == "1d" else 0,
        "thesis_summary":   evidence_summary(events),
        "model_version":    MODEL_VERSION,
        # Snapshot the weights actually used for this signal — price_agent reads
        # this to attribute outcomes back to the contributing agents at the
        # weight that was in effect at fire time, not whatever it is later.
        "weight_at_time":   {
            "agents":  cluster_agents,
            "weights": {a: round(weights.get(a, 1.0), 4) for a in cluster_agents},
            "primary_event_types": event_types,
            # Subtype of the first event of the primary event_type — required for
            # trade_setup_agent to match the rule_key event_paper_agent writes.
            "primary_event_subtype": primary_subtype,
        },
        "status":           "open",
        "action":           action,
        "score":            round(min(max(score, 0), 100), 2),
        "score_breakdown":  decompose_breakdown(breakdown, risk_off=risk_off,
                                                has_mature_rule=has_mature_rule),
        "evidence_summary": evidence_summary(events),
        "dedupe_key":       dedupe_key,
        "status_v2":        "candidate",
    }
    headers = {**HEADERS_SB, "Prefer": "return=representation"}
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/stock_signals",
        headers=headers, json=payload, timeout=15,
    )
    if r.status_code not in (200, 201) or not r.json():
        if action == "CHASE_RISK" and fallback_action in ("WATCH", "RESEARCH"):
            payload["action"] = fallback_action
            wt = payload["weight_at_time"]
            if isinstance(wt, dict):
                wt["display_action"] = "CHASE_RISK"
                wt["schema_fallback_action"] = fallback_action
            r = requests.post(
                f"{SUPABASE_URL}/rest/v1/stock_signals",
                headers=headers, json=payload, timeout=15,
            )
            if r.status_code in (200, 201) and r.json():
                print("  CHASE_RISK action rejected by DB; stored fallback action until sql/0007 is applied",
                      file=sys.stderr)
            else:
                print(f"  signal insert {r.status_code}: {r.text}", file=sys.stderr)
                return None
        else:
            print(f"  signal insert {r.status_code}: {r.text}", file=sys.stderr)
            return None
    if r.status_code not in (200, 201) or not r.json():
        print(f"  signal insert {r.status_code}: {r.text}", file=sys.stderr)
        return None
    sig = r.json()[0]
    sig_id = sig["id"]
    write_signal_evidence(sig_id, events)
    return sig_id


def mark_signal_status(signal_id: int, status_v2: str) -> None:
    requests.patch(
        f"{SUPABASE_URL}/rest/v1/stock_signals?id=eq.{signal_id}",
        headers=HEADERS_SB,
        json={"status_v2": status_v2}, timeout=10,
    )


def retry_dispatch_failed(cap_remaining: int) -> int:
    """Retry previously inserted signals whose Telegram dispatch failed."""
    if cap_remaining <= 0:
        return 0
    rows = requests.get(
        f"{SUPABASE_URL}/rest/v1/stock_signals",
        headers=HEADERS_SB,
        params={
            "status_v2": "eq.dispatch_failed",
            "action":    "in.(WATCH,AVOID_CHASE)",
            "select":    "id,ticker,score,action",
            "order":     "fired_at.asc",
            "limit":     str(cap_remaining),
        },
        timeout=15,
    )
    if rows.status_code != 200 or not rows.json():
        return 0
    from telegram_dispatcher import dispatch_signal
    sent = 0
    for sig in rows.json():
        sig_id = int(sig["id"])
        ok = dispatch_signal(sig_id)
        if ok:
            mark_signal_status(sig_id, "sent")
            sent += 1
            print(f"  {sig.get('ticker')}: RETRY SENT (sig_id={sig_id})")
        else:
            print(f"  {sig.get('ticker')}: retry dispatch failed again (sig_id={sig_id})")
    return sent


# ============================================================
# Main
# ============================================================

def main() -> int:
    started = time.time()
    run_id = job_run_start("thesis_agent")
    sent = 0
    suppressed = 0

    try:
        # Per-lane cap: count only signals this agent emits (model_version=MODEL_VERSION).
        # Intraday-spike alerts have their own per-run safety cap and were silently
        # eating thesis's daily budget under the old unscoped query.
        already_today = alerts_sent_today(model_version=MODEL_VERSION)
        cap_remaining = max(0, MAX_ALERTS_PER_DAY - already_today)
        retried = retry_dispatch_failed(cap_remaining)
        sent += retried
        cap_remaining = max(0, cap_remaining - retried)
        if retried:
            print(f"Retried dispatch_failed signals: {retried} sent, cap remaining: {cap_remaining}")

        events = fetch_fresh_events()
        print(f"Fresh events in last {FRESHNESS_WINDOW_MIN}m: {len(events)}")
        if not events:
            job_run_finish(run_id, "ok", 0, sent)
            return 0

        # Learning loop: pull current per-agent weights so well-performing agents
        # get amplified and chronically-wrong ones get dampened. Empty dict on
        # cold start is fine — score_evidence treats missing as weight=1.0.
        agent_weights = fetch_latest_agent_weights()
        if agent_weights:
            print("Agent weights in effect: " +
                  ", ".join(f"{a}={w:.2f}" for a, w in sorted(agent_weights.items())))
        else:
            print("No agent_weights yet — using default 1.0 for all (cold start)")

        # Phase 7 — per-rule calibration. Maturity gate (≥0.90 accuracy with n≥30
        # closed paper trades) unlocks BUY/SELL action vocabulary for any cluster
        # that contains at least one mature rule.
        rule_calibration = fetch_rule_calibration()
        mature_keys = [k for k, v in rule_calibration.items() if v.get("is_mature")]
        if mature_keys:
            print(f"Mature rules ({len(mature_keys)}): {sorted(mature_keys)[:6]}{'...' if len(mature_keys) > 6 else ''}")
        else:
            print("No mature rules yet — BUY/SELL gated; staying paper-only")

        # Sector-aware calibration multiplier (feature-flagged via
        # SECTOR_CALIB_MULT_ENABLED). When OFF, both fetches return {} and
        # score_evidence treats every (rule_key, sector) cell as 1.0 — zero
        # behavior delta. When ON, the stock_rule_sector_multiplier view
        # supplies per-cell scaling derived from closed paper trades.
        sector_multipliers = fetch_sector_multipliers()
        ticker_sectors = fetch_ticker_sectors()
        if sector_multipliers:
            amp_count = sum(1 for m in sector_multipliers.values() if m > 1.0)
            damp_count = sum(1 for m in sector_multipliers.values() if m < 1.0)
            print(f"Sector calib multipliers: {len(sector_multipliers)} cells active "
                  f"({amp_count} amplify, {damp_count} dampen)")
        elif _sector_mult_enabled():
            print("Sector calib multipliers: enabled but view returned 0 rows")

        # Group by (ticker, 5-min bucket)
        clusters: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for e in events:
            try:
                t = datetime.fromisoformat(e["event_at"].replace("Z", "+00:00"))
            except Exception:
                continue
            bucket = t.replace(second=0, microsecond=0)
            bucket = bucket.replace(minute=(bucket.minute // CLUSTER_WINDOW_MIN) * CLUSTER_WINDOW_MIN)
            clusters[(e["ticker"], bucket.isoformat())].append(e)

        print(f"Distinct (ticker, 5-min) clusters: {len(clusters)}")

        print(f"Alerts already sent today: {already_today} (cap remaining after retries: {cap_remaining})")

        # Intelligence layer — cross-rule context for sector/hyperscaler/power signals
        watchlist_map = fetch_watchlist_map()
        wide_events = fetch_recent_events_window(hours=POWER_SCARCITY_LOOKBACK_HOURS)
        risk_off = is_risk_off()
        if risk_off:
            print("⚠️  Macro risk-off active (VIX > 25) — bullish thresholds tightened +10")

        # Score and rank
        scored = []
        for (ticker, bucket), ev_list in clusters.items():
            ok, cluster_label = cluster_passes(ev_list)
            score, breakdown = score_evidence(
                ev_list,
                agent_weights=agent_weights,
                sector_multipliers=sector_multipliers,
                ticker_sectors=ticker_sectors,
            )
            direction = signal_direction(ev_list)
            # Structural flip: if STRUCTURAL_FLIP_ENABLED and >=50% of the
            # cluster's events are in the flip set, invert the direction. The
            # flip is reflected in the breakdown so downstream audits can see
            # which signals were flipped and why.
            direction = apply_structural_flip(ev_list, direction, breakdown)

            # Apply intelligence bonuses
            sb, sb_detail = sector_cluster_bonus(ticker, direction, wide_events, watchlist_map)
            if sb:
                score += sb
                breakdown.append({"rule": "sector_cluster_bonus", "points": sb,
                                  "event_id": None, "detail": sb_detail})
            hb, hb_detail = hyperscaler_capex_echo(ticker, wide_events)
            if hb:
                score += hb
                breakdown.append({"rule": "hyperscaler_capex_echo", "points": hb,
                                  "event_id": None, "detail": hb_detail})
            pb, pb_detail = power_scarcity_active(ticker, wide_events, watchlist_map)
            if pb:
                score += pb
                breakdown.append({"rule": "power_scarcity_boost", "points": pb,
                                  "event_id": None, "detail": pb_detail})

            mature = cluster_has_mature_rule(ev_list, rule_calibration)
            sub_scores = decompose_score(breakdown)

            # PR1B: race-window / classifier-gap safety net. If the normalized
            # events for this cluster don't yield catalyst_score>0, fetch
            # last-48h raw_news for the ticker and apply the causal-keyword
            # classifier. A matched causal headline promotes the signal back
            # to CATALYST_* tier (operator sees the real cause); generic
            # headlines without catalyst keywords get attached as context but
            # do NOT promote — signal stays MOMENTUM_ONLY honestly.
            news_causal_promoted = False
            news_top_causal_headline: str | None = None
            recent_news: list[dict] = []
            if sub_scores["catalyst"] == 0:
                from _catalyst_policy import is_causal_headline
                recent_news = fetch_recent_news(ticker, hours=48, limit=5)
                for n in recent_news:
                    if is_causal_headline(n.get("headline") or ""):
                        news_causal_promoted = True
                        news_top_causal_headline = n.get("headline")
                        # Inject a synthetic catalyst entry into breakdown so
                        # decompose_score reflects it on the next call AND the
                        # operator can see the source attribution.
                        breakdown.append({
                            "rule":         "news_causal_promoted",
                            "points":       8,
                            "raw_points":   8,
                            "event_id":     None,
                            "detail":       f"raw_news 48h: {(news_top_causal_headline or '')[:120]}",
                            "role":         "catalyst",
                            "catalyst_ok":  True,
                        })
                        score += 8
                        sub_scores = decompose_score(breakdown)
                        break

            action = action_for(score, direction, has_mature_rule=mature,
                                risk_off=risk_off,
                                catalyst_score=sub_scores["catalyst"])

            # Score-based cluster_passes override.
            # When CLUSTER_SCORE_OVERRIDE_ENABLED, a cluster that failed the
            # source-count heuristic but whose computed score crosses the
            # rubric's own alert threshold gets promoted to ok=True. The rubric
            # is the authoritative judgment of "alert-worthy"; cluster_passes
            # is a pre-rubric coarse filter. When the rubric says yes (>=50)
            # and cluster_passes says no, defer to the rubric.
            # Note: this DOES NOT bypass maturity gating (BUY/SELL still
            # require an is_mature rule) or the daily cap. It only lifts the
            # cluster-source double-gate for high-conviction single-source
            # clusters that would otherwise be silently dropped.
            if (not ok
                and _cluster_score_override_enabled()
                and score >= CLUSTER_SCORE_OVERRIDE_THRESHOLD
                and action):
                breakdown.append({
                    "rule":       "cluster_passes_override",
                    "points":     0,           # informational; score already computed
                    "event_id":   None,
                    "detail":     f"score={round(score, 1)}>={CLUSTER_SCORE_OVERRIDE_THRESHOLD}; "
                                  f"original cluster_label={cluster_label}",
                    "role":       "bonus",
                    "catalyst_ok": True,
                })
                ok = True
                cluster_label = f"override:high_score_{int(score)}"

            scored.append({
                "ticker":   ticker,
                "bucket":   bucket,
                "events":   ev_list,
                "score":    score,
                "catalyst_score":   sub_scores["catalyst"],
                "context_score":    sub_scores["context"],
                "background_score": sub_scores["background"],
                "action":   action,
                "direction": direction,
                "cluster_ok": ok,
                "cluster_label": cluster_label,
                "breakdown": breakdown,
                "dedupe_key": f"thesis_{ticker}_{bucket}",
                "has_mature_rule": mature,
                "risk_off": risk_off,
                "recent_news":          recent_news,
                "news_causal_promoted": news_causal_promoted,
                "news_top_headline":    news_top_causal_headline,
            })

        # Audit rejected clusters BEFORE filtering — record one row per cluster
        # that gets dropped so we can measure which gate is binding. Three
        # buckets of rejection are interesting:
        #   * cluster_passes failure (cluster_ok=False) — cluster_label has the reason
        #   * action=="" (score<50 or non-bullish low-score) — score_too_low
        #   * cluster_ok but downgraded to MOMENTUM_ONLY because catalyst_score==0
        #     — these DO pass through (kept in candidates) but worth recording
        #     so we can quantify the catalyst-class gap separately.
        # Wrapped in try so a transient DB issue can't break thesis_agent itself.
        try:
            _record_rejected_clusters(run_id, scored)
        except Exception as _rej_exc:  # noqa: BLE001
            print(f"  rejection audit failed (non-fatal): {_rej_exc}", file=sys.stderr)

        # Filter: must pass cluster + have non-empty action
        candidates = [s for s in scored if s["cluster_ok"] and s["action"]]
        # Skip already-signaled buckets
        existing = already_signaled_dedupe_keys([c["dedupe_key"] for c in candidates])
        if existing:
            existing_ids = existing_signals_by_dedupe(list(existing))
            for c in candidates:
                sig_id = existing_ids.get(c["dedupe_key"])
                if sig_id is not None:
                    write_signal_evidence(sig_id, c["events"])
        candidates = [c for c in candidates if c["dedupe_key"] not in existing]

        # Chase-risk downgrade: WATCH/RESEARCH on a stock that has already moved
        # >5% in the cluster's bullish direction since the earliest event becomes
        # CHASE_RISK (suppressed from Telegram dispatch but still recorded).
        if candidates:
            tickers_to_check = list({c["ticker"] for c in candidates if c["direction"] == "bullish"})
            closes_map = fetch_recent_closes(tickers_to_check, days_back=7)
            for c in candidates:
                # Chase-risk downgrade applies to any bullish "actionable" alert tier
                # (catalyst-backed or momentum-only — both can chase the same bar).
                bullish_actionable = ("WATCH", "RESEARCH", "CATALYST_WATCH",
                                      "CATALYST_RESEARCH", "MOMENTUM_ONLY")
                if c["direction"] != "bullish" or c["action"] not in bullish_actionable:
                    continue
                earliest = min((e["event_at"] for e in c["events"] if e.get("event_at")),
                               default=None)
                if not earliest:
                    continue
                pct = chase_risk_pct(closes_map.get(c["ticker"], []), earliest)
                if pct is not None and pct >= CHASE_RISK_PCT:
                    print(f"  {c['ticker']}: chase risk — already +{pct*100:.1f}% since cluster start, downgrade {c['action']}→CHASE_RISK")
                    c["chase_pct"] = round(pct, 4)
                    c["original_action"] = c["action"]
                    c["action"] = "CHASE_RISK"
                    c["breakdown"].append({
                        "rule":     "chase_risk_downgrade",
                        "points":   0,
                        "event_id": None,
                        "detail":   f"+{pct*100:.1f}% since {earliest[:10]}, was {c['original_action']}",
                    })

        # Sort by score descending — top-k for governor
        candidates.sort(key=lambda x: x["score"], reverse=True)

        for cand in candidates:
            ticker = cand["ticker"]
            event_type = cand["events"][0]["event_type"]
            if recently_dispatched(ticker, event_type):
                print(f"  {ticker}: dedupe — recent dispatch, skip")
                continue
            sig_id = write_signal(
                ticker=ticker, score=cand["score"], action=cand["action"],
                direction=cand["direction"],
                breakdown=cand["breakdown"], events=cand["events"],
                dedupe_key=cand["dedupe_key"],
                agent_weights=agent_weights,
                fallback_action=cand.get("original_action"),
                risk_off=cand.get("risk_off", False),
                has_mature_rule=cand.get("has_mature_rule", False),
            )
            if sig_id is None:
                continue
            # Severity-4 events bypass the daily cap (LITE-style critical alerts).
            # RESEARCH-tier gets promoted to dispatch when a sev4 event is present.
            max_sev = max((e.get("severity") or 0) for e in cand["events"])
            priority = max_sev >= 4 and SEV4_PRIORITY_BYPASS_CAP
            # WATCH/RESEARCH retained for backward compat with any in-flight signals
            # still using the legacy vocabulary. New signals use CATALYST_* / MOMENTUM_ONLY.
            dispatchable_actions = ("WATCH", "AVOID_CHASE", "BUY", "SELL",
                                    "CATALYST_WATCH", "MOMENTUM_ONLY")
            if priority and cand["action"] in ("RESEARCH", "CATALYST_RESEARCH"):
                dispatchable_actions = dispatchable_actions + ("RESEARCH", "CATALYST_RESEARCH")

            if cand["action"] in dispatchable_actions and (cap_remaining > 0 or priority):
                from telegram_dispatcher import dispatch_signal
                ok = dispatch_signal(sig_id)
                if ok:
                    mark_signal_status(sig_id, "sent")
                    if not priority:
                        cap_remaining -= 1
                    sent += 1
                    tag = " [SEV4-PRIORITY]" if priority else ""
                    print(f"  {ticker}: SENT{tag} (score={cand['score']:.0f}, sig_id={sig_id})")
                else:
                    mark_signal_status(sig_id, "dispatch_failed")
                    print(f"  {ticker}: dispatch failed (sig_id={sig_id})")
            else:
                mark_signal_status(sig_id, "suppressed")
                suppressed += 1
                print(f"  {ticker}: suppressed action={cand['action']} score={cand['score']:.0f} cap_remaining={cap_remaining}")

        elapsed = time.time() - started
        print(f"Done in {elapsed:.1f}s. Sent: {sent}, suppressed: {suppressed}, candidates: {len(candidates)}")
        job_run_finish(run_id, "ok", len(events), sent + suppressed)
        return 0

    except Exception as e:  # noqa: BLE001
        import traceback
        tb = traceback.format_exc()
        try:
            requests.post(f"{SUPABASE_URL}/rest/v1/stock_dead_letter_events",
                          headers=HEADERS_SB,
                          json={"agent": "thesis_agent", "reason": "top_level_failure",
                                "detail": tb[:2000], "payload": {}}, timeout=10)
        except Exception:
            pass
        job_run_finish(run_id, "failed", 0, 0, err=str(e))
        print(f"FATAL: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
