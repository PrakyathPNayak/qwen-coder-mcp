"""Loop 277 -- mkdir / touch write tools."""
from __future__ import annotations

from pathlib import Path

import pytest

from qwen_coder_mcp import fs_tools
from qwen_coder_mcp.agent_loop import (
    DESTRUCTIVE_TOOLS,
    TOOL_BLURBS,
    WRITE_TOOLS,
    _tool_mkdir,
    _tool_touch,
)


@pytest.fixture
def cfg(tmp_path: Path) -> fs_tools.FsConfig:
    return fs_tools.FsConfig(root=tmp_path)


class TestRegistration:
    def test_in_write_tools(self):
        assert "mkdir" in WRITE_TOOLS
        assert "touch" in WRITE_TOOLS

    def test_destructive(self):
        assert "mkdir" in DESTRUCTIVE_TOOLS
        assert "touch" in DESTRUCTIVE_TOOLS

    def test_blurbs(self):
        assert "mkdir" in TOOL_BLURBS
        assert "touch" in TOOL_BLURBS


class TestMkdir:
    def test_creates(self, cfg):
        out = _tool_mkdir({"path": "newdir"}, cfg)
        assert "created" in out
        assert (cfg.root / "newdir").is_dir()

    def test_nested_with_parents(self, cfg):
        out = _tool_mkdir({"path": "a/b/c"}, cfg)
        assert "created" in out
        assert (cfg.root / "a/b/c").is_dir()

    def test_no_parents_fails(self, cfg):
        out = _tool_mkdir({"path": "a/b/c", "parents": False}, cfg)
        assert out.startswith("error:")

    def test_exist_ok(self, cfg):
        (cfg.root / "x").mkdir()
        out = _tool_mkdir({"path": "x"}, cfg)
        assert "created" in out  # exist_ok=True default

    def test_exist_not_ok(self, cfg):
        (cfg.root / "x").mkdir()
        out = _tool_mkdir({"path": "x", "exist_ok": False}, cfg)
        assert "already exists" in out

    def test_missing_path(self, cfg):
        out = _tool_mkdir({}, cfg)
        assert out.startswith("error:")

    def test_escape_blocked(self, cfg):
        out = _tool_mkdir({"path": "../escaped"}, cfg)
        assert out.startswith("error:")
        assert not (cfg.root.parent / "escaped").exists()


class TestTouch:
    def test_creates_empty(self, cfg):
        out = _tool_touch({"path": "new.txt"}, cfg)
        assert "created empty" in out
        assert (cfg.root / "new.txt").read_text() == ""

    def test_existing_updates(self, cfg):
        (cfg.root / "x.txt").write_text("hi")
        out = _tool_touch({"path": "x.txt"}, cfg)
        assert "existing" in out
        assert (cfg.root / "x.txt").read_text() == "hi"  # unchanged

    def test_no_parents_fails(self, cfg):
        out = _tool_touch({"path": "missing/x.txt"}, cfg)
        assert out.startswith("error:")

    def test_create_parents(self, cfg):
        out = _tool_touch({"path": "deep/nested/x.txt", "create_parents": True}, cfg)
        assert "created" in out
        assert (cfg.root / "deep/nested/x.txt").exists()

    def test_missing_path(self, cfg):
        out = _tool_touch({}, cfg)
        assert out.startswith("error:")

    def test_escape_blocked(self, cfg):
        out = _tool_touch({"path": "../escape.txt"}, cfg)
        assert out.startswith("error:")
