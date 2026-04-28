"""Tests for ``load_latest_checkpoint`` (loop 181) — the helper that
falls back from the primary state file to the newest rotated snapshot
when the primary is missing or corrupt."""
from __future__ import annotations

import time
from pathlib import Path

from qwen_coder_mcp import agent_loop
from qwen_coder_mcp.qwen_client import ChatMessage


def _msg(content: str) -> ChatMessage:
    return ChatMessage(role="user", content=content)


class TestLoadLatestCheckpoint:
    def test_primary_present_used(self, tmp_path: Path) -> None:
        primary = tmp_path / ".agent" / "agent_state.json"
        agent_loop.save_agent_checkpoint(primary, [_msg("primary")])
        history, source = agent_loop.load_latest_checkpoint(primary)
        assert [m.content for m in history] == ["primary"]
        assert source == primary

    def test_falls_back_to_newest_rotation(self, tmp_path: Path) -> None:
        primary = tmp_path / ".agent" / "agent_state.json"
        # Populate three rotations, then nuke the primary.
        for i in range(3):
            agent_loop.rotate_agent_checkpoints(
                primary, [_msg(f"v{i}")], keep=10
            )
            time.sleep(0.001)
        primary.unlink()
        history, source = agent_loop.load_latest_checkpoint(primary)
        assert [m.content for m in history] == ["v2"]  # newest
        assert source is not None and source.name.startswith("agent_state-")

    def test_returns_empty_when_nothing_present(
        self, tmp_path: Path
    ) -> None:
        primary = tmp_path / ".agent" / "agent_state.json"
        history, source = agent_loop.load_latest_checkpoint(primary)
        assert history == []
        assert source is None

    def test_skips_corrupt_primary(self, tmp_path: Path) -> None:
        primary = tmp_path / ".agent" / "agent_state.json"
        for i in range(2):
            agent_loop.rotate_agent_checkpoints(
                primary, [_msg(f"v{i}")], keep=10
            )
            time.sleep(0.001)
        # Trash the primary file with garbage JSON.
        primary.write_text("{not valid json", encoding="utf-8")
        history, source = agent_loop.load_latest_checkpoint(primary)
        assert [m.content for m in history] == ["v1"]
        assert source is not None and source != primary

    def test_skips_corrupt_newest_rotation(self, tmp_path: Path) -> None:
        primary = tmp_path / ".agent" / "agent_state.json"
        for i in range(3):
            agent_loop.rotate_agent_checkpoints(
                primary, [_msg(f"v{i}")], keep=10
            )
            time.sleep(0.001)
        primary.unlink()
        # Corrupt the newest rotation; helper must walk back further.
        snaps = agent_loop.list_agent_checkpoints(primary)
        snaps[-1].write_text("garbage", encoding="utf-8")
        history, source = agent_loop.load_latest_checkpoint(primary)
        assert [m.content for m in history] == ["v1"]
        assert source is not None

    def test_all_corrupt_returns_empty(self, tmp_path: Path) -> None:
        primary = tmp_path / ".agent" / "agent_state.json"
        for i in range(2):
            agent_loop.rotate_agent_checkpoints(
                primary, [_msg(f"v{i}")], keep=10
            )
            time.sleep(0.001)
        primary.write_text("garbage", encoding="utf-8")
        for snap in agent_loop.list_agent_checkpoints(primary):
            snap.write_text("garbage", encoding="utf-8")
        history, source = agent_loop.load_latest_checkpoint(primary)
        assert history == []
        assert source is None

    def test_empty_primary_falls_back(self, tmp_path: Path) -> None:
        # An empty-list checkpoint counts as "nothing useful here" —
        # callers should get the most recent rotation.
        primary = tmp_path / ".agent" / "agent_state.json"
        agent_loop.rotate_agent_checkpoints(
            primary, [_msg("rot")], keep=10
        )
        time.sleep(0.001)
        # Overwrite primary with a valid-but-empty history.
        agent_loop.save_agent_checkpoint(primary, [])
        history, source = agent_loop.load_latest_checkpoint(primary)
        assert [m.content for m in history] == ["rot"]
        assert source is not None and source != primary
