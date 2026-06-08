"""Guard: every action thesis_agent can emit has a dashboard color rule.

The post-PR1A vocabulary (CATALYST_*/MOMENTUM_ONLY) was emitted from 2026-05-22
but had no .tag.<ACTION> rule in styles.css, so those signals rendered as the
default uncolored tag. This test fails if a future vocabulary addition ships
without a matching color, catching the regression at CI instead of on the
live dashboard.
"""
from __future__ import annotations

import re
from pathlib import Path

STYLES = Path(__file__).resolve().parents[1] / "templates" / "styles.css"

# The signal actions thesis_agent.action_for() can write. Pre-maturity +
# legacy + post-PR1A catalyst vocabulary. Maturity-gated BUY/SELL included.
EMITTABLE_ACTIONS = [
    "WATCH", "RESEARCH", "AVOID_CHASE", "CHASE_RISK", "BUY", "SELL",
    "CATALYST_WATCH", "CATALYST_RESEARCH", "MOMENTUM_ONLY",
]


def test_every_emittable_action_has_a_css_rule():
    css = STYLES.read_text()
    missing = [a for a in EMITTABLE_ACTIONS
               if not re.search(rf"\.tag\.{re.escape(a)}\b", css)]
    assert not missing, f"actions with no .tag color rule in styles.css: {missing}"
