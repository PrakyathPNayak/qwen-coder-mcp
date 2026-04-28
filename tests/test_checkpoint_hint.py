"""Tests for ``render_checkpoint_hint`` (loop 183) — the boot-time
helper that surfaces ``/resume`` as a recovery affordance when the
JSONL history is empty but an agent checkpoint exists."""
from __future__ import annotations

from pathlib import Path

import pytest

from qwen_coder_mcp import agent_loop, fs_tools
from qwen_coder_mcp.qwen_client import ChatMessage
from qwen_coder_mcp.tui import render_checkpoint_hint


def _msg(content: str) -> ChatMessage:
    return ChatMessage(role="user", content=content)


@pytest.fixture()
def fs_cfg(tmp_path: Path) -> fs_tools.FsConfig:
    return fs_tools.FsConfig(root=tmp_path)


class TestRenderCheckpointHint:
    def test_no_checkpoint_returns_none(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        assert render_checkpoint_hint(fs_cfg) is None

    def test_primary_checkpoint_renders_hint(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        target = fs_cfg.root / ".agent" / "agent_state.json"
        agent_loop.save_agent_checkpoint(target, [_msg("a"), _msg("b")])
        hint = render_checkpoint_hint(fs_cfg)
        assert hint is not None
        assert "/resume" in hint
        assert "2 messages" in hint
        assert "agent_state.json" in hint

    def test_rotation_only_still_produces_hint(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        target = fs_cfg.root / ".agent" / "agent_state.json"
        agent_loop.rotate_agent_checkpoints(target, [_msg("rot")], keep=10)
        # Drop the primary so only the rotation remains.
        target.unlink()
        hint = render_checkpoint_hint(fs_cfg)
        assert hint is not None
        assert "/resume" in hint
        assert "agent_state-" in hint  # rotation filename

    def test_empty_checkpoint_returns_none(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        target = fs_cfg.root / ".agent" / "agent_state.json"
        agent_loop.save_agent_checkpoint(target, [])
        # Empty primary AND no rotations -> no hint.
        assert render_checkpoint_hint(fs_cfg) is None

    def test_corrupt_checkpoint_returns_none(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        target = fs_cfg.root / ".agent" / "agent_state.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("not json", encoding="utf-8")
        # No rotations either, so the helper has nothing to fall back to.
        assert render_checkpoint_hint(fs_cfg) is None
