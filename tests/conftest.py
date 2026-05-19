"""Shared test fixtures.

Agent modules read SUPABASE_URL / SUPABASE_SERVICE_KEY at import time, so we
set dummy values here before any test file imports them. Tests must mock or
patch any function that actually issues HTTP requests.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Make repo root importable so `from agents.foo import bar` works.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("SUPABASE_URL", "https://test.invalid")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-key-not-real")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-bot-token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "0")
