"""Tests for ``/checkpoints`` slash-command behaviour added in loop 180.

Covers the listing renderer (`_format_checkpoint_listing`), then the
three forms of the `/checkpoints` dispatcher branch: bare listing,
`load N` rehydration into history (in-place mutation), and `prune K`
deletion of older snapshots.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from qwen_coder_mcp import agent_loop, fs_tools
from qwen_coder_mcp.qwen_client import ChatMessage
from qwen_coder_mcp.tui import (
    _format_checkpoint_listing,
    dispatch_slash,
    parse_slash,
)


def _msg(role: str, content: str) -> ChatMessage:
    return ChatMessage(role=role, content=content)


def _populate(root: Path, count: int) -> Path:
    target = root / ".agent" / "agent_state.json"
    for i in range(count):
        agent_loop.rotate_agent_checkpoints(
            target, [_msg("user", f"v{i}")], keep=count
        )
        time.sleep(0.001)
    return target


@pytest.fixture()
def fs_cfg(tmp_path: Path) -> fs_tools.FsConfig:
    return fs_tools.FsConfig(root=tmp_path)


class TestFormatCheckpointListing:
    def test_empty_returns_placeholder(self) -> None:
        out = _format_checkpoint_listing([])
        assert "no rotated checkpoints" in out

    def test_lists_with_index_and_size(self, tmp_path: Path) -> None:
        target = _populate(tmp_path, 3)
        snaps = agent_loop.list_agent_checkpoints(target)
        out = _format_checkpoint_listing(snaps)
        assert "  1." in out and "  2." in out and "  3." in out
        assert "agent_state-" in out
        assert "B  " in out  # size column suffix


class TestCheckpointsBare:
    def test_bare_lists_snapshots(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        _populate(fs_cfg.root, 2)
        out, quit_now = dispatch_slash(
            parse_slash("/checkpoints"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[],
        )
        assert quit_now is False
        assert out.count("agent_state-") == 2

    def test_bare_with_no_rotations(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        out, _ = dispatch_slash(
            parse_slash("/checkpoints"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[],
        )
        assert "no rotated checkpoints" in out


class TestCheckpointsLoad:
    def test_load_replaces_history_in_place(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        _populate(fs_cfg.root, 3)
        history: list[ChatMessage] = [_msg("user", "stale")]
        original_id = id(history)
        out, _ = dispatch_slash(
            parse_slash("/checkpoints load 1"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=history,
        )
        assert id(history) == original_id  # mutated in place
        assert len(history) == 1
        assert history[0].content == "v0"  # oldest first
        assert "loaded" in out

    def test_load_picks_newest_when_index_is_last(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        _populate(fs_cfg.root, 4)
        history: list[ChatMessage] = []
        dispatch_slash(
            parse_slash("/checkpoints load 4"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=history,
        )
        assert history[0].content == "v3"

    def test_load_missing_index_arg(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        out, _ = dispatch_slash(
            parse_slash("/checkpoints load"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[],
        )
        assert "usage" in out.lower()

    def test_load_non_integer_index(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        _populate(fs_cfg.root, 1)
        out, _ = dispatch_slash(
            parse_slash("/checkpoints load oops"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[],
        )
        assert "invalid" in out.lower()

    def test_load_out_of_range(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        _populate(fs_cfg.root, 2)
        out, _ = dispatch_slash(
            parse_slash("/checkpoints load 99"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[],
        )
        assert "out of range" in out

    def test_load_with_no_snapshots(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        out, _ = dispatch_slash(
            parse_slash("/checkpoints load 1"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[],
        )
        assert "no rotated checkpoints" in out


class TestCheckpointsPrune:
    def test_prune_keeps_newest_n(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        target = _populate(fs_cfg.root, 5)
        out, _ = dispatch_slash(
            parse_slash("/checkpoints prune 2"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[],
        )
        remaining = agent_loop.list_agent_checkpoints(target)
        assert len(remaining) == 2
        # The two retained snapshots must be the newest two.
        loaded = [
            agent_loop.load_agent_checkpoint(s)[0].content for s in remaining
        ]
        assert loaded == ["v3", "v4"]
        assert "pruned 3" in out

    def test_prune_zero_deletes_all(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        target = _populate(fs_cfg.root, 3)
        dispatch_slash(
            parse_slash("/checkpoints prune 0"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[],
        )
        assert agent_loop.list_agent_checkpoints(target) == []

    def test_prune_negative_rejected(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        out, _ = dispatch_slash(
            parse_slash("/checkpoints prune -1"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[],
        )
        assert ">= 0" in out

    def test_prune_missing_arg(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        out, _ = dispatch_slash(
            parse_slash("/checkpoints prune"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[],
        )
        assert "usage" in out.lower()


class TestCheckpointsUnknownSub:
    def test_unknown_subcommand(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        out, _ = dispatch_slash(
            parse_slash("/checkpoints zap 1"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[],
        )
        assert "unknown subcommand" in out


class TestRegistryWiring:
    def test_command_in_slash_commands(self) -> None:
        from qwen_coder_mcp.tui import SLASH_COMMANDS

        assert "/checkpoints" in SLASH_COMMANDS

    def test_command_in_help_text(self) -> None:
        from qwen_coder_mcp.tui import HELP_TEXT

        assert "/checkpoints" in HELP_TEXT
