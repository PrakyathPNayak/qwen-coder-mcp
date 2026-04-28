"""Loop 199 — `/checkpoints export N <path>` archives a snapshot."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from qwen_coder_mcp import agent_loop, fs_tools
from qwen_coder_mcp.tui import ChatMessage, dispatch_slash, parse_slash


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


class TestCheckpointsExport:
    def test_usage_when_args_short(self, fs_cfg: fs_tools.FsConfig) -> None:
        out, _ = dispatch_slash(
            parse_slash("/checkpoints export 1"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[],
        )
        assert "usage:" in out and "export" in out

    def test_no_snapshots(self, fs_cfg: fs_tools.FsConfig) -> None:
        out, _ = dispatch_slash(
            parse_slash("/checkpoints export 1 archive.json"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[],
        )
        assert "no rotated checkpoints" in out

    def test_invalid_index(self, fs_cfg: fs_tools.FsConfig) -> None:
        out, _ = dispatch_slash(
            parse_slash("/checkpoints export abc archive.json"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[],
        )
        assert "invalid index" in out

    def test_index_out_of_range(self, fs_cfg: fs_tools.FsConfig) -> None:
        snap_dir = fs_cfg.root / ".agent" / "checkpoints"
        _write_snapshot(snap_dir / "agent_state-2024.json", [_msg("user", "a")])
        out, _ = dispatch_slash(
            parse_slash("/checkpoints export 9 archive.json"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[],
        )
        assert "out of range" in out

    def test_export_writes_byte_identical_copy(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        snap_dir = fs_cfg.root / ".agent" / "checkpoints"
        src = _write_snapshot(
            snap_dir / "agent_state-2024.json",
            [_msg("user", "hi"), _msg("assistant", "yo")],
        )
        out, _ = dispatch_slash(
            parse_slash("/checkpoints export 1 archive.json"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[],
        )
        dest = fs_cfg.root / "archive.json"
        assert dest.exists()
        assert dest.read_bytes() == src.read_bytes()
        assert "agent_state-2024.json" in out
        assert "exported" in out

    def test_export_path_escape_rejected(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        snap_dir = fs_cfg.root / ".agent" / "checkpoints"
        _write_snapshot(snap_dir / "agent_state-2024.json", [_msg("user", "a")])
        out, _ = dispatch_slash(
            parse_slash("/checkpoints export 1 ../escape.json"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[],
        )
        assert "export failed" in out
        # And the file must not exist outside root.
        assert not (fs_cfg.root.parent / "escape.json").exists()

    def test_export_does_not_mutate_history(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        snap_dir = fs_cfg.root / ".agent" / "checkpoints"
        _write_snapshot(
            snap_dir / "agent_state-2024.json",
            [_msg("user", "old"), _msg("assistant", "old-reply")],
        )
        history = [_msg("user", "current"), _msg("assistant", "current-reply")]
        before = list(history)
        dispatch_slash(
            parse_slash("/checkpoints export 1 archive.json"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=history,
        )
        assert history == before

    def test_export_creates_parent_dir(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        snap_dir = fs_cfg.root / ".agent" / "checkpoints"
        _write_snapshot(snap_dir / "agent_state-2024.json", [_msg("user", "a")])
        dispatch_slash(
            parse_slash("/checkpoints export 1 archives/by-date/snap.json"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[],
        )
        dest = fs_cfg.root / "archives" / "by-date" / "snap.json"
        assert dest.exists()

    def test_unknown_subcommand_lists_export(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        out, _ = dispatch_slash(
            parse_slash("/checkpoints frobnicate"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[],
        )
        assert "export" in out

    def test_export_leaves_no_tmp_file(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        snap_dir = fs_cfg.root / ".agent" / "checkpoints"
        _write_snapshot(snap_dir / "agent_state-2024.json", [_msg("user", "a")])
        dispatch_slash(
            parse_slash("/checkpoints export 1 archive.json"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[],
        )
        # No leftover .tmp.
        leftover = list(fs_cfg.root.glob("*.tmp"))
        assert leftover == []

    def test_export_overwrites_existing(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        snap_dir = fs_cfg.root / ".agent" / "checkpoints"
        _write_snapshot(
            snap_dir / "agent_state-2024.json",
            [_msg("user", "snapshot-content")],
        )
        existing = fs_cfg.root / "archive.json"
        existing.write_text("STALE", encoding="utf-8")
        dispatch_slash(
            parse_slash("/checkpoints export 1 archive.json"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[],
        )
        # Should now contain the snapshot's serialized history, not STALE.
        text = existing.read_text(encoding="utf-8")
        assert "STALE" not in text
        assert "snapshot-content" in text
