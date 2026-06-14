"""Single source of truth for Layer-2 lane identity + the signal statuses
Layer-3 is allowed to consume.

stock_signals is a SHARED table written by two producers:
  * thesis_agent          -> model_version = THESIS_MODEL_VERSION  (the real Layer 2)
  * intraday_alert_agent  -> model_version = "intraday-spike-v1"   (a Layer-1 lane)

Consumers (Layer-3 trade_setup_agent, the Layer-6 dashboard) MUST filter by
producer lane + status, NOT just by time — otherwise the intraday lane and
explicitly-suppressed signals bleed across the layer boundary (CLAUDE.md note
#7; the L3 boundary leak verified 2026-06-09). Centralizing the strings here
prevents the filter from drifting from the producer.
"""
from __future__ import annotations

# The ONLY Layer-2 (thesis) producer of stock_signals. Must equal
# thesis_agent.MODEL_VERSION — a test pins them so a version bump can't
# silently starve Layer-3 (which would fail closed: 0 setups).
THESIS_MODEL_VERSION = "rubric-v1.1"

# Statuses representing a live, non-suppressed Layer-2 emission that Layer-3
# should construct a trade setup from. POSITIVE allowlist on purpose: a negative
# list would silently admit closed/expired/demoted/backtest/dispatch_failed.
#   candidate = scored + written, pre-dispatch (or cap-deferred) — still a real
#               L2 emission the paper-learning loop should see.
#   sent      = dispatched to the operator.
# Excludes 'suppressed' (L2 explicitly chose NOT to emit) by construction.
# TRADE-OFF (documented Low): L3 reads these statuses within its LOOKBACK_HOURS
# window. A signal that was dispatch-retried and aged past that window falls out
# of L3's view and never becomes a setup. Accepted: such a stale signal's
# valid_until has almost always expired anyway (it would be skipped at L3), and
# widening the window costs egress. Revisit if retry latency grows.
L3_INPUT_STATUSES = ("candidate", "sent")
