"""Tests for STATE.md rotation."""
from __future__ import annotations

from pathlib import Path

import pytest

from agent import loop


@pytest.fixture
def temp_repo(tmp_path, monkeypatch):
    """Redirect all the loop's STATE/archive paths into a temp tree."""
    state = tmp_path / "STATE.md"
    archive_dir = tmp_path / ".loop" / "state_archive"
    monkeypatch.setattr(loop, "_REPO", tmp_path)
    monkeypatch.setattr(loop, "STATE_FILE", state)
    monkeypatch.setattr(loop, "STATE_ARCHIVE_DIR", archive_dir)
    return tmp_path


def test_no_rotation_when_under_threshold(temp_repo, monkeypatch):
    monkeypatch.setattr(loop, "STATE_MAX_BYTES", 1024)
    loop.STATE_FILE.write_text("small\n", "utf-8")
    result = loop._rotate_state_if_needed()
    assert result is None
    assert loop.STATE_FILE.read_text() == "small\n"
    assert not loop.STATE_ARCHIVE_DIR.exists()


def test_rotation_when_over_threshold(temp_repo, monkeypatch):
    monkeypatch.setattr(loop, "STATE_MAX_BYTES", 100)
    body = "# old state\n" + ("x" * 200) + "\n"
    loop.STATE_FILE.write_text(body, "utf-8")
    archive = loop._rotate_state_if_needed()
    assert archive is not None
    assert archive.exists()
    assert archive.read_text() == body
    # Fresh STATE.md has just the header + archive pointer.
    new = loop.STATE_FILE.read_text()
    assert new.startswith("# qwen-coder-mcp — Rolling State")
    assert archive.relative_to(loop._REPO).as_posix() in new


def test_rotation_missing_state_file_returns_none(temp_repo):
    # No STATE.md exists at all.
    assert loop._rotate_state_if_needed() is None


def test_append_state_triggers_rotation(temp_repo, monkeypatch):
    monkeypatch.setattr(loop, "STATE_MAX_BYTES", 50)
    loop.STATE_FILE.write_text("x" * 200, "utf-8")
    loop._append_state("- new entry\n")
    # The pre-rotation body must be in the archive.
    archives = list(loop.STATE_ARCHIVE_DIR.glob("STATE.*.md"))
    assert len(archives) == 1
    assert archives[0].read_text() == "x" * 200
    # The new entry must be present in the fresh file.
    fresh = loop.STATE_FILE.read_text()
    assert "- new entry" in fresh
    assert fresh.startswith("# qwen-coder-mcp — Rolling State")


def test_rotation_dedupes_same_second_collision(temp_repo, monkeypatch):
    """Two rotations triggered within the same second must not lose data."""
    monkeypatch.setattr(loop, "STATE_MAX_BYTES", 500)

    import datetime as real_dt
    fixed = real_dt.datetime(2025, 1, 1, 12, 0, 0, tzinfo=real_dt.timezone.utc)

    class FakeDT:
        @staticmethod
        def now(tz):
            return fixed

    monkeypatch.setattr(loop._dt, "datetime", FakeDT)

    loop.STATE_FILE.write_text("payload-A" * 100, "utf-8")
    a1 = loop._rotate_state_if_needed()
    loop.STATE_FILE.write_text("payload-B" * 100, "utf-8")
    a2 = loop._rotate_state_if_needed()

    assert a1 is not None and a2 is not None
    assert a1 != a2
    assert a1.read_text().startswith("payload-A")
    assert a2.read_text().startswith("payload-B")


def test_archive_dir_is_under_loop(temp_repo):
    """Archive lives under .loop/, which is .gitignored and internal."""
    assert ".loop" in loop.STATE_ARCHIVE_DIR.parts
    # And the loop's internal-path filter excludes .loop, so archive
    # files are correctly invisible to scope checks.
    assert loop._is_internal_path(Path(".loop") / "state_archive" / "STATE.X.md")


def test_rotation_threshold_default_is_reasonable():
    """Default threshold should be neither tiny nor unbounded."""
    assert 16 * 1024 <= loop.STATE_MAX_BYTES <= 4 * 1024 * 1024


def test_rotation_idempotent_when_fresh(temp_repo, monkeypatch):
    """Rotating again immediately after rotation is a no-op."""
    monkeypatch.setattr(loop, "STATE_MAX_BYTES", 500)
    loop.STATE_FILE.write_text("y" * 1000, "utf-8")
    a1 = loop._rotate_state_if_needed()
    a2 = loop._rotate_state_if_needed()
    assert a1 is not None
    assert a2 is None
    assert sorted(loop.STATE_ARCHIVE_DIR.glob("STATE.*.md")) == [a1]
