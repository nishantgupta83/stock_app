"""site_generator.sb_get/sb_count must retry transient Supabase read blips before
tripping the "refuse to publish" fail-safe. Regression guard for the recurring
'SB stock_agent_freshness' read failures: a single 5xx/429/connection hiccup on
any of ~32 reads used to fail the whole run (no retry).
"""
import os
import sys
from pathlib import Path
from unittest import mock

os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-key")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agents"))

import site_generator as sg  # noqa: E402


class _Resp:
    def __init__(self, code, payload=None, headers=None):
        self.status_code = code
        self._payload = payload if payload is not None else []
        self.text = "" if code == 200 else f"err{code}"
        self.headers = headers or {}

    def json(self):
        return self._payload


def _run(seq):
    """Drive sb_get with a fixed sequence of responses/exceptions."""
    calls = {"n": 0}

    def fake_get(*_a, **_k):
        item = seq[calls["n"]]
        calls["n"] += 1
        if isinstance(item, Exception):
            raise item
        return item

    with mock.patch.object(sg, "requests") as mreq, mock.patch.object(sg.time, "sleep"):
        mreq.RequestException = Exception
        mreq.get.side_effect = fake_get
        sg.SB_ERRORS.clear()
        out = sg.sb_get("stock_agent_freshness")
    return out, calls["n"], list(sg.SB_ERRORS)


def test_transient_5xx_then_success_is_not_an_error():
    out, n, errs = _run([_Resp(503), _Resp(503), _Resp(200, [{"ok": 1}])])
    assert out == [{"ok": 1}]
    assert n == 3          # retried twice, succeeded on the third
    assert errs == []      # no SB_ERRORS -> publish proceeds


def test_persistent_5xx_still_fails_safe():
    out, n, errs = _run([_Resp(503), _Resp(503), _Resp(503)])
    assert out == []
    assert n == 3
    assert len(errs) == 1  # persistent failure still recorded -> refuse to publish


def test_non_transient_4xx_does_not_retry():
    out, n, errs = _run([_Resp(400), _Resp(200, [{"ok": 1}])])
    assert out == []       # 400 is not transient -> no retry, reported
    assert n == 1
    assert len(errs) == 1


def test_connection_error_retries_then_reports():
    out, n, errs = _run([ConnectionError("reset"), ConnectionError("reset"), ConnectionError("reset")])
    assert out == []
    assert n == 3
    assert len(errs) == 1 and "conn-error" in errs[0]
