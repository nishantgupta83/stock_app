"""Canonical rule_key derivation.

Single source of truth used by event_paper_agent (writes calibration rows),
trade_setup_agent (looks up calibration for sizing), and thesis_agent
(checks rule maturity for vocabulary gating).

Format:
    "{event_type}:{subtype}:h{horizon_days}d"

The subtype field is always present (empty string when absent) so split(":")
always yields three parts — downstream parsers can rely on it.

Why a shared module: prior to this, event_paper_agent wrote keys like
"earnings_release:beat:h7d" while trade_setup_agent looked up
"earnings_release::h7d". Every signal with a non-empty subtype silently fell
back to a default confidence and skipped adaptive sizing. Centralizing here
prevents that drift from re-emerging.
"""
from __future__ import annotations


def derive(event_type: str, subtype: str | None, horizon_days: int) -> str:
    """Build a rule_key from its three components.

    Args:
      event_type: required, e.g. "earnings_release", "8k_material_event"
      subtype: optional, e.g. "beat", "miss", or None / "" for subtype-less
      horizon_days: integer days, typically one of (1, 7, 15, 30)

    Returns:
      Canonical key string. Empty/None subtypes render as an empty middle
      field so format remains stable: "event_type::h7d".
    """
    sub = (subtype or "").strip()
    return f"{event_type}:{sub}:h{int(horizon_days)}d"
