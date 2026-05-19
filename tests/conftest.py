"""Shared test fixtures.

Agent modules read SUPABASE_URL / SUPABASE_SERVICE_KEY at import time, so we
set dummy values here before any test file imports them. Tests must mock or
patch any function that actually issues HTTP requests.

Heavy third-party deps (yfinance, pandas) used by some agents at import time
are stubbed with empty modules — tests should never need their actual
behavior, so installing them in CI is wasteful.
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

# Stub heavy import-time deps before any agent module is imported.
# Agents only use these for live-data fetches, never in unit-tested code paths.
for mod_name in ("yfinance", "pandas", "lxml", "feedparser"):
    if mod_name not in sys.modules:
        sys.modules[mod_name] = types.ModuleType(mod_name)

# curl_cffi: source_review_agent and backtester do `from curl_cffi import
# requests as cffi_requests`. Provide a submodule .requests attribute.
if "curl_cffi" not in sys.modules:
    cc = types.ModuleType("curl_cffi")
    cc_req = types.ModuleType("curl_cffi.requests")
    class _StubSession:
        def __init__(self, *a, **kw): pass
        def get(self, *a, **kw): raise RuntimeError("curl_cffi.Session.get called in test")
        def post(self, *a, **kw): raise RuntimeError("curl_cffi.Session.post called in test")
    cc_req.Session = _StubSession  # type: ignore
    cc.requests = cc_req            # type: ignore
    sys.modules["curl_cffi"] = cc
    sys.modules["curl_cffi.requests"] = cc_req

# jinja2 is used by site_generator at module load (FileSystemLoader path
# computation). Stub Environment / FileSystemLoader / select_autoescape so
# imports succeed without the real dep — tests don't render templates.
if "jinja2" not in sys.modules:
    jinja2_stub = types.ModuleType("jinja2")
    class _StubEnv:
        def __init__(self, *a, **kw): pass
        def get_template(self, *a, **kw): return self
        def render(self, *a, **kw): return ""
    jinja2_stub.Environment = _StubEnv  # type: ignore
    jinja2_stub.FileSystemLoader = lambda *a, **kw: None  # type: ignore
    jinja2_stub.select_autoescape = lambda *a, **kw: None  # type: ignore
    sys.modules["jinja2"] = jinja2_stub

# Make repo root importable so `from agents.foo import bar` works,
# AND make agents/ importable so the agents' own intra-module imports
# (e.g. `from filing_agent import ...`, `import _rule_key`) resolve the
# same way they do at live runtime when scripts run as `python agents/foo.py`.
REPO_ROOT = Path(__file__).resolve().parents[1]
AGENTS_DIR = REPO_ROOT / "agents"
for p in (REPO_ROOT, AGENTS_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

os.environ.setdefault("SUPABASE_URL", "https://test.invalid")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-key-not-real")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-bot-token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "0")
