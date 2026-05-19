"""Import-smoke for every agent module.

The agents read env vars at import time and have intra-package imports (e.g.
event_paper_agent does `from filing_agent import ...`). A subtle change to
the import chain can break a live agent at runtime even when unit tests
that target specific functions pass. This file imports every module and
asserts that import succeeds — the cheapest way to catch a broken module.

If any agent breaks here, the corresponding GH workflow will fail
immediately on startup. Better to learn about it in CI than in
production at 04:00 UTC.
"""
from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENTS_DIR = REPO_ROOT / "agents"

# Modules we don't try to import — utility entry points or files we know
# can't run under the test env.
SKIP = {
    # __init__-like files; no .py here today, but defensive
    "_pycache_",
}


def _agent_modules() -> list[str]:
    """List every agents/*.py module name (without extension)."""
    names = []
    for p in sorted(AGENTS_DIR.glob("*.py")):
        if p.stem.startswith("_") and p.stem != "_rule_key":
            continue
        if p.stem in SKIP:
            continue
        names.append(p.stem)
    return names


@pytest.mark.parametrize("mod_name", _agent_modules())
def test_module_imports(mod_name):
    """Each agent module imports cleanly with the conftest-provided env."""
    mod = importlib.import_module(mod_name)
    assert mod is not None


def test_rule_key_module_exposes_derive():
    """Canonical API check on the shared module."""
    import _rule_key
    assert callable(_rule_key.derive)
    assert _rule_key.derive("foo", "bar", 1) == "foo:bar:h1d"


def test_thesis_exposes_e2_ttl_constants():
    """E2 added the TTL machinery; confirm it's exported."""
    import thesis_agent
    assert hasattr(thesis_agent, "EVENT_REAL_TTL_HOURS")
    assert hasattr(thesis_agent, "EVENT_REAL_TTL_DEFAULT_HOURS")
    assert hasattr(thesis_agent, "_event_within_real_ttl")
    assert hasattr(thesis_agent, "fetch_fresh_events")


def test_risk_agent_exposes_a1_function():
    import risk_agent
    assert hasattr(risk_agent, "compute_equity_curve_drawdown")
    assert hasattr(risk_agent, "compute_portfolio_state")
    assert hasattr(risk_agent, "SETUP_AGE_FLOOR_DAYS")
    # The old LOOKBACK_HOURS constant should be gone — caught here if someone
    # accidentally restores it.
    assert not hasattr(risk_agent, "LOOKBACK_HOURS")


def test_trade_setup_exposes_b1_function():
    import trade_setup_agent
    assert hasattr(trade_setup_agent, "compute_target_and_stop")
    assert hasattr(trade_setup_agent, "derive_primary_event_subtype")


def test_audit_agent_exposes_c2_invariants():
    import audit_agent
    assert hasattr(audit_agent, "INVARIANTS")
    assert len(audit_agent.INVARIANTS) == 5


def test_site_generator_exposes_d1_d2_d3():
    import site_generator
    assert hasattr(site_generator, "count_alerts_today_split")
    assert hasattr(site_generator, "fetch_recent_trade_setups")
    assert hasattr(site_generator, "fetch_recent_risk_decisions")
    assert hasattr(site_generator, "PIPELINE_VERSION")


def test_telegram_dispatcher_has_callbacks_flag():
    import telegram_dispatcher
    assert hasattr(telegram_dispatcher, "TELEGRAM_CALLBACKS_ENABLED")
    # E5: flag default must stay False until the webhook handler ships.
    assert telegram_dispatcher.TELEGRAM_CALLBACKS_ENABLED is False
