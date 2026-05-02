"""
Paper trade forecast agent — Phase 6A.

Converts live stock_signals into probability-calibrated paper forecasts.

This is deliberately not a BUY/SELL engine. It writes paper-only actions:
PAPER_LONG, PAPER_WATCH, PAPER_AVOID, PAPER_CHASE_RISK, or NO_TRADE.

Calibration is empirical and conservative:
  prob_win = (setup_wins + K * base_rate) / (setup_n + K)

Where:
  - base_rate is the historical audited win rate for the same direction/horizon.
  - setup_wins/setup_n come from similar audited backtest/live signals.
  - K shrinks small samples toward the base rate.

Trigger: .github/workflows/paper_trade_agent.yml
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import mean
from typing import Any

import requests

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

HEADERS_SB = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
    "Prefer":        "resolution=ignore-duplicates,return=minimal",
}

MODEL_VERSION = "paper-calibration-v1"
CALIBRATION_METHOD = "empirical_shrinkage_v1"
SHRINKAGE_K = 20
MIN_SETUP_N_FOR_LONG = 8
MIN_SETUP_N_FOR_WATCH = 4


@dataclass(frozen=True)
class SetupFeatures:
    action: str
    direction: str
    horizon_days: int
    score_bucket: str
    agents: tuple[str, ...]
    has_earnings: bool
    has_8k: bool
    has_form4: bool
    has_momentum: bool
    has_news: bool
    has_truth: bool
    has_dilution: bool

    def important_flags(self) -> set[str]:
        flags = set()
        for name in (
            "has_earnings", "has_8k", "has_form4", "has_momentum",
            "has_news", "has_truth", "has_dilution",
        ):
            if getattr(self, name):
                flags.add(name)
        return flags


@dataclass
class CalibrationRow:
    signal_id: int
    features: SetupFeatures
    correct: bool
    realized_return: float
    thesis_return: float


# ============================================================
# Supabase helpers
# ============================================================

def sb_get(path: str, params: dict[str, str] | None = None) -> tuple[int, list[dict]]:
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/{path}",
        headers=HEADERS_SB,
        params=params or {},
        timeout=30,
    )
    if r.status_code != 200:
        return r.status_code, []
    return r.status_code, r.json()


def sb_post(path: str, rows: list[dict], on_conflict: str | None = None,
            merge: bool = False) -> bool:
    if not rows:
        return True
    prefer = "resolution=merge-duplicates,return=minimal" if merge else "resolution=ignore-duplicates,return=minimal"
    headers = {**HEADERS_SB, "Prefer": prefer}
    suffix = f"?on_conflict={on_conflict}" if on_conflict else ""
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/{path}{suffix}",
        headers=headers,
        json=rows,
        timeout=30,
    )
    if r.status_code not in (200, 201, 204):
        print(f"  SB POST {path} {r.status_code}: {r.text[:300]}", file=sys.stderr)
        return False
    return True


def sb_patch(path: str, payload: dict) -> bool:
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/{path}",
        headers=HEADERS_SB,
        json=payload,
        timeout=20,
    )
    if r.status_code not in (200, 201, 204):
        print(f"  SB PATCH {path} {r.status_code}: {r.text[:300]}", file=sys.stderr)
        return False
    return True


def table_available(table: str) -> bool:
    status, _ = sb_get(table, {"select": "id", "limit": "1"})
    if status == 200:
        return True
    if status in (400, 404):
        print(f"{table} is not available yet. Apply sql/0008_paper_forecasts.sql, then rerun.")
        return False
    print(f"{table} availability check returned HTTP {status}", file=sys.stderr)
    return False


def job_run_start() -> int | None:
    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/stock_job_runs",
            headers={**HEADERS_SB, "Prefer": "return=representation"},
            json={"agent": "paper_trade_agent"},
            timeout=10,
        )
        if r.status_code in (200, 201) and r.json():
            return r.json()[0]["id"]
    except Exception as e:  # noqa: BLE001
        print(f"  job_run_start failed: {e}", file=sys.stderr)
    return None


def job_run_finish(run_id: int | None, status: str, rows_in: int, rows_out: int,
                   err: str | None = None) -> None:
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
            },
            timeout=10,
        )
    except Exception as e:  # noqa: BLE001
        print(f"  job_run_finish failed: {e}", file=sys.stderr)


# ============================================================
# Feature extraction
# ============================================================

def _score_bucket(score: Any) -> str:
    try:
        value = float(score or 0)
    except (TypeError, ValueError):
        value = 0.0
    lo = int(value // 10) * 10
    lo = max(0, min(90, lo))
    return f"{lo}-{lo + 10}"


def _agents_from_signal(signal: dict) -> tuple[str, ...]:
    wt = signal.get("weight_at_time") or {}
    agents = wt.get("agents", []) if isinstance(wt, dict) else []
    return tuple(sorted(str(a) for a in agents if a))


def _display_action(signal: dict) -> str:
    wt = signal.get("weight_at_time") or {}
    if isinstance(wt, dict) and wt.get("display_action"):
        return str(wt["display_action"])
    return str(signal.get("action") or "RESEARCH")


def features_for_signal(signal: dict) -> SetupFeatures:
    summary = str(signal.get("evidence_summary") or "").lower()
    items = []
    breakdown = signal.get("score_breakdown") or {}
    if isinstance(breakdown, dict) and isinstance(breakdown.get("items"), list):
        items = [str(x.get("rule") or "").lower() for x in breakdown["items"] if isinstance(x, dict)]
    rule_text = " ".join(items)
    text = f"{summary} {rule_text}"
    horizon = int(signal.get("horizon_days") or 1)
    return SetupFeatures(
        action=_display_action(signal),
        direction=str(signal.get("direction") or "neutral"),
        horizon_days=horizon,
        score_bucket=_score_bucket(signal.get("score")),
        agents=_agents_from_signal(signal),
        has_earnings=("earnings" in text),
        has_8k=("8-k" in text or "8k" in text or "new_8k" in text),
        has_form4=("form 4" in text or "filing_4" in text),
        has_momentum=("momentum" in text),
        has_news=("news" in text),
        has_truth=("truth" in text or "trump" in text),
        has_dilution=("dilution" in text or "s-3" in text or "s3" in text),
    )


def thesis_return(realized_return: float, features: SetupFeatures) -> float:
    """Positive means the paper thesis was favorable, regardless of long/avoid direction."""
    if features.direction == "bearish" or features.action in ("AVOID_CHASE", "CHASE_RISK"):
        return -realized_return
    return realized_return


# ============================================================
# Data fetch
# ============================================================

def fetch_candidate_signals(limit: int = 200) -> list[dict]:
    status, rows = sb_get("stock_signals", {
        "status_v2": "in.(candidate,sent,suppressed)",
        "select": (
            "id,ticker,fired_at,action,direction,horizon_days,score,confidence,"
            "evidence_summary,status_v2,model_version,weight_at_time,score_breakdown"
        ),
        "order": "fired_at.desc",
        "limit": str(limit),
    })
    if status != 200:
        print(f"Could not fetch candidate signals: HTTP {status}", file=sys.stderr)
        return []
    return [r for r in rows if r.get("ticker") and r.get("id")]


def existing_forecast_signal_ids(signal_ids: list[int]) -> set[int]:
    if not signal_ids:
        return set()
    found: set[int] = set()
    for i in range(0, len(signal_ids), 100):
        chunk = ",".join(str(x) for x in signal_ids[i:i + 100])
        status, rows = sb_get("stock_paper_forecasts", {
            "signal_id": f"in.({chunk})",
            "select": "signal_id",
        })
        if status != 200:
            return set()
        found.update(int(r["signal_id"]) for r in rows if r.get("signal_id") is not None)
    return found


def fetch_calibration_rows(limit: int = 5000) -> list[CalibrationRow]:
    status, audits = sb_get("stock_forecast_audit", {
        "select": "signal_id,horizon_days,realized_return,correct",
        "order":  "computed_at.desc",
        "limit":  str(limit),
    })
    if status != 200 or not audits:
        return []
    audits = [
        a for a in audits
        if a.get("signal_id") is not None
        and a.get("realized_return") is not None
        and a.get("correct") is not None
    ]
    ids = sorted({int(a["signal_id"]) for a in audits})
    signals: dict[int, dict] = {}
    for i in range(0, len(ids), 100):
        chunk = ",".join(str(x) for x in ids[i:i + 100])
        st, rows = sb_get("stock_signals", {
            "id": f"in.({chunk})",
            "select": (
                "id,ticker,fired_at,action,direction,horizon_days,score,"
                "evidence_summary,status_v2,model_version,weight_at_time,score_breakdown"
            ),
            "limit": "100",
        })
        if st == 200:
            signals.update({int(r["id"]): r for r in rows if r.get("id") is not None})

    out: list[CalibrationRow] = []
    seen_keys: set[tuple] = set()
    for audit in audits:
        sid = int(audit["signal_id"])
        sig = signals.get(sid)
        if not sig:
            continue
        model_version = str(sig.get("model_version") or "")
        if "backtest" in model_version and "v1.1" not in model_version:
            continue
        dedupe_key = (
            sig.get("ticker"),
            str(sig.get("fired_at") or "")[:19],
            sig.get("action"),
            sig.get("direction"),
            sig.get("score"),
            sig.get("evidence_summary"),
            audit.get("horizon_days"),
        )
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)
        features = features_for_signal(sig)
        try:
            realized = float(audit["realized_return"])
        except (TypeError, ValueError):
            continue
        out.append(CalibrationRow(
            signal_id=sid,
            features=features,
            correct=bool(audit["correct"]),
            realized_return=realized,
            thesis_return=thesis_return(realized, features),
        ))
    return out


def fetch_latest_prices(tickers: list[str]) -> dict[str, float]:
    if not tickers:
        return {}
    in_list = ",".join(f'"{t}"' for t in sorted(set(tickers)))
    status, rows = sb_get("stock_raw_prices", {
        "ticker": f"in.({in_list})",
        "select": "ticker,ts,close",
        "order":  "ts.desc",
        "limit":  "500",
    })
    if status != 200:
        return {}
    latest: dict[str, float] = {}
    for row in rows:
        ticker = row.get("ticker")
        if ticker in latest or row.get("close") is None:
            continue
        try:
            latest[ticker] = float(row["close"])
        except (TypeError, ValueError):
            continue
    return latest


# ============================================================
# Calibration
# ============================================================

def _mean_rate(rows: list[CalibrationRow]) -> float | None:
    if not rows:
        return None
    return sum(1 for r in rows if r.correct) / len(rows)


def _same_setup(row: CalibrationRow, target: SetupFeatures, strict: bool) -> bool:
    f = row.features
    if f.horizon_days != target.horizon_days or f.direction != target.direction:
        return False
    if strict:
        if f.score_bucket != target.score_bucket:
            return False
        if f.action != target.action and not (f.important_flags() & target.important_flags()):
            return False
        return True
    return f.action == target.action or f.score_bucket == target.score_bucket


def similar_rows(rows: list[CalibrationRow], target: SetupFeatures) -> list[CalibrationRow]:
    strict = [r for r in rows if _same_setup(r, target, strict=True)]
    if len(strict) >= MIN_SETUP_N_FOR_WATCH:
        return strict
    relaxed = [r for r in rows if _same_setup(r, target, strict=False)]
    if len(relaxed) >= MIN_SETUP_N_FOR_WATCH:
        return relaxed
    directional = [
        r for r in rows
        if r.features.horizon_days == target.horizon_days and r.features.direction == target.direction
    ]
    return directional


def bounded_level(value: float | None, default: float, lo: float, hi: float) -> float:
    if value is None or value <= 0:
        value = default
    return max(lo, min(hi, value))


def build_forecast(signal: dict, calibration_rows: list[CalibrationRow],
                   latest_prices: dict[str, float]) -> dict:
    features = features_for_signal(signal)
    same_direction = [
        r for r in calibration_rows
        if r.features.horizon_days == features.horizon_days and r.features.direction == features.direction
    ]
    base_rows = same_direction if same_direction else [
        r for r in calibration_rows if r.features.horizon_days == features.horizon_days
    ]
    base_rate = _mean_rate(base_rows)
    if base_rate is None:
        base_rate = 0.5

    setup = similar_rows(calibration_rows, features)
    setup_rate = _mean_rate(setup)
    n = len(setup)
    setup_wins = sum(1 for r in setup if r.correct)
    prob_win = (setup_wins + SHRINKAGE_K * base_rate) / (n + SHRINKAGE_K)

    favorable = [r.thesis_return for r in setup if r.thesis_return > 0]
    adverse = [-r.thesis_return for r in setup if r.thesis_return < 0]
    avg_win = mean(favorable) if favorable else 0.02
    avg_loss = mean(adverse) if adverse else 0.015
    expected_value = prob_win * avg_win - (1 - prob_win) * avg_loss
    risk_reward = (avg_win / avg_loss) if avg_loss > 0 else None

    paper_action = choose_paper_action(features, prob_win, expected_value, risk_reward, n)

    ticker = str(signal["ticker"])
    entry = latest_prices.get(ticker)
    level_win = bounded_level(avg_win, 0.02, 0.01, 0.08)
    level_loss = bounded_level(avg_loss, 0.015, 0.01, 0.06)
    target = stop = None
    if paper_action == "PAPER_LONG" and entry:
        target = entry * (1 + level_win)
        stop = entry * (1 - level_loss)

    reason = (
        f"{paper_action}: {prob_win:.0%} calibrated win probability; "
        f"n={n} similar, base={base_rate:.0%}, EV={expected_value:+.2%}"
    )

    feature_payload = {
        "model_version": MODEL_VERSION,
        "source_status": signal.get("status_v2"),
        "signal_score": float(signal.get("score") or 0),
        "score_bucket": features.score_bucket,
        "agents": list(features.agents),
        "flags": sorted(features.important_flags()),
        "base_n": len(base_rows),
        "setup_n": n,
        "shrinkage_k": SHRINKAGE_K,
        "source_evidence_summary": signal.get("evidence_summary"),
    }

    return {
        "signal_id":          int(signal["id"]),
        "ticker":             ticker,
        "fired_at":           signal["fired_at"],
        "horizon_days":       features.horizon_days,
        "direction":          features.direction,
        "source_action":      features.action,
        "paper_action":       paper_action,
        "prob_win":           round(prob_win, 4),
        "base_rate":          round(base_rate, 4),
        "setup_hit_rate":     round(setup_rate, 4) if setup_rate is not None else None,
        "sample_size":        n,
        "score_bucket":       features.score_bucket,
        "avg_win":            round(avg_win, 6),
        "avg_loss":           round(avg_loss, 6),
        "expected_value":     round(expected_value, 6),
        "risk_reward":        round(risk_reward, 4) if risk_reward is not None else None,
        "entry_price":        round(entry, 4) if entry else None,
        "target_price":       round(target, 4) if target else None,
        "stop_price":         round(stop, 4) if stop else None,
        "status":             "open",
        "features_json":      feature_payload,
        "calibration_method": CALIBRATION_METHOD,
        "reason_summary":     reason,
        "dedupe_key":         f"paper_v1_signal_{signal['id']}_h{features.horizon_days}",
        "updated_at":         datetime.now(timezone.utc).isoformat(),
    }


def choose_paper_action(features: SetupFeatures, prob_win: float, expected_value: float,
                        risk_reward: float | None, sample_size: int) -> str:
    if features.action == "CHASE_RISK":
        return "PAPER_CHASE_RISK"
    if features.action == "AVOID_CHASE" or features.direction == "bearish":
        return "PAPER_AVOID"
    if sample_size < MIN_SETUP_N_FOR_WATCH:
        return "NO_TRADE"
    if sample_size < MIN_SETUP_N_FOR_LONG:
        return "PAPER_WATCH" if prob_win >= 0.53 and expected_value > 0 else "NO_TRADE"
    if prob_win >= 0.55 and expected_value > 0 and (risk_reward or 0) >= 1.0:
        return "PAPER_LONG"
    if prob_win < 0.50 or expected_value <= 0:
        return "PAPER_AVOID"
    return "PAPER_WATCH"


# ============================================================
# Main
# ============================================================

def main() -> int:
    run_id = job_run_start()
    rows_in = rows_out = 0
    try:
        if not table_available("stock_paper_forecasts"):
            job_run_finish(run_id, "ok", 0, 0, "sql/0008_paper_forecasts.sql not applied")
            return 0

        signals = fetch_candidate_signals()
        rows_in = len(signals)
        if not signals:
            print("No candidate/sent/suppressed signals to forecast.")
            job_run_finish(run_id, "ok", rows_in, 0)
            return 0

        existing = existing_forecast_signal_ids([int(s["id"]) for s in signals])
        todo = [s for s in signals if int(s["id"]) not in existing]
        if not todo:
            print(f"All {len(signals)} open signals already have paper forecasts.")
            job_run_finish(run_id, "ok", rows_in, 0)
            return 0

        calibration_rows = fetch_calibration_rows()
        latest_prices = fetch_latest_prices([str(s["ticker"]) for s in todo])
        forecasts = [build_forecast(s, calibration_rows, latest_prices) for s in todo]
        ok = sb_post("stock_paper_forecasts", forecasts, on_conflict="dedupe_key", merge=True)
        rows_out = len(forecasts) if ok else 0
        print(f"Paper forecasts written: {rows_out} from {rows_in} open signals")
        for f in forecasts[:10]:
            print(
                f"  {f['ticker']} {f['paper_action']} p={f['prob_win']:.2f} "
                f"EV={f['expected_value']:+.4f} n={f['sample_size']}"
            )
        job_run_finish(run_id, "ok" if ok else "failed", rows_in, rows_out)
        return 0 if ok else 1
    except Exception as e:  # noqa: BLE001
        import traceback
        print(traceback.format_exc(), file=sys.stderr)
        job_run_finish(run_id, "failed", rows_in, rows_out, str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
