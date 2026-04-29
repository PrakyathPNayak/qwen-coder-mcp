"""Loop 274 -- git_status / git_diff / git_log read-only tools."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from qwen_coder_mcp import fs_tools
from qwen_coder_mcp.agent_loop import (
    DEFAULT_TOOLS,
    DESTRUCTIVE_TOOLS,
    TOOL_BLURBS,
    _tool_git_diff,
    _tool_git_log,
    _tool_git_status,
)


@pytest.fixture
def repo(tmp_path: Path) -> fs_tools.FsConfig:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "a.txt").write_text("hello\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init"],
        cwd=tmp_path,
        check=True,
    )
    return fs_tools.FsConfig(root=tmp_path)


class TestRegistration:
    def test_in_default_tools(self):
        assert "git_status" in DEFAULT_TOOLS
        assert "git_diff" in DEFAULT_TOOLS
        assert "git_log" in DEFAULT_TOOLS

    def test_not_destructive(self):
        for name in ("git_status", "git_diff", "git_log"):
            assert name not in DESTRUCTIVE_TOOLS

    def test_blurbs_present(self):
        for name in ("git_status", "git_diff", "git_log"):
            assert name in TOOL_BLURBS


class TestGitTools:
    def test_status_clean(self, repo):
        out = _tool_git_status({}, repo)
        assert "branch" in out.lower() or "##" in out

    def test_status_dirty(self, repo):
        (repo.root / "a.txt").write_text("hello\nworld\n")
        out = _tool_git_status({}, repo)
        assert "a.txt" in out

    def test_diff_unstaged(self, repo):
        (repo.root / "a.txt").write_text("hello\nworld\n")
        out = _tool_git_diff({}, repo)
        assert "+world" in out

    def test_diff_path_scoped(self, repo):
        (repo.root / "a.txt").write_text("hello\nworld\n")
        (repo.root / "b.txt").write_text("other\n")
        subprocess.run(["git", "add", "b.txt"], cwd=repo.root, check=True)
        out = _tool_git_diff({"path": "a.txt"}, repo)
        assert "a.txt" in out
        assert "b.txt" not in out

    def test_log_default(self, repo):
        out = _tool_git_log({}, repo)
        assert "init" in out

    def test_log_n_clamped(self, repo):
        out = _tool_git_log({"n": 99999}, repo)
        # Should not error; cap at 200, only 1 commit anyway.
        assert "init" in out

    def test_log_invalid_n(self, repo):
        out = _tool_git_log({"n": "abc"}, repo)
        assert "init" in out
