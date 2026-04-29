"""Loop 275 -- file_info read-only stat tool."""
from __future__ import annotations

from pathlib import Path

import pytest

from qwen_coder_mcp import fs_tools
from qwen_coder_mcp.agent_loop import (
    DEFAULT_TOOLS,
    DESTRUCTIVE_TOOLS,
    TOOL_BLURBS,
    _tool_file_info,
)


@pytest.fixture
def cfg(tmp_path: Path) -> fs_tools.FsConfig:
    (tmp_path / "a.txt").write_text("hello\nworld\n")
    (tmp_path / "sub").mkdir()
    return fs_tools.FsConfig(root=tmp_path)


def test_registered_and_safe():
    assert "file_info" in DEFAULT_TOOLS
    assert "file_info" not in DESTRUCTIVE_TOOLS
    assert "file_info" in TOOL_BLURBS


def test_file(cfg):
    out = _tool_file_info({"path": "a.txt"}, cfg)
    assert "kind: file" in out
    assert "size: 12" in out
    assert "mode: 0o" in out
    assert "mtime:" in out


def test_dir(cfg):
    out = _tool_file_info({"path": "sub"}, cfg)
    assert "kind: dir" in out


def test_missing_path(cfg):
    out = _tool_file_info({}, cfg)
    assert out.startswith("error:")


def test_not_found(cfg):
    out = _tool_file_info({"path": "nope.txt"}, cfg)
    assert "not found" in out


def test_escape_blocked(cfg):
    out = _tool_file_info({"path": "../../../etc/passwd"}, cfg)
    assert out.startswith("error:")


def test_sha256_optional(cfg):
    out = _tool_file_info({"path": "a.txt", "sha256": True}, cfg)
    assert "sha256:" in out
    # sha256 of "hello\nworld\n" -- computed at runtime, just sanity-check format
    sha_line = [ln for ln in out.splitlines() if ln.startswith("sha256:")][0]
    assert len(sha_line.split(": ", 1)[1]) == 64


def test_sha256_off_by_default(cfg):
    out = _tool_file_info({"path": "a.txt"}, cfg)
    assert "sha256:" not in out
