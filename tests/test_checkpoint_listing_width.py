"""Terminal-width awareness for ``_format_checkpoint_listing``.

Mirrors the loop-202/203 pattern: a ``width: int | None = None`` kwarg
that defaults to the current terminal columns via
``shutil.get_terminal_size`` and truncates long snapshot names with a
trailing ``…`` instead of wrapping. This is the third and final
renderer in the terminal-width arc (turn profile, history diff,
checkpoint listing).
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from qwen_coder_mcp.tui import _format_checkpoint_listing


def _make_snap(tmp_path: Path, name: str, body: bytes = b"x") -> Path:
    p = tmp_path / name
    p.write_bytes(body)
    # Set a deterministic mtime so length math is stable.
    os.utime(p, (1700000000, 1700000000))
    return p


class TestExplicitWidth:
    def test_wide_terminal_keeps_full_name(self, tmp_path: Path) -> None:
        snap = _make_snap(tmp_path, "agent-state-2024-01-01T00-00-00Z.json")
        out = _format_checkpoint_listing([snap], width=200)
        assert snap.name in out
        assert "…" not in out

    def test_narrow_terminal_truncates_name(self, tmp_path: Path) -> None:
        # 40-col floor: name budget = 40 - 37 = 3, but min(8) kicks in -> 8.
        snap = _make_snap(
            tmp_path, "agent-state-2024-01-01T00-00-00Z-very-long.json"
        )
        out = _format_checkpoint_listing([snap], width=40)
        assert "…" in out
        # The full name must NOT appear.
        assert snap.name not in out

    def test_floor_protects_against_absurd_narrow(self, tmp_path: Path) -> None:
        # width=10 should be floored to 8-char name budget, never crash.
        snap = _make_snap(tmp_path, "abcdefghijklmnop.json")
        out = _format_checkpoint_listing([snap], width=10)
        # Output renders something with the truncation marker.
        assert "…" in out
        # Should contain at least a few chars of the name.
        assert "a" in out

    def test_short_name_never_truncated(self, tmp_path: Path) -> None:
        snap = _make_snap(tmp_path, "tiny.json")
        out = _format_checkpoint_listing([snap], width=80)
        assert "tiny.json" in out
        assert "…" not in out


class TestAutoWidth:
    def test_default_uses_terminal_size(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Pin the terminal at 50 cols; long name should truncate.
        monkeypatch.setattr(
            shutil,
            "get_terminal_size",
            lambda default=(80, 24): os.terminal_size((50, 24)),
        )
        snap = _make_snap(
            tmp_path,
            "agent-state-2024-12-31T23-59-59Z-with-extra-suffix-bytes.json",
        )
        out = _format_checkpoint_listing([snap])
        assert "…" in out
        assert snap.name not in out

    def test_default_wide_terminal_no_truncation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            shutil,
            "get_terminal_size",
            lambda default=(80, 24): os.terminal_size((200, 24)),
        )
        snap = _make_snap(tmp_path, "agent-state-2024-01-01T00-00-00Z.json")
        out = _format_checkpoint_listing([snap])
        assert snap.name in out
        assert "…" not in out

    def test_get_terminal_size_failure_falls_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(default=(80, 24)):
            raise OSError("not a tty")

        monkeypatch.setattr(shutil, "get_terminal_size", _boom)
        snap = _make_snap(tmp_path, "tiny.json")
        # Should not raise; falls back to 80 cols.
        out = _format_checkpoint_listing([snap])
        assert "tiny.json" in out


class TestEmpty:
    def test_empty_listing_unchanged(self) -> None:
        out = _format_checkpoint_listing([])
        assert out == "(no rotated checkpoints found)"

    def test_empty_listing_unchanged_with_width(self) -> None:
        # Width arg ignored on empty list.
        out = _format_checkpoint_listing([], width=20)
        assert out == "(no rotated checkpoints found)"


class TestStatFailure:
    def test_missing_file_still_truncates_name(self, tmp_path: Path) -> None:
        # Pass a path that doesn't exist; stat() raises but we should
        # still render a row containing the (possibly truncated) name.
        ghost = tmp_path / ("z" * 200 + ".json")
        out = _format_checkpoint_listing([ghost], width=60)
        assert "stat failed" in out
        # Long name truncated.
        assert "…" in out
