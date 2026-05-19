"""Smoke test — confirms pytest infra works and agents can be imported with
the conftest's dummy env vars in place. Real coverage lives in the other
test_*.py files."""
from __future__ import annotations


def test_pytest_runs():
    assert 1 + 1 == 2


def test_repo_root_importable():
    import agents  # noqa: F401
