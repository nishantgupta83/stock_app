"""orchestrator_agent must only persist DB-allowed stock_job_runs.status values.

Regression guard for the 2026-08 fix: it used to write 'warning'/'error', which
the stock_job_runs.status CHECK constraint rejects (verified live 2026-08-02: 0
'warning' + 0 'error' rows ever, vs 212k 'ok' / 9k 'failed'). Those rejected
PATCHes silently no-op'd, orphaning every run as 'running' → reaped presumed_killed.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-key")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agents"))

import orchestrator_agent as orch  # noqa: E402

# The status vocabulary the CHECK constraint allows (sql/0004_ops_tables.sql:16).
# 'warning'/'error' are NOT in it — that was the bug.
DB_ALLOWED = {"running", "ok", "failed", "partial"}


def test_finish_status_maps_to_db_allowed_vocabulary():
    assert orch.finish_status(errored=False) == "ok"
    assert orch.finish_status(errored=True) == "failed"
    # The whole point: never emit a value the CHECK constraint rejects.
    assert orch.finish_status(False) in DB_ALLOWED
    assert orch.finish_status(True) in DB_ALLOWED
    assert "warning" not in {orch.finish_status(False), orch.finish_status(True)}
    assert "error" not in {orch.finish_status(False), orch.finish_status(True)}
