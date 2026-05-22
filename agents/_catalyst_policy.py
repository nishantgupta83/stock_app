"""Per-event-type evidence role + catalyst-eligibility age.

Single source of truth — imported by thesis_agent (cluster scoring) and
intraday_alert_agent (fast-twitch path). Both code paths must apply the
same policy so a 13F filing never gets framed as a same-day catalyst on
either surface.

See `thesis_agent.score_evidence` docstring + plan v3 for full rationale.
The short version: the 2026-05-22 alert audit found 45% of signals cited
a week-old 13F as the "catalyst" for that day's move; this dict + the
`decompose_score` step in thesis_agent prevent that by routing each
event_type to a role (catalyst / context / background) with a per-type
max age beyond which a catalyst-role event demotes to context.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# Each entry: {role, max_age_hours}
# role ∈ {"catalyst", "context", "background"}
# max_age_hours = how recent event_at must be for catalyst-role events to count
#                 as causal for the current bar. background events are never
#                 "catalyst" regardless of age (max_age_hours=0 is a marker).
CATALYST_POLICY: dict[str, dict] = {
    # === catalyst (causal for same-day moves) ===
    "news_article":               {"role": "catalyst",   "max_age_hours":  48},
    "analyst_rating_change":      {"role": "catalyst",   "max_age_hours":  72},  # future PR2
    "earnings_release":           {"role": "catalyst",   "max_age_hours": 120},
    "8k_material_event":          {"role": "catalyst",   "max_age_hours": 168},
    "clinical_readout":           {"role": "catalyst",   "max_age_hours": 240},
    "fda_pdufa_decision":         {"role": "catalyst",   "max_age_hours": 240},
    "truth_social_post":          {"role": "catalyst",   "max_age_hours":  48},
    "filing_dilution":            {"role": "catalyst",   "max_age_hours": 168},
    "filing_s-3":                 {"role": "catalyst",   "max_age_hours": 168},
    "filing_s-3/a":               {"role": "catalyst",   "max_age_hours": 168},
    "activist_5pct_crossed":      {"role": "catalyst",   "max_age_hours": 168},
    "activist_initial_position":  {"role": "catalyst",   "max_age_hours": 168},
    "insider_cluster_buy":        {"role": "catalyst",   "max_age_hours": 168},
    "dod_contract_award":         {"role": "catalyst",   "max_age_hours": 168},
    "nuclear_license_approval":   {"role": "catalyst",   "max_age_hours": 336},
    "fomc_decision":              {"role": "catalyst",   "max_age_hours": 168},
    "cpi_release":                {"role": "catalyst",   "max_age_hours":  72},
    "nfp_release":                {"role": "catalyst",   "max_age_hours":  72},
    "yield_milestone":            {"role": "catalyst",   "max_age_hours":  48},
    "vix_spike":                  {"role": "catalyst",   "max_age_hours":  24},
    # === context (supports thesis but not causal claim for today) ===
    "filing_13d":                 {"role": "context",    "max_age_hours": 720},
    "filing_13g":                 {"role": "context",    "max_age_hours": 336},
    "consumer_sentiment":         {"role": "context",    "max_age_hours": 168},
    "traffic_data":               {"role": "context",    "max_age_hours": 168},
    "momentum":                   {"role": "context",    "max_age_hours":  24},
    "crypto_macro_move":          {"role": "context",    "max_age_hours":  24},
    "price_gap":                  {"role": "context",    "max_age_hours":  24},
    "volume_anomaly":             {"role": "context",    "max_age_hours":  24},
    "volatility_spike":           {"role": "context",    "max_age_hours":  24},
    # === background (display-only; NEVER drives alert score) ===
    "institutional_new_position": {"role": "background", "max_age_hours":   0},
    "institutional_exit":         {"role": "background", "max_age_hours":   0},
    "institutional_increase":     {"role": "background", "max_age_hours":   0},
    "institutional_decrease":     {"role": "background", "max_age_hours":   0},
}
DEFAULT_POLICY = {"role": "context", "max_age_hours": 168}


def policy_for(event_type: str) -> dict:
    """Return {role, max_age_hours} for an event_type; falls back to default."""
    return CATALYST_POLICY.get(event_type, DEFAULT_POLICY)


def is_catalyst_eligible(event: dict, now: datetime | None = None) -> bool:
    """True if event's role is `catalyst` AND its event_at is within max_age_hours.

    Returns False for context/background roles regardless of age — only catalyst
    role with fresh event_at counts as a same-day causal signal.
    """
    et = event.get("event_type") or ""
    p = policy_for(et)
    if p["role"] != "catalyst":
        return False
    ea_str = event.get("event_at")
    if not ea_str:
        return False
    try:
        ea = datetime.fromisoformat(ea_str.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if now is None:
        now = datetime.now(timezone.utc)
    return (now - ea) <= timedelta(hours=p["max_age_hours"])


def split_events_by_role(events: list[dict], now: datetime | None = None) -> dict:
    """Partition events into {catalyst, context, background} lists.

    Catalyst list contains ONLY events whose role is catalyst AND whose age
    is within their type-specific max_age_hours. A catalyst-eligible event
    type with stale event_at falls into the context list (still display-
    able but not framed as a same-day cause).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    out = {"catalyst": [], "context": [], "background": []}
    for e in events:
        et = e.get("event_type") or ""
        p = policy_for(et)
        role = p["role"]
        if role == "catalyst":
            if is_catalyst_eligible(e, now):
                out["catalyst"].append(e)
            else:
                # Catalyst-type but too old → demote to context bucket
                out["context"].append(e)
        elif role == "background":
            out["background"].append(e)
        else:
            out["context"].append(e)
    return out
