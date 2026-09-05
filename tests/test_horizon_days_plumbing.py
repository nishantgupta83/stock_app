"""The emitted horizon must survive the trip from thesis_agent to the setup row.

Found 2026-09-04 by an adversarial review of the maturity gate.

`thesis_agent.write_signal` wrote `horizon_days` as a 1/0 FLAG
(`1 if horizon_for(events) == "1d" else 0`), and `stock_signals.horizon_days`
has no CHECK constraint, so any horizon other than 1d persisted as 0. Every
consumer then coerced it back with `int(x or 1)`:

  price_agent.py:143,290,325,780,881,944 | trade_setup_agent.py:366
  realistic_loop_agent.py:289

so changing `horizon_for()` would NOT have lengthened the horizon anywhere —
it would only have made `cluster_has_mature_rule` start matching the h7d/h15d/
h30d adult cells, i.e. it would have unlocked BUY/SELL on signals still graded
and held for one day. That is the failure mode these tests exist to prevent.

`trade_setup_agent.derive_rule_key`'s own docstring (:235-237) already warns
against the `or 1` short-circuit and fixes it at :242-243 — but :366 still had
it. Same invariant, asserted in a comment, not enforced two functions later.

Both fixes are behaviour-neutral today: `horizon_for()` returns "1d", and live
`stock_signals.horizon_days` holds only 1 and 30 (no 0, no NULL) as of
2026-09-04. These tests pin the plumbing so a future horizon change is safe.
"""
from __future__ import annotations

import thesis_agent
import trade_setup_agent


class _Resp:
    status_code = 201

    def json(self):
        return [{"id": 1}]


class TestWriteSignalHorizon:
    def _capture(self, monkeypatch):
        seen = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            # write_signal posts the signal first, then evidence rows as a LIST.
            # Capture only the signal insert.
            if url.endswith("/stock_signals") and "payload" not in seen:
                seen["payload"] = json
            return _Resp()

        monkeypatch.setattr(thesis_agent.requests, "post", fake_post)
        return seen

    def test_writes_1_for_the_current_1d_horizon(self, monkeypatch):
        seen = self._capture(monkeypatch)
        events = [{"id": 1, "event_type": "clinical_readout", "event_subtype": "completed"}]
        thesis_agent.write_signal("ABC", 70.0, "WATCH", "bullish", [], events, "k")
        assert seen["payload"]["horizon_days"] == 1

    def test_a_longer_horizon_is_written_as_that_horizon_not_zero(self, monkeypatch):
        # The regression: this wrote 0, which every consumer read back as 1.
        seen = self._capture(monkeypatch)
        monkeypatch.setattr(thesis_agent, "horizon_for", lambda events: "7d")
        events = [{"id": 1, "event_type": "clinical_readout", "event_subtype": "completed"}]
        thesis_agent.write_signal("ABC", 70.0, "WATCH", "bullish", [], events, "k")
        assert seen["payload"]["horizon_days"] == 7

    def test_horizon_days_matches_emitted_horizon_days(self, monkeypatch):
        # The two must never disagree — emitted_horizon_days is what the C2
        # maturity check uses to decide WHICH horizon's calibration licenses
        # BUY/SELL. If the persisted horizon differs, the gate is scoped to a
        # horizon the trade is not actually held for.
        seen = self._capture(monkeypatch)
        monkeypatch.setattr(thesis_agent, "horizon_for", lambda events: "15d")
        events = [{"id": 1, "event_type": "8k_material_event", "event_subtype": ""}]
        thesis_agent.write_signal("ABC", 70.0, "WATCH", "bullish", [], events, "k")
        assert seen["payload"]["horizon_days"] == thesis_agent.emitted_horizon_days(events)


class TestSetupHorizonNotCoerced:
    def _signal(self, horizon):
        return {
            "id": 1, "ticker": "ABC", "direction": "bullish", "score": 70,
            "action": "WATCH", "horizon_days": horizon,
            "valid_until": "2099-01-01T00:00:00+00:00",
            "weight_at_time": {"primary_event_types": ["8k_material_event"],
                               "primary_event_subtype": ""},
        }

    def test_explicit_horizon_is_preserved(self):
        setup = trade_setup_agent.compute_setup(self._signal(7), {})
        assert setup is not None and setup["horizon_days"] == 7

    def test_zero_is_not_silently_promoted_to_one(self):
        # `int(x or 1)` turned 0 into 1 — the exact short-circuit
        # derive_rule_key's docstring says was fixed. A 0 is corrupt data and
        # must not masquerade as a valid 1-day horizon.
        setup = trade_setup_agent.compute_setup(self._signal(0), {})
        assert setup is not None and setup["horizon_days"] == 0

    def test_missing_horizon_still_defaults_to_one(self):
        setup = trade_setup_agent.compute_setup(self._signal(None), {})
        assert setup is not None and setup["horizon_days"] == 1
