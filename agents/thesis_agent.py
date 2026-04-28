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
  -10 staleness (event > 15 min old at scoring time)

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

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

HEADERS_SB = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
    "Prefer":        "resolution=ignore-duplicates,return=minimal",
}

# Look back this far for "fresh" events. Anything older has likely been
# scored already (dedupe_key prevents double signals).
FRESHNESS_WINDOW_MIN = 30
CLUSTER_WINDOW_MIN   = 5
MAX_ALERTS_PER_DAY   = 5
DEDUPE_WINDOW_MIN    = 60

MODEL_VERSION = "rubric-v1.0"


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

def fetch_fresh_events() -> list[dict]:
    """Pull normalized_events from the last FRESHNESS_WINDOW_MIN minutes.
    Use params= so requests URL-encodes the +00:00 in the ISO timestamp correctly."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=FRESHNESS_WINDOW_MIN)).isoformat()
    params = {
        "event_at":  f"gte.{cutoff}",
        "ticker":    "not.is.null",
        "select":    "id,event_type,event_subtype,ticker,event_at,severity,source_table,parser_confidence,payload",
        "order":     "event_at.desc",
        "limit":     "500",
    }
    r = requests.get(f"{SUPABASE_URL}/rest/v1/stock_normalized_events",
                     headers=HEADERS_SB, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


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


def alerts_sent_today() -> int:
    """Count signals already sent today (UTC) — for the daily cap."""
    today = datetime.now(timezone.utc).date().isoformat()
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/stock_signals",
        headers=HEADERS_SB,
        params={
            "fired_at":  f"gte.{today}T00:00:00Z",
            "status_v2": "eq.sent",
            "select":    "id",
        },
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
            "limit":     "1",
        },
        timeout=10,
    )
    return r.status_code == 200 and len(r.json()) > 0


# ============================================================
# Scoring (§17.7 active-Phase-1 rules only)
# ============================================================

def source_agent_for(event: dict) -> str:
    """Map normalized event back to its originating agent."""
    et = event["event_type"]
    if et == "truth_social_post":   return "truth_social"
    if et.startswith("filing_"):    return "filing"
    if et == "8k_material_event":   return "filing"
    if et == "position_change":     return "flows"
    if et in ("price_gap", "volume_anomaly", "volatility_spike"): return "price"
    if et in ("news_headline",):    return "news"
    return "unknown"


def score_evidence(events: list[dict]) -> tuple[float, list[dict]]:
    """Apply Phase-1 rubric. Returns (total_score, breakdown)."""
    score = 0.0
    breakdown: list[dict] = []

    def add(rule: str, points: float, ev_id: int | None = None, detail: str = "") -> None:
        nonlocal score
        score += points
        breakdown.append({"rule": rule, "points": points, "event_id": ev_id, "detail": detail})

    now = datetime.now(timezone.utc)

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
        # Truth Social mention
        elif et == "truth_social_post":
            add("truth_social_mapping", 15, e["id"], e.get("event_subtype") or "")
        # Other filings carry only the severity component
        elif et.startswith("filing_") and sev > 0:
            add(f"filing_other_sev{sev}", min(15, sev * 4), e["id"])

        # Staleness penalty
        try:
            event_at = datetime.fromisoformat(e["event_at"].replace("Z", "+00:00"))
            age_min = (now - event_at).total_seconds() / 60
            if age_min > 15:
                add("staleness", -10, e["id"], f"{int(age_min)}m old")
        except Exception:
            pass

    return score, breakdown


def cluster_passes(events: list[dict]) -> tuple[bool, str]:
    """§15.3 cluster rule: ≥2 distinct source agents OR a single-source exception."""
    agents = {source_agent_for(e) for e in events}
    if len(agents) >= 2:
        return True, f"cluster:{','.join(sorted(agents))}"
    # Single-source exceptions
    for e in events:
        et = e["event_type"]
        sev = e.get("severity") or 0
        if et == "filing_13d" or e.get("event_subtype") == "13D":
            return True, "exception:sc_13d"
        if et == "8k_material_event" and sev >= 4:
            return True, "exception:8k_sev4"
    return False, "single_source_no_exception"


def action_for_score(score: float) -> str:
    if score >= 70: return "WATCH"
    if score >= 50: return "RESEARCH"
    return ""  # suppress


def horizon_for(events: list[dict]) -> str:
    # Filings → 1d default. Truth Social → 15m. Position changes → 5d.
    if any(e["event_type"] == "truth_social_post" for e in events):
        return "15m"
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

def write_signal(ticker: str, score: float, action: str, breakdown: list[dict],
                 events: list[dict], dedupe_key: str) -> int | None:
    payload = {
        "ticker":           ticker,
        "fired_at":         datetime.now(timezone.utc).isoformat(),
        "direction":        "WATCH",   # legacy column; status_v2 + action carry the real meaning
        "confidence":       round(min(score, 100) / 100, 4),
        "horizon_days":     1 if horizon_for(events) == "1d" else 0,
        "thesis_summary":   evidence_summary(events),
        "model_version":    MODEL_VERSION,
        "weight_at_time":   {"agents": list({source_agent_for(e) for e in events})},
        "status":           "open",
        "action":           action,
        "score":            round(score, 2),
        "score_breakdown":  {"items": breakdown},
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
        print(f"  signal insert {r.status_code}: {r.text}", file=sys.stderr)
        return None
    sig = r.json()[0]
    sig_id = sig["id"]
    # Link evidence
    ev_rows = [{
        "signal_id": sig_id,
        "agent":     source_agent_for(e),
        "event_id":  e["id"],
        "strength":  1.0,
        "detail":    f"{e['event_type']}{':'+e.get('event_subtype') if e.get('event_subtype') else ''}",
    } for e in events]
    if ev_rows:
        requests.post(f"{SUPABASE_URL}/rest/v1/stock_signal_evidence",
                      headers=HEADERS_SB, json=ev_rows, timeout=15)
    return sig_id


def mark_signal_status(signal_id: int, status_v2: str) -> None:
    requests.patch(
        f"{SUPABASE_URL}/rest/v1/stock_signals?id=eq.{signal_id}",
        headers=HEADERS_SB,
        json={"status_v2": status_v2}, timeout=10,
    )


# ============================================================
# Main
# ============================================================

def main() -> int:
    started = time.time()
    run_id = job_run_start("thesis_agent")
    sent = 0
    suppressed = 0

    try:
        events = fetch_fresh_events()
        print(f"Fresh events in last {FRESHNESS_WINDOW_MIN}m: {len(events)}")
        if not events:
            job_run_finish(run_id, "ok", 0, 0)
            return 0

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

        # Daily cap check
        already_today = alerts_sent_today()
        cap_remaining = max(0, MAX_ALERTS_PER_DAY - already_today)
        print(f"Alerts already sent today: {already_today} (cap remaining: {cap_remaining})")

        # Score and rank
        scored = []
        for (ticker, bucket), ev_list in clusters.items():
            ok, cluster_label = cluster_passes(ev_list)
            score, breakdown = score_evidence(ev_list)
            action = action_for_score(score)
            scored.append({
                "ticker":   ticker,
                "bucket":   bucket,
                "events":   ev_list,
                "score":    score,
                "action":   action,
                "cluster_ok": ok,
                "cluster_label": cluster_label,
                "breakdown": breakdown,
                "dedupe_key": f"thesis_{ticker}_{bucket}",
            })

        # Filter: must pass cluster + have non-empty action
        candidates = [s for s in scored if s["cluster_ok"] and s["action"]]
        # Skip already-signaled buckets
        existing = already_signaled_dedupe_keys([c["dedupe_key"] for c in candidates])
        candidates = [c for c in candidates if c["dedupe_key"] not in existing]
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
                breakdown=cand["breakdown"], events=cand["events"],
                dedupe_key=cand["dedupe_key"],
            )
            if sig_id is None:
                continue
            # Cap: only WATCH respects the daily cap; RESEARCH is suppressed past cap
            if cand["action"] == "WATCH" and cap_remaining > 0:
                from telegram_dispatcher import dispatch_signal
                ok = dispatch_signal(sig_id)
                if ok:
                    mark_signal_status(sig_id, "sent")
                    cap_remaining -= 1
                    sent += 1
                    print(f"  {ticker}: SENT (score={cand['score']:.0f}, sig_id={sig_id})")
                else:
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
