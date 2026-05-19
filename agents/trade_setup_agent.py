"""
trade_setup_agent — Trade construction layer.

LAYER BOUNDARY:
  Input:  stock_signals (intelligence layer output)
  Output: stock_trade_setups (this layer's only write target)
  Reads:  stock_rule_calibration (for confidence + skip-decision inputs)
  Never:  writes to stock_signals or any layer above it.

This agent answers "how would you actually enter this?" — NOT "should I
trade it?" The risk_agent (downstream) makes the capital decision.

A setup is one of:
  - next_open       : open the position at next session's open
  - limit_pullback  : wait for a retrace before entering
  - breakout        : wait for confirmation above prior high (long) / below
                      prior low (short)
  - vwap_band       : enter at VWAP ± volatility band
  - manual_skip     : structurally not tradeable (illiquid, no entry plan)

A non-NULL reason_to_skip means the setup exists for audit/learning but
the risk_agent will skip it. Reasons include:
  - signal past valid_until
  - action == AVOID_CHASE / CHASE_RISK (intelligence already flagged)
  - no edge: profit_factor < 1.0 AND not training-mature
  - rule sample too small (<5 closed trades) AND not mature
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from typing import Optional

import requests

import _rule_key   # agents/ on sys.path at runtime; canonical rule_key

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

HEADERS_SB = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
}

# How far back to look for signals that haven't been processed yet. The
# unique(signal_id) constraint on stock_trade_setups makes the agent
# idempotent — re-running over the same window won't duplicate setups.
LOOKBACK_HOURS = 24

# Map event_type → preferred setup_type. Fast-decay sentiment opens at the
# bell; slow-burn position events wait for a pullback so we don't chase.
SETUP_TYPE_BY_EVENT: dict[str, str] = {
    "news_article":            "next_open",
    "truth_social_post":       "next_open",
    "momentum":                "breakout",
    "8k_material_event":       "next_open",
    "earnings_release":        "next_open",
    "clinical_readout":        "next_open",
    "fda_pdufa_decision":      "next_open",
    "nuclear_license_approval": "next_open",
    "dod_contract_award":      "next_open",
    "filing_13d":              "limit_pullback",
    "filing_13g":              "limit_pullback",
    "filing_4":                "limit_pullback",
    "institutional_buy":       "limit_pullback",
    "institutional_sell":      "limit_pullback",
    "activist_initial_position": "limit_pullback",
    "insider_cluster_buy":     "limit_pullback",
    "consumer_sentiment":      "vwap_band",
    "traffic_data":            "vwap_band",
    "vix_spike":               "next_open",
    "yield_milestone":         "next_open",
    "yield_snapshot":          "vwap_band",
    "fomc_decision":           "next_open",
    "cpi_release":             "next_open",
    "nfp_release":             "next_open",
    "crypto_macro_move":       "next_open",
}
DEFAULT_SETUP_TYPE = "next_open"


def sb_get(path: str, params: dict) -> list[dict]:
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{path}",
                     headers=HEADERS_SB, params=params, timeout=20)
    if r.status_code != 200:
        print(f"  GET {path} {r.status_code}: {r.text[:200]}", file=sys.stderr)
        return []
    return r.json()


def job_run_start() -> int | None:
    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/stock_job_runs",
            headers={**HEADERS_SB, "Prefer": "return=representation"},
            json={"agent": "trade_setup_agent"}, timeout=10,
        )
        if r.status_code in (200, 201) and r.json():
            return r.json()[0]["id"]
    except Exception as exc:
        print(f"  job_run_start failed: {exc}", file=sys.stderr)
    return None


def job_run_finish(run_id: int | None, status: str,
                   rows_in: int, rows_out: int, err: str | None = None) -> None:
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
    except Exception:
        pass


def fetch_recent_signals() -> list[dict]:
    from datetime import timedelta as _td
    cutoff = (datetime.now(timezone.utc) - _td(hours=LOOKBACK_HOURS)).isoformat()
    return sb_get("stock_signals", {
        "fired_at": f"gte.{cutoff}",
        "select": "id,ticker,direction,action,score,fired_at,valid_until,"
                  "horizon_days,score_breakdown,weight_at_time",
        "order":  "fired_at.desc",
        "limit":  "500",
    })


def fetch_existing_setup_signal_ids(signal_ids: list[int]) -> set[int]:
    if not signal_ids:
        return set()
    in_list = ",".join(str(i) for i in signal_ids)
    rows = sb_get("stock_trade_setups", {
        "signal_id": f"in.({in_list})",
        "select":    "signal_id",
    })
    return {r["signal_id"] for r in rows}


def fetch_calibration_for_rule_keys(rule_keys: list[str]) -> dict[str, dict]:
    if not rule_keys:
        return {}
    in_list = ",".join(f'"{k}"' for k in rule_keys)
    rows = sb_get("stock_rule_calibration", {
        "rule_key": f"in.({in_list})",
        "select":   "rule_key,n_observations,accuracy,profit_factor,"
                    "avg_win_pct,avg_loss_pct,is_mature",
    })
    return {r["rule_key"]: r for r in rows}


def derive_primary_event_type(signal: dict) -> Optional[str]:
    wt = signal.get("weight_at_time") or {}
    pet = wt.get("primary_event_types") or []
    return pet[0] if pet else None


def derive_primary_event_subtype(signal: dict) -> str | None:
    """Subtype set by thesis_agent at signal-fire time. Older signals (written
    before the subtype was persisted) return None and fall back to subtype-less
    rule_key lookup."""
    wt = signal.get("weight_at_time") or {}
    return wt.get("primary_event_subtype")


def derive_rule_key(signal: dict) -> Optional[str]:
    """Canonical rule_key for this signal — same format event_paper_agent writes
    to stock_rule_calibration, so the lookup actually finds the row.

    Signals carry horizon_days = 0 (multi-day) or 1 (next-day). Map 0 → 7 so the
    multi-day playbook anchors to the h7d calibration track (where most
    event-driven patterns play out).

    The explicit None check matters: `signal.get("horizon_days") or 1` would
    coerce 0 to 1 via short-circuit, which historically caused every signal to
    lookup h1d calibration regardless of intent — bug fixed alongside A2."""
    pet = derive_primary_event_type(signal)
    if not pet:
        return None
    sub = derive_primary_event_subtype(signal)
    raw_h = signal.get("horizon_days")
    horizon_int = 1 if raw_h is None else int(raw_h)
    h = 1 if horizon_int == 1 else 7
    return _rule_key.derive(pet, sub, h)


def _map_direction(raw: str | None) -> tuple[str, str | None]:
    """Map intelligence-layer direction (bullish/bearish/neutral) onto the
    trade-construction vocabulary (long/short). Returns (direction, skip_reason).
    Neutral signals get direction=long for column-constraint purposes but
    are tagged with a skip reason so the risk_agent ignores them.
    """
    d = (raw or "").lower()
    if d in ("long", "bullish"):
        return "long", None
    if d in ("short", "bearish"):
        return "short", None
    # neutral, empty, or unknown — record as long-default but mark for skip
    return "long", "neutral or unknown signal direction"


def compute_setup(signal: dict, cal: dict[str, dict]) -> dict:
    """Translate one signal into a trade_setup row.

    Always returns a row (we record every signal's setup decision for audit).
    The reason_to_skip field determines whether the risk_agent acts on it.
    """
    pet = derive_primary_event_type(signal)
    setup_type = SETUP_TYPE_BY_EVENT.get(pet or "", DEFAULT_SETUP_TYPE)
    rule_key = derive_rule_key(signal)
    rule_cal = cal.get(rule_key or "") or {}
    direction, dir_skip_reason = _map_direction(signal.get("direction"))

    accuracy = rule_cal.get("accuracy")
    profit_factor = rule_cal.get("profit_factor")
    n_obs = int(rule_cal.get("n_observations") or 0)
    is_mature = bool(rule_cal.get("is_mature"))

    # Confidence: blend of hit rate and payoff. profit_factor==None means
    # too few closed trades to compute payoff — fall back to accuracy alone.
    if profit_factor is not None and accuracy is not None:
        # Sigmoid-shaped blend: stronger payoff lifts confidence, capped at 1
        pf_factor = min(profit_factor / 2.0, 2.0)   # PF=2 → 1.0, PF=8 → 4.0 capped at 2
        confidence = min(float(accuracy) * pf_factor, 1.0)
    elif accuracy is not None:
        confidence = float(accuracy)
    else:
        confidence = 0.5  # no calibration yet

    # Skip reasons — populated in priority order. The first non-null reason
    # wins; subsequent checks are still informative for audit but don't
    # change the outcome.
    reason_to_skip: Optional[str] = None
    valid_until = signal.get("valid_until")
    if valid_until:
        try:
            vu = datetime.fromisoformat(valid_until.replace("Z", "+00:00"))
            if vu < datetime.now(timezone.utc):
                reason_to_skip = f"signal expired ({valid_until[:16]})"
        except Exception:
            pass

    action = signal.get("action") or ""
    if reason_to_skip is None and action in ("AVOID_CHASE", "CHASE_RISK"):
        reason_to_skip = f"intelligence flagged {action}"

    if reason_to_skip is None and dir_skip_reason is not None:
        reason_to_skip = dir_skip_reason

    if reason_to_skip is None and rule_key:
        if not is_mature and n_obs < 5:
            reason_to_skip = f"rule {rule_key} has only n={n_obs} closed trades (need ≥5 or mature)"
        elif not is_mature and profit_factor is not None and profit_factor < 1.0:
            reason_to_skip = f"rule {rule_key} profit_factor {profit_factor:.2f} < 1.0 (no payoff edge)"

    # Stop/target inherit thesis's horizon scaling. Without explicit
    # per-event tuning, mirror event_paper_agent's defaults so audits
    # line up against the same yardstick.
    target_pct = 0.05
    stop_pct = 0.03
    horizon = int(signal.get("horizon_days") or 1)

    return {
        "signal_id":       signal["id"],
        "ticker":          signal["ticker"],
        "direction":       direction,
        "setup_type":      setup_type,
        "entry_reference": f"{setup_type} from fired_at={signal.get('fired_at','')[:16]}",
        "entry_ref_price": None,    # populated later when we wire price snapshots
        "stop_pct":        round(stop_pct, 4),
        "target_pct":      round(target_pct, 4),
        "horizon_days":    horizon,
        "valid_until":     valid_until,
        "confidence":      round(confidence, 4),
        "reason_to_skip":  reason_to_skip,
        "rule_key":        rule_key,
    }


def write_setups(rows: list[dict]) -> int:
    if not rows:
        return 0
    written = 0
    # Chunked POST with on_conflict=signal_id so re-runs are idempotent.
    for i in range(0, len(rows), 100):
        chunk = rows[i:i + 100]
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/stock_trade_setups?on_conflict=signal_id",
            headers={**HEADERS_SB, "Prefer": "resolution=ignore-duplicates,return=minimal"},
            json=chunk, timeout=20,
        )
        if r.status_code in (200, 201, 204):
            written += len(chunk)
        else:
            print(f"  setup insert {r.status_code}: {r.text[:300]}", file=sys.stderr)
    return written


def main() -> int:
    run_id = job_run_start()
    rows_in = rows_out = 0
    try:
        signals = fetch_recent_signals()
        rows_in = len(signals)
        print(f"Fetched {rows_in} signals from last {LOOKBACK_HOURS}h")
        if not signals:
            job_run_finish(run_id, "ok", 0, 0)
            return 0

        existing = fetch_existing_setup_signal_ids([s["id"] for s in signals])
        signals = [s for s in signals if s["id"] not in existing]
        print(f"  {len(existing)} already have setups; {len(signals)} new to process")

        rule_keys = sorted({k for k in (derive_rule_key(s) for s in signals) if k})
        cal = fetch_calibration_for_rule_keys(rule_keys)
        print(f"  loaded calibration for {len(cal)} rules")

        setups = [compute_setup(s, cal) for s in signals]
        actionable = [s for s in setups if s["reason_to_skip"] is None]
        skipped = [s for s in setups if s["reason_to_skip"] is not None]
        print(f"  {len(actionable)} actionable setups, {len(skipped)} flagged to skip")
        if skipped:
            from collections import Counter
            reasons = Counter(s["reason_to_skip"][:50] for s in skipped)
            for reason, n in reasons.most_common(5):
                print(f"    skip: {n}× {reason}")

        rows_out = write_setups(setups)
        print(f"DONE — wrote {rows_out} trade setups")
        job_run_finish(run_id, "ok", rows_in, rows_out)
        return 0

    except Exception as exc:
        import traceback
        tb = traceback.format_exc()
        print(f"FATAL: {exc}\n{tb}", file=sys.stderr)
        job_run_finish(run_id, "failed", rows_in, rows_out, err=str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
