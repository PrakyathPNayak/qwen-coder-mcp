"""Loop 193 — `--inline` per-message unified diff for `/checkpoints diff`."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from qwen_coder_mcp import agent_loop, fs_tools
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


def _write_snapshot(target: Path, history: list[ChatMessage]) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(agent_loop.serialize_agent_state(history)),
        encoding="utf-8",
    )
    return target


class TestInlineDiffRenderer:
    def test_inline_off_by_default(self) -> None:
        a = [_msg("user", "line1\nline2")]
        b = [_msg("user", "line1\nLINE2")]
        out = format_history_diff(a, b)
        # No unified-diff fragment lines.
        assert "---" not in out
        assert "+++" not in out

    def test_inline_emits_unified_diff(self) -> None:
        a = [_msg("user", "line1\nline2\nline3")]  # current
        b = [_msg("user", "line1\nLINE2\nline3")]  # snapshot
        out = format_history_diff(a, b, inline_diff=True)
        assert "---" in out
        assert "+++" in out
        # Snapshot (from) line removed, current (to) line added.
        assert "-LINE2" in out
        assert "+line2" in out

    def test_inline_only_for_changed_rows(self) -> None:
        a = [_msg("user", "same"), _msg("assistant", "old")]
        b = [_msg("user", "same"), _msg("assistant", "new")]
        out = format_history_diff(a, b, inline_diff=True)
        # Exactly one unified-diff block — for the changed row.
        assert out.count("---") == 1
        assert out.count("+++") == 1

    def test_inline_truncation(self) -> None:
        cur_text = "\n".join(f"line{i}" for i in range(50))
        snap_text = "\n".join(f"LINE{i}" for i in range(50))
        a = [_msg("user", cur_text)]
        b = [_msg("user", snap_text)]
        out = format_history_diff(
            a, b, inline_diff=True, inline_diff_max_lines=8
        )
        assert "diff truncated to 8 lines" in out

    def test_inline_no_truncation_when_under_cap(self) -> None:
        a = [_msg("user", "a\nb")]
        b = [_msg("user", "a\nB")]
        out = format_history_diff(
            a, b, inline_diff=True, inline_diff_max_lines=20
        )
        assert "truncated" not in out

    def test_inline_uses_snapshot_label(self) -> None:
        a = [_msg("user", "x")]
        b = [_msg("user", "y")]
        out = format_history_diff(
            a, b, snapshot_label="snap.json", inline_diff=True
        )
        assert "snap.json#1" in out

    def test_inline_skips_role_mismatch(self) -> None:
        a = [_msg("user", "hi")]
        b = [_msg("assistant", "yo")]
        out = format_history_diff(a, b, inline_diff=True)
        # Role-mismatch row, no unified-diff (different role, not "changed").
        assert "---" not in out


class TestInlineDispatch:
    def test_inline_flag_after_index(self, fs_cfg: fs_tools.FsConfig) -> None:
        snap_dir = fs_cfg.root / ".agent" / "checkpoints"
        _write_snapshot(
            snap_dir / "agent_state-2024.json",
            [_msg("user", "line1\nold")],
        )
        out, _ = dispatch_slash(
            parse_slash("/checkpoints diff 1 --inline"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[_msg("user", "line1\nnew")],
        )
        assert "+new" in out
        assert "-old" in out

    def test_inline_flag_before_index(self, fs_cfg: fs_tools.FsConfig) -> None:
        snap_dir = fs_cfg.root / ".agent" / "checkpoints"
        _write_snapshot(
            snap_dir / "agent_state-2024.json",
            [_msg("user", "old")],
        )
        out, _ = dispatch_slash(
            parse_slash("/checkpoints diff --inline 1"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[_msg("user", "new")],
        )
        assert "+new" in out
        assert "-old" in out

    def test_no_inline_no_unified_diff(self, fs_cfg: fs_tools.FsConfig) -> None:
        snap_dir = fs_cfg.root / ".agent" / "checkpoints"
        _write_snapshot(
            snap_dir / "agent_state-2024.json",
            [_msg("user", "old")],
        )
        out, _ = dispatch_slash(
            parse_slash("/checkpoints diff 1"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[_msg("user", "new")],
        )
        # Plain summary still produced; no unified-diff.
        assert "changed=1" in out
        assert "---" not in out

    def test_inline_alone_without_index_errors(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        out, _ = dispatch_slash(
            parse_slash("/checkpoints diff --inline"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[_msg("user", "x")],
        )
        assert "usage:" in out
