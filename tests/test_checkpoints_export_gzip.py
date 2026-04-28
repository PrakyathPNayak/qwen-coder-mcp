"""Loop 269 — `/checkpoints export N <path> --gzip` writes a compressed copy."""
from __future__ import annotations

import gzip
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


class TestCheckpointsExportGzip:
    def test_gzip_flag_writes_compressed_file(self, fs_cfg):
        snap_dir = fs_cfg.root / ".agent" / "checkpoints"
        src = _write_snapshot(
            snap_dir / "agent_state-2024.json",
            [_msg("user", "hi" * 200), _msg("assistant", "yo" * 200)],
        )
        out, _ = dispatch_slash(
            parse_slash("/checkpoints export 1 archive.json --gzip"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[],
        )
        # Auto-suffixed .gz
        dest = fs_cfg.root / "archive.json.gz"
        assert dest.exists()
        # Decompressed equals source bytes
        assert gzip.decompress(dest.read_bytes()) == src.read_bytes()
        assert "exported" in out and "gzip" in out

    def test_gzip_flag_does_not_double_suffix(self, fs_cfg):
        snap_dir = fs_cfg.root / ".agent" / "checkpoints"
        src = _write_snapshot(
            snap_dir / "agent_state-2024.json", [_msg("user", "a")]
        )
        out, _ = dispatch_slash(
            parse_slash("/checkpoints export 1 archive.json.gz --gzip"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[],
        )
        dest = fs_cfg.root / "archive.json.gz"
        not_double = fs_cfg.root / "archive.json.gz.gz"
        assert dest.exists()
        assert not not_double.exists()
        assert gzip.decompress(dest.read_bytes()) == src.read_bytes()

    def test_no_gzip_keeps_byte_identical_copy(self, fs_cfg):
        # Backward compat — flag absent must behave exactly like before.
        snap_dir = fs_cfg.root / ".agent" / "checkpoints"
        src = _write_snapshot(
            snap_dir / "agent_state-2024.json", [_msg("user", "abc")]
        )
        dispatch_slash(
            parse_slash("/checkpoints export 1 archive.json"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[],
        )
        dest = fs_cfg.root / "archive.json"
        assert dest.read_bytes() == src.read_bytes()

    def test_gzip_compresses_repetitive_content(self, fs_cfg):
        # Sanity: gzipped output should be smaller than the source for
        # highly repetitive data. Catches a regression where --gzip
        # writes raw bytes by mistake.
        snap_dir = fs_cfg.root / ".agent" / "checkpoints"
        big = "A" * 50000
        src = _write_snapshot(
            snap_dir / "agent_state-2024.json", [_msg("user", big)]
        )
        dispatch_slash(
            parse_slash("/checkpoints export 1 archive.json --gzip"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[],
        )
        dest = fs_cfg.root / "archive.json.gz"
        assert dest.exists()
        assert dest.stat().st_size < src.stat().st_size // 2

    def test_gzip_flag_anywhere_in_args(self, fs_cfg):
        # Tolerate `--gzip` before path arg too.
        snap_dir = fs_cfg.root / ".agent" / "checkpoints"
        _write_snapshot(snap_dir / "agent_state-2024.json", [_msg("user", "x")])
        dispatch_slash(
            parse_slash("/checkpoints export --gzip 1 archive.json"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[],
        )
        assert (fs_cfg.root / "archive.json.gz").exists()

    def test_gzip_path_escape_rejected(self, fs_cfg):
        snap_dir = fs_cfg.root / ".agent" / "checkpoints"
        _write_snapshot(snap_dir / "agent_state-2024.json", [_msg("user", "x")])
        out, _ = dispatch_slash(
            parse_slash("/checkpoints export 1 ../escape.json --gzip"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[],
        )
        assert "export failed" in out
        assert not (fs_cfg.root.parent / "escape.json.gz").exists()

    def test_gzip_no_snapshots(self, fs_cfg):
        out, _ = dispatch_slash(
            parse_slash("/checkpoints export 1 archive.json --gzip"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[],
        )
        assert "no rotated checkpoints" in out

    def test_gzip_reports_compressed_byte_count(self, fs_cfg):
        # The output line should report the bytes ACTUALLY written
        # (compressed), not the source size — otherwise users can't
        # eyeball the savings.
        snap_dir = fs_cfg.root / ".agent" / "checkpoints"
        _write_snapshot(
            snap_dir / "agent_state-2024.json",
            [_msg("user", "B" * 100000)],
        )
        out, _ = dispatch_slash(
            parse_slash("/checkpoints export 1 archive.json --gzip"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[],
        )
        dest = fs_cfg.root / "archive.json.gz"
        # Find the byte count in output
        assert f"({dest.stat().st_size} bytes)" in out

    def test_help_documents_gzip_flag(self):
        from qwen_coder_mcp.tui import HELP_TEXT
        assert "--gzip" in HELP_TEXT
