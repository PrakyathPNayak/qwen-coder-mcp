"""Tests for cursor persistence: atomic `_save_cursor` plus the
existing `_load_cursor` fallback path.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


@pytest.fixture
def cursor(tmp_path: Path, monkeypatch):
    import agent.loop as L

    cursor_file = tmp_path / ".loop" / "cursor.json"
    monkeypatch.setattr(L, "CURSOR_FILE", cursor_file)
    return L, cursor_file


def test_save_then_load_round_trips(cursor):
    L, f = cursor
    L._save_cursor(42)
    assert L._load_cursor() == 42


def test_save_overwrites_previous_value(cursor):
    L, f = cursor
    L._save_cursor(1)
    L._save_cursor(7)
    assert L._load_cursor() == 7


def test_save_does_not_leave_tmp_artifact(cursor):
    L, f = cursor
    L._save_cursor(3)
    siblings = list(f.parent.iterdir())
    assert siblings == [f], f"unexpected leftovers: {siblings}"


def test_load_returns_0_on_missing_file(cursor):
    L, f = cursor
    assert not f.exists()
    assert L._load_cursor() == 0


def test_load_returns_0_on_empty_file_simulated_crash(cursor):
    """Simulates being killed *between* truncate and write."""
    L, f = cursor
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("", "utf-8")
    assert L._load_cursor() == 0


def test_load_returns_0_on_corrupt_json(cursor):
    L, f = cursor
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("{not json", "utf-8")
    assert L._load_cursor() == 0


def test_save_atomicity_no_partial_state_visible(cursor):
    """The replacement must be all-or-nothing.

    We pre-seed a valid cursor at value 11, then simulate a save that
    *fails* by patching `os.replace` to raise. After the failed save,
    the original file must still parse as 11 (not be truncated to 0).

    Loop 19 changed `_save_cursor` to log-and-continue rather than
    re-raise on rename failure (re-raising caused unbounded re-scans
    of the same file when disk filled up, since the next iteration
    re-loads the old cursor). The test now asserts the silent path.
    """
    L, f = cursor
    L._save_cursor(11)

    import agent.loop as mod

    real_replace = os.replace

    def boom(*_a, **_kw):
        raise OSError("simulated rename failure")

    mod.os.replace = boom  # type: ignore[attr-defined]
    try:
        # Must NOT raise — but must also NOT corrupt the existing cursor.
        L._save_cursor(99)
    finally:
        mod.os.replace = real_replace  # type: ignore[attr-defined]

    assert L._load_cursor() == 11
    siblings = list(f.parent.iterdir())
    assert siblings == [f], f"tmp not cleaned up: {siblings}"


def test_save_creates_parent_directory(tmp_path, monkeypatch):
    import agent.loop as L

    deep = tmp_path / "a" / "b" / "c" / "cursor.json"
    monkeypatch.setattr(L, "CURSOR_FILE", deep)
    L._save_cursor(5)
    assert json.loads(deep.read_text("utf-8")) == {"idx": 5}


def test_save_cursor_swallows_rename_failure_and_logs(cursor, monkeypatch, tmp_path):
    """Loop 19 contract: rename failure does not propagate, but the
    failure is logged so an operator can see disk problems.
    """
    L, f = cursor
    L._save_cursor(7)
    log_calls: list[str] = []
    monkeypatch.setattr(L, "_log", lambda msg: log_calls.append(msg))

    import agent.loop as mod
    real_replace = os.replace
    mod.os.replace = lambda *a, **k: (_ for _ in ()).throw(OSError("nope"))
    try:
        L._save_cursor(13)  # must not raise
    finally:
        mod.os.replace = real_replace
    assert L._load_cursor() == 7
    assert any("cursor save failed" in m for m in log_calls), log_calls


def test_save_cursor_swallows_rename_failure_even_if_log_fails(cursor, monkeypatch):
    """Logging itself is wrapped in try/except — so even a broken logger
    cannot crash the loop."""
    L, f = cursor
    L._save_cursor(3)

    def boom_log(_msg):
        raise RuntimeError("logger broken")

    monkeypatch.setattr(L, "_log", boom_log)
    import agent.loop as mod
    real_replace = os.replace
    mod.os.replace = lambda *a, **k: (_ for _ in ()).throw(OSError("nope"))
    try:
        L._save_cursor(99)  # must not raise even though logger raises
    finally:
        mod.os.replace = real_replace
    assert L._load_cursor() == 3
