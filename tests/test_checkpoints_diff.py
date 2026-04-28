"""Loop 192 — `/checkpoints diff N` and the underlying
`format_history_diff` renderer."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from qwen_coder_mcp import fs_tools
from qwen_coder_mcp.tui import (
    ChatMessage,
    dispatch_slash,
    format_history_diff,
    parse_slash,
)


@pytest.fixture()
def fs_cfg(tmp_path: Path) -> fs_tools.FsConfig:
    return fs_tools.FsConfig(root=tmp_path)


def _msg(role: str, content: str) -> ChatMessage:
    return ChatMessage(role=role, content=content)


# ---------- format_history_diff ----------


class TestFormatHistoryDiff:
    def test_both_empty(self) -> None:
        out = format_history_diff([], [])
        assert "both" in out and "empty" in out

    def test_identical(self) -> None:
        a = [_msg("user", "hi"), _msg("assistant", "yo")]
        b = [_msg("user", "hi"), _msg("assistant", "yo")]
        out = format_history_diff(a, b)
        assert "same=2" in out
        assert "changed=0" in out
        # Each row prefixed with "=".
        assert out.count("=  ") == 2

    def test_content_changed(self) -> None:
        a = [_msg("user", "hi"), _msg("assistant", "yo")]
        b = [_msg("user", "hi"), _msg("assistant", "different")]
        out = format_history_diff(a, b)
        assert "same=1" in out
        assert "changed=1" in out
        assert "~  " in out

    def test_role_mismatch(self) -> None:
        a = [_msg("user", "hi")]
        b = [_msg("assistant", "hi")]
        out = format_history_diff(a, b)
        assert "role_mismatch=1" in out
        assert "≠" in out
        assert "user != assistant" in out

    def test_added_in_current(self) -> None:
        a = [_msg("user", "a"), _msg("assistant", "b"), _msg("user", "c")]
        b = [_msg("user", "a"), _msg("assistant", "b")]
        out = format_history_diff(a, b)
        assert "added=1" in out
        assert "+  " in out

    def test_dropped_from_current(self) -> None:
        a = [_msg("user", "a")]
        b = [_msg("user", "a"), _msg("assistant", "b")]
        out = format_history_diff(a, b)
        assert "dropped=1" in out
        assert "-  " in out

    def test_preview_truncates_long_content(self) -> None:
        a = [_msg("user", "x" * 200)]
        b = [_msg("user", "x" * 200)]
        out = format_history_diff(a, b, preview_chars=20)
        # 19 x's + ellipsis
        assert "x" * 19 + "…" in out
        # Full 200-x string never appears inline.
        assert "x" * 50 not in out

    def test_preview_collapses_newlines(self) -> None:
        a = [_msg("user", "line1\nline2")]
        b = [_msg("user", "line1\nline2")]
        out = format_history_diff(a, b)
        assert "line1 line2" in out
        # No inline literal newline inside the rendered preview row.
        # (Each row is one line in the output.)
        rows = [r for r in out.splitlines() if r.startswith("=")]
        assert all("\n" not in r for r in rows)

    def test_snapshot_label_in_header(self) -> None:
        a = [_msg("user", "hi")]
        b = [_msg("user", "hi")]
        out = format_history_diff(a, b, snapshot_label="agent_state-X.json")
        assert "agent_state-X.json" in out

    def test_header_counts_correct(self) -> None:
        a = [_msg("user", "a"), _msg("assistant", "b1"), _msg("user", "c")]
        b = [_msg("user", "a"), _msg("assistant", "b2")]
        out = format_history_diff(a, b)
        assert "current=3" in out
        assert "snapshot=2" in out
        assert "same=1" in out
        assert "changed=1" in out
        assert "added=1" in out


# ---------- /checkpoints diff dispatch ----------


def _write_snapshot(target: Path, history: list[ChatMessage]) -> Path:
    """Write a checkpoint via the same path the rotation helper uses."""
    from qwen_coder_mcp import agent_loop

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(agent_loop.serialize_agent_state(history)),
        encoding="utf-8",
    )
    return target


class TestCheckpointsDiffDispatch:
    def test_diff_no_args(self, fs_cfg: fs_tools.FsConfig) -> None:
        out, _ = dispatch_slash(
            parse_slash("/checkpoints diff"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[],
        )
        assert out == "usage: /checkpoints diff <N> [--inline]"

    def test_diff_no_snapshots(self, fs_cfg: fs_tools.FsConfig) -> None:
        out, _ = dispatch_slash(
            parse_slash("/checkpoints diff 1"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[_msg("user", "x")],
        )
        assert "no rotated checkpoints" in out

    def test_diff_index_out_of_range(self, fs_cfg: fs_tools.FsConfig) -> None:
        snap_dir = fs_cfg.root / ".agent" / "checkpoints"
        _write_snapshot(snap_dir / "agent_state-2024.json", [])
        out, _ = dispatch_slash(
            parse_slash("/checkpoints diff 5"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[_msg("user", "x")],
        )
        assert "out of range" in out

    def test_diff_invalid_index(self, fs_cfg: fs_tools.FsConfig) -> None:
        out, _ = dispatch_slash(
            parse_slash("/checkpoints diff abc"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[_msg("user", "x")],
        )
        assert "invalid index" in out

    def test_diff_renders_snapshot(self, fs_cfg: fs_tools.FsConfig) -> None:
        snap_dir = fs_cfg.root / ".agent" / "checkpoints"
        _write_snapshot(
            snap_dir / "agent_state-2024.json",
            [_msg("user", "old"), _msg("assistant", "yo")],
        )
        history = [_msg("user", "new"), _msg("assistant", "yo")]
        out, _ = dispatch_slash(
            parse_slash("/checkpoints diff 1"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=history,
        )
        assert "agent_state-2024.json" in out
        assert "changed=1" in out
        assert "same=1" in out

    def test_diff_no_history(self, fs_cfg: fs_tools.FsConfig) -> None:
        out, _ = dispatch_slash(
            parse_slash("/checkpoints diff 1"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=None,
        )
        assert "no history available" in out

    def test_unknown_subcommand_message_lists_diff(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        out, _ = dispatch_slash(
            parse_slash("/checkpoints zzz"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[],
        )
        assert "diff" in out
