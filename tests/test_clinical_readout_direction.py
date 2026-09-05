"""clinical_readout direction must be a DECLARED decision, not dict fallthrough.

Found 2026-09-04 by an adversarial review of the maturity gate, confirmed live:
biotech_agent emits direction_prior="neutral" for every ctgov Phase-3 readout
(agents/biotech_agent.py:284), derive_direction deliberately defers "neutral"
to the per-type default table (event_paper_agent.py:260-262), and
`clinical_readout` was absent from that table — so `.get(et, "long")` graded
every clinical trade LONG, including 164 `terminated` (failed/halted) trials.
The family that produced all five is_mature rules never had its direction
decided by anyone.
"""
from __future__ import annotations

import event_paper_agent as epa


def _ev(subtype, prior="neutral"):
    return {"event_type": "clinical_readout", "event_subtype": subtype,
            "payload": {"direction_prior": prior}}


def test_clinical_readout_is_declared_not_fallthrough():
    assert "clinical_readout" in epa._DIRECTION_DEFAULT


# --- known-BAD fixture: must FAIL on the pre-fix code ---------------------
def test_terminated_trial_is_not_graded_long():
    # A terminated Phase 3 is a failed/halted trial. Grading it long was the
    # bug; the direction must be short.
    assert epa.derive_direction(_ev("terminated")) == "short"


def test_completed_and_active_not_recruiting_are_long_by_declaration():
    assert epa.derive_direction(_ev("completed")) == "long"
    assert epa.derive_direction(_ev("active_not_recruiting")) == "long"
    # and that comes from the explicit subtype table, not the type default
    assert epa._CLINICAL_SUBTYPE_DIRECTION["completed"] == "long"
    assert epa._CLINICAL_SUBTYPE_DIRECTION["active_not_recruiting"] == "long"


def test_unknown_clinical_subtype_uses_declared_type_default():
    assert epa.derive_direction(_ev("suspended")) == epa._DIRECTION_DEFAULT["clinical_readout"]


def test_explicit_long_short_prior_still_wins():
    # An upstream agent that DOES decide a direction is not overridden.
    assert epa.derive_direction(_ev("terminated", prior="long")) == "long"
    assert epa.derive_direction(_ev("completed", prior="short")) == "short"


def test_short_graded_terminated_trades_accrue_in_a_fresh_calibration_cell():
    # Review C2: rule_keys carry no direction. Without quarantine the new SHORT
    # outcomes would be averaged into `clinical_readout:terminated:h7d`
    # (n=38, PF 2.31, long-graded) — the cell the maturity gate reads.
    rk = epa.derive_rule_key(_ev("terminated"), 7)
    assert rk.split(":")[1] == "terminated_short"
    assert rk != epa._rule_key.derive("clinical_readout", "terminated", 7)


def test_long_declared_clinical_subtypes_keep_their_existing_cells():
    # completed / active_not_recruiting were ALREADY graded long; their cells
    # stay continuous (no direction change => no blending).
    for sub in ("completed", "active_not_recruiting"):
        assert epa.derive_rule_key(_ev(sub), 15) == epa._rule_key.derive("clinical_readout", sub, 15)


def test_neutral_prior_is_still_deferred_not_honoured():
    # Pre-existing behaviour, pinned so a future change is deliberate: a
    # "neutral" prior does not suppress the trade, it defers to the tables.
    assert epa.derive_direction(_ev("completed", prior="neutral")) == "long"
