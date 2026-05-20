"""Regression tests for telegram_dispatcher.send_and_log + log_dispatch.

These exercise the contract audit_agent invariant #1 enforces:
  every status_v2='sent' signal has a delivery_ok=true dispatch_log row.

The dispatcher is the only thing on the path that can satisfy that
contract, so it gets pinned by tests.
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest


# We must set env before import — module-level os.environ[...] reads.
os.environ.setdefault("SUPABASE_URL", "https://test.local")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "BOT")
os.environ.setdefault("TELEGRAM_CHAT_ID", "CHAT")

import telegram_dispatcher as td  # noqa: E402


class FakeResp:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text

    def json(self):
        return self._json


def _patch_requests(monkeypatch, *, posts: list, gets: list | None = None):
    """Install fakes that record every POST/GET, with a queue of responses."""
    posts_out: list = []
    gets_out: list = []
    post_iter = iter(posts)
    get_iter = iter(gets or [])

    def fake_post(url, headers=None, json=None, data=None, timeout=None):
        posts_out.append({"url": url, "headers": headers, "json": json, "data": data})
        try:
            return next(post_iter)
        except StopIteration:
            return FakeResp(200, {})

    def fake_get(url, headers=None, params=None, timeout=None):
        gets_out.append({"url": url, "params": params})
        try:
            return next(get_iter)
        except StopIteration:
            return FakeResp(200, [])

    monkeypatch.setattr(td.requests, "post", fake_post)
    monkeypatch.setattr(td.requests, "get",  fake_get)
    return posts_out, gets_out


# ---------- send_and_log -----------------------------------------------------

def test_send_and_log_success_writes_dispatch_log(monkeypatch):
    """Happy path: Telegram returns ok=true → dispatch_log INSERT fires with
    delivery_ok=true and dedupe_key=dispatch_signal_<id>."""
    posts = [
        FakeResp(200, {"ok": True, "result": {"message_id": 999}}),   # Telegram
        FakeResp(201, {}),                                            # dispatch_log INSERT
    ]
    gets = [FakeResp(200, [])]   # pre-check: no existing row
    posts_out, gets_out = _patch_requests(monkeypatch, posts=posts, gets=gets)

    ok = td.send_and_log(42, "hello")

    assert ok is True
    assert len(posts_out) == 2
    assert "api.telegram.org" in posts_out[0]["url"]
    assert "stock_telegram_dispatch_log" in posts_out[1]["url"]
    log_row = posts_out[1]["json"]
    assert log_row["signal_id"]   == 42
    assert log_row["delivery_ok"] is True
    assert log_row["telegram_msg_id"] == 999
    assert log_row["dedupe_key"]  == "dispatch_signal_42"
    assert log_row["error"]       is None


def test_send_and_log_telegram_failure_still_logs(monkeypatch):
    """Telegram returns 400 → ok=false, dispatch_log still gets a row with
    delivery_ok=false and the error text."""
    posts = [
        FakeResp(400, {"ok": False, "description": "Bad Request"}, text="Bad Request"),
        FakeResp(201, {}),
    ]
    gets = [FakeResp(200, [])]
    posts_out, _ = _patch_requests(monkeypatch, posts=posts, gets=gets)

    ok = td.send_and_log(7, "fail-text")

    assert ok is False
    assert len(posts_out) == 2
    log_row = posts_out[1]["json"]
    assert log_row["signal_id"]   == 7
    assert log_row["delivery_ok"] is False
    assert log_row["telegram_msg_id"] is None
    assert log_row["error"]       is not None


def test_send_and_log_telegram_exception_still_logs(monkeypatch):
    """Connection error → ok=false, dispatch_log row written with the
    exception message in error."""
    posts_out: list = []
    gets_out: list = []
    insert_call = {"n": 0}

    def fake_post(url, headers=None, json=None, data=None, timeout=None):
        posts_out.append({"url": url, "json": json})
        if "api.telegram.org" in url:
            raise ConnectionError("network down")
        insert_call["n"] += 1
        return FakeResp(201, {})

    def fake_get(url, **kw):
        gets_out.append(url)
        return FakeResp(200, [])

    monkeypatch.setattr(td.requests, "post", fake_post)
    monkeypatch.setattr(td.requests, "get",  fake_get)

    ok = td.send_and_log(13, "boom")

    assert ok is False
    assert insert_call["n"] == 1
    assert posts_out[1]["json"]["error"] == "network down"
    assert posts_out[1]["json"]["delivery_ok"] is False


# ---------- log_dispatch (precheck + plain INSERT) ---------------------------

def test_log_dispatch_skips_if_already_logged(monkeypatch):
    """If precheck GET returns an existing row, no INSERT POST is made.
    This is the dedupe pattern that replaces ?on_conflict=dedupe_key
    (broken on partial index per CLAUDE.md rule #2)."""
    insert_count = {"n": 0}

    def fake_post(url, **kw):
        if "stock_telegram_dispatch_log" in url:
            insert_count["n"] += 1
        return FakeResp(201, {})

    def fake_get(url, **kw):
        return FakeResp(200, [{"id": 1}])   # existing row

    monkeypatch.setattr(td.requests, "post", fake_post)
    monkeypatch.setattr(td.requests, "get",  fake_get)

    td.log_dispatch(42, "txt", True, 999, None)
    assert insert_count["n"] == 0


def test_log_dispatch_inserts_plain_no_on_conflict_in_url(monkeypatch):
    """The INSERT URL must NOT carry ?on_conflict=dedupe_key — that's the
    partial-index bug we're avoiding."""
    captured = {"url": None}

    def fake_post(url, headers=None, json=None, data=None, timeout=None):
        if "stock_telegram_dispatch_log" in url:
            captured["url"] = url
        return FakeResp(201, {})

    def fake_get(url, **kw):
        return FakeResp(200, [])   # no existing row

    monkeypatch.setattr(td.requests, "post", fake_post)
    monkeypatch.setattr(td.requests, "get",  fake_get)

    td.log_dispatch(42, "txt", True, 999, None)
    assert captured["url"] is not None
    assert "on_conflict=" not in captured["url"]
