"""Loop 282: diff_files read-only tool tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from qwen_coder_mcp import agent_loop, fs_tools


@pytest.fixture
def cfg(tmp_path: Path) -> fs_tools.FsConfig:
    return fs_tools.FsConfig(root=tmp_path)


def test_identical_files(tmp_path: Path, cfg: fs_tools.FsConfig) -> None:
    (tmp_path / "a.txt").write_text("hello\n")
    (tmp_path / "b.txt").write_text("hello\n")
    out = agent_loop._tool_diff_files({"a": "a.txt", "b": "b.txt"}, cfg)
    assert out == "files are identical"


def test_simple_diff(tmp_path: Path, cfg: fs_tools.FsConfig) -> None:
    (tmp_path / "a.txt").write_text("one\ntwo\nthree\n")
    (tmp_path / "b.txt").write_text("one\nTWO\nthree\n")
    out = agent_loop._tool_diff_files({"a": "a.txt", "b": "b.txt"}, cfg)
    assert "--- a.txt" in out
    assert "+++ b.txt" in out
    assert "-two" in out
    assert "+TWO" in out


def test_missing_a(tmp_path: Path, cfg: fs_tools.FsConfig) -> None:
    (tmp_path / "b.txt").write_text("x\n")
    out = agent_loop._tool_diff_files({"a": "ghost.txt", "b": "b.txt"}, cfg)
    assert out.startswith("error:")
    assert "ghost.txt" in out


def test_missing_b(tmp_path: Path, cfg: fs_tools.FsConfig) -> None:
    (tmp_path / "a.txt").write_text("x\n")
    out = agent_loop._tool_diff_files({"a": "a.txt", "b": "ghost.txt"}, cfg)
    assert out.startswith("error:")


def test_missing_args(cfg: fs_tools.FsConfig) -> None:
    assert agent_loop._tool_diff_files({}, cfg).startswith("error:")
    assert agent_loop._tool_diff_files({"a": "x"}, cfg).startswith("error:")


def test_dir_rejected(tmp_path: Path, cfg: fs_tools.FsConfig) -> None:
    (tmp_path / "d").mkdir()
    (tmp_path / "a.txt").write_text("x\n")
    out = agent_loop._tool_diff_files({"a": "a.txt", "b": "d"}, cfg)
    assert out.startswith("error:") and "not a file" in out


def test_escape_blocked(tmp_path: Path, cfg: fs_tools.FsConfig) -> None:
    out = agent_loop._tool_diff_files(
        {"a": "../etc/passwd", "b": "../etc/hostname"}, cfg
    )
    assert out.startswith("error:")


def test_too_large(tmp_path: Path) -> None:
    cfg = fs_tools.FsConfig(root=tmp_path, max_read_bytes=20)
    (tmp_path / "a.txt").write_text("x" * 50)
    (tmp_path / "b.txt").write_text("y" * 50)
    out = agent_loop._tool_diff_files({"a": "a.txt", "b": "b.txt"}, cfg)
    assert out.startswith("error:") and "too large" in out


def test_context_clamped(tmp_path: Path, cfg: fs_tools.FsConfig) -> None:
    (tmp_path / "a.txt").write_text("\n".join(f"line{i}" for i in range(20)) + "\n")
    (tmp_path / "b.txt").write_text("\n".join(f"line{i}" for i in range(20)).replace("line10", "LINE10") + "\n")
    out = agent_loop._tool_diff_files(
        {"a": "a.txt", "b": "b.txt", "context": 999}, cfg
    )
    assert "-line10" in out and "+LINE10" in out


def test_negative_context_clamped(tmp_path: Path, cfg: fs_tools.FsConfig) -> None:
    (tmp_path / "a.txt").write_text("a\n")
    (tmp_path / "b.txt").write_text("b\n")
    out = agent_loop._tool_diff_files(
        {"a": "a.txt", "b": "b.txt", "context": -5}, cfg
    )
    assert "-a" in out and "+b" in out


def test_in_default_registry() -> None:
    assert "diff_files" in agent_loop.DEFAULT_TOOLS
    assert agent_loop.DEFAULT_TOOLS["diff_files"] is agent_loop._tool_diff_files


def test_in_tool_blurbs() -> None:
    assert "diff_files" in agent_loop.TOOL_BLURBS
    assert "unified diff" in agent_loop.TOOL_BLURBS["diff_files"].lower()


def test_not_destructive() -> None:
    assert "diff_files" not in agent_loop.DESTRUCTIVE_TOOLS
