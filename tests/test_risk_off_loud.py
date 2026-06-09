"""H3 — the VIX risk-off branch must FAIL LOUD, not silently, when VIX isn't
ingested (verified 2026-06-09: zero VIX rows). It still fails open (returns
False, never suppresses on missing data) but logs that the VIX dimension is
inactive — a silent phantom safety net is worse than a known-absent one.
"""
from __future__ import annotations

import thesis_agent


class _Empty200:
    status_code = 200
    def json(self):
        return []


def test_is_risk_off_warns_loudly_when_no_vix(monkeypatch, capsys):
    monkeypatch.setattr(thesis_agent.requests, "get", lambda *a, **k: _Empty200())
    result = thesis_agent.is_risk_off()
    assert result is False                       # fail OPEN — never suppress on a gap
    err = capsys.readouterr().err
    assert "VIX dimension" in err and "INACTIVE" in err
