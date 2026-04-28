"""Tests for ``rotate_agent_checkpoints`` and ``list_agent_checkpoints``
— the multi-snapshot variant of agent-state checkpointing introduced in
loop 179."""
from __future__ import annotations

import time
from pathlib import Path

from qwen_coder_mcp import agent_loop
from qwen_coder_mcp.qwen_client import ChatMessage


def _msg(content: str) -> ChatMessage:
    return ChatMessage(role="user", content=content)


class TestRotateAgentCheckpoints:
    def test_writes_primary_and_snapshot(self, tmp_path: Path) -> None:
        primary = tmp_path / ".agent" / "agent_state.json"
        snap = agent_loop.rotate_agent_checkpoints(primary, [_msg("hi")])
        assert primary.exists()
        assert snap.exists()
        assert snap.parent == primary.parent / "checkpoints"
        assert snap.name.startswith("agent_state-")
        assert snap.name.endswith(".json")

    def test_primary_holds_latest_history(self, tmp_path: Path) -> None:
        primary = tmp_path / ".agent" / "agent_state.json"
        agent_loop.rotate_agent_checkpoints(primary, [_msg("first")])
        agent_loop.rotate_agent_checkpoints(primary, [_msg("second")])
        loaded = agent_loop.load_agent_checkpoint(primary)
        assert [m.content for m in loaded] == ["second"]

    def test_keeps_only_last_n_snapshots(self, tmp_path: Path) -> None:
        primary = tmp_path / ".agent" / "agent_state.json"
        for i in range(8):
            agent_loop.rotate_agent_checkpoints(
                primary, [_msg(f"v{i}")], keep=3
            )
            # Sleep just enough for the microsecond-precision timestamp
            # to advance — sub-microsecond writes on a fast disk could
            # otherwise produce duplicate filenames.
            time.sleep(0.001)
        snaps = agent_loop.list_agent_checkpoints(primary)
        assert len(snaps) == 3

    def test_keep_zero_retains_all(self, tmp_path: Path) -> None:
        primary = tmp_path / ".agent" / "agent_state.json"
        for i in range(4):
            agent_loop.rotate_agent_checkpoints(
                primary, [_msg(f"v{i}")], keep=0
            )
            time.sleep(0.001)
        snaps = agent_loop.list_agent_checkpoints(primary)
        assert len(snaps) == 4

    def test_oldest_snapshots_pruned_first(self, tmp_path: Path) -> None:
        primary = tmp_path / ".agent" / "agent_state.json"
        for i in range(5):
            agent_loop.rotate_agent_checkpoints(
                primary, [_msg(f"v{i}")], keep=2
            )
            time.sleep(0.001)
        snaps = agent_loop.list_agent_checkpoints(primary)
        # The two surviving snapshots must be the most recent two: load
        # them and check their contents are v3 and v4.
        loaded_contents = [
            agent_loop.load_agent_checkpoint(s)[0].content for s in snaps
        ]
        assert loaded_contents == ["v3", "v4"]

    def test_snapshot_filenames_sort_chronologically(
        self, tmp_path: Path
    ) -> None:
        primary = tmp_path / ".agent" / "agent_state.json"
        names: list[str] = []
        for _ in range(3):
            snap = agent_loop.rotate_agent_checkpoints(
                primary, [_msg("x")], keep=10
            )
            names.append(snap.name)
            time.sleep(0.001)
        assert names == sorted(names)


class TestListAgentCheckpoints:
    def test_missing_directory_returns_empty(self, tmp_path: Path) -> None:
        primary = tmp_path / ".agent" / "agent_state.json"
        # Primary dir doesn't exist yet.
        assert agent_loop.list_agent_checkpoints(primary) == []

    def test_primary_only_no_rotations_returns_empty(
        self, tmp_path: Path
    ) -> None:
        primary = tmp_path / ".agent" / "agent_state.json"
        agent_loop.save_agent_checkpoint(primary, [_msg("solo")])
        # We saved primary but never rotated → checkpoints/ doesn't exist.
        assert agent_loop.list_agent_checkpoints(primary) == []

    def test_filters_to_matching_stem(self, tmp_path: Path) -> None:
        primary = tmp_path / ".agent" / "agent_state.json"
        agent_loop.rotate_agent_checkpoints(primary, [_msg("ours")])
        # Drop a foreign snapshot in the same dir; it must be ignored.
        rot = primary.parent / "checkpoints"
        (rot / "other_state-99999999T999999999999.json").write_text("{}")
        snaps = agent_loop.list_agent_checkpoints(primary)
        assert all(s.name.startswith("agent_state-") for s in snaps)
        assert len(snaps) == 1
