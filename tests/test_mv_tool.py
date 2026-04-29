"""Loop 278 -- mv (rename/move) write tool."""
from __future__ import annotations

from pathlib import Path

import pytest

from qwen_coder_mcp import fs_tools
from qwen_coder_mcp.agent_loop import (
    DESTRUCTIVE_TOOLS,
    TOOL_BLURBS,
    WRITE_TOOLS,
    _tool_mv,
)


@pytest.fixture
def cfg(tmp_path: Path) -> fs_tools.FsConfig:
    (tmp_path / "src.txt").write_text("hello")
    (tmp_path / "dir").mkdir()
    (tmp_path / "dir/inner.txt").write_text("x")
    return fs_tools.FsConfig(root=tmp_path)


class TestRegistration:
    def test_in_write_tools(self):
        assert "mv" in WRITE_TOOLS
        assert "mv" in DESTRUCTIVE_TOOLS
        assert "mv" in TOOL_BLURBS


class TestMv:
    def test_rename_file(self, cfg):
        out = _tool_mv({"src": "src.txt", "dst": "renamed.txt"}, cfg)
        assert "moved" in out
        assert (cfg.root / "renamed.txt").read_text() == "hello"
        assert not (cfg.root / "src.txt").exists()

    def test_rename_dir(self, cfg):
        out = _tool_mv({"src": "dir", "dst": "moved-dir"}, cfg)
        assert "moved" in out
        assert (cfg.root / "moved-dir/inner.txt").read_text() == "x"

    def test_create_parent_dirs(self, cfg):
        out = _tool_mv({"src": "src.txt", "dst": "deep/nest/x.txt"}, cfg)
        assert "moved" in out
        assert (cfg.root / "deep/nest/x.txt").read_text() == "hello"

    def test_overwrite_default_false(self, cfg):
        (cfg.root / "tgt.txt").write_text("existing")
        out = _tool_mv({"src": "src.txt", "dst": "tgt.txt"}, cfg)
        assert out.startswith("error:")
        assert (cfg.root / "tgt.txt").read_text() == "existing"

    def test_overwrite_true_file(self, cfg):
        (cfg.root / "tgt.txt").write_text("existing")
        out = _tool_mv({"src": "src.txt", "dst": "tgt.txt", "overwrite": True}, cfg)
        assert "moved" in out
        assert (cfg.root / "tgt.txt").read_text() == "hello"

    def test_overwrite_true_dir(self, cfg):
        (cfg.root / "tgt").mkdir()
        (cfg.root / "tgt/old.txt").write_text("old")
        out = _tool_mv({"src": "dir", "dst": "tgt", "overwrite": True}, cfg)
        assert "moved" in out
        assert (cfg.root / "tgt/inner.txt").exists()
        assert not (cfg.root / "tgt/old.txt").exists()

    def test_missing_args(self, cfg):
        assert _tool_mv({"src": "src.txt"}, cfg).startswith("error:")
        assert _tool_mv({"dst": "x.txt"}, cfg).startswith("error:")

    def test_src_not_found(self, cfg):
        out = _tool_mv({"src": "nope", "dst": "x"}, cfg)
        assert "not found" in out

    def test_escape_blocked_src(self, cfg):
        out = _tool_mv({"src": "../escape", "dst": "x"}, cfg)
        assert out.startswith("error:")

    def test_escape_blocked_dst(self, cfg):
        out = _tool_mv({"src": "src.txt", "dst": "../escape"}, cfg)
        assert out.startswith("error:")
