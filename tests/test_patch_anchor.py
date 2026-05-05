"""Tests for the new fs_tools.patch_anchor helper."""
from __future__ import annotations

from pathlib import Path

import pytest

from qwen_coder_mcp import fs_tools


@pytest.fixture
def cfg(tmp_path: Path) -> fs_tools.FsConfig:
    return fs_tools.FsConfig(root=tmp_path)


def test_replaces_unique_match(cfg: fs_tools.FsConfig) -> None:
    p = cfg.root / "a.py"
    p.write_text("x = 1\nprint(x)\n", encoding="utf-8")
    res = fs_tools.patch_anchor(cfg, "a.py", "x = 1", "x = 42")
    assert res["replaced"] == 1
    assert p.read_text(encoding="utf-8") == "x = 42\nprint(x)\n"
    assert res["size_before"] == len("x = 1\nprint(x)\n".encode())
    assert res["size_after"] == len("x = 42\nprint(x)\n".encode())


def test_rejects_zero_matches(cfg: fs_tools.FsConfig) -> None:
    (cfg.root / "a.py").write_text("hello\n", encoding="utf-8")
    with pytest.raises(fs_tools.FsError, match="not found"):
        fs_tools.patch_anchor(cfg, "a.py", "missing", "x")


def test_rejects_multiple_matches(cfg: fs_tools.FsConfig) -> None:
    (cfg.root / "a.py").write_text("x x x\n", encoding="utf-8")
    with pytest.raises(fs_tools.FsError, match="matches 3 times"):
        fs_tools.patch_anchor(cfg, "a.py", "x", "y")


def test_rejects_empty_old_str(cfg: fs_tools.FsConfig) -> None:
    (cfg.root / "a.py").write_text("a\n", encoding="utf-8")
    with pytest.raises(fs_tools.FsError, match="non-empty"):
        fs_tools.patch_anchor(cfg, "a.py", "", "y")


def test_rejects_noop(cfg: fs_tools.FsConfig) -> None:
    (cfg.root / "a.py").write_text("a\n", encoding="utf-8")
    with pytest.raises(fs_tools.FsError, match="identical"):
        fs_tools.patch_anchor(cfg, "a.py", "a", "a")


def test_rejects_missing_file(cfg: fs_tools.FsConfig) -> None:
    with pytest.raises(fs_tools.FsError, match="not found"):
        fs_tools.patch_anchor(cfg, "missing.py", "a", "b")


def test_rejects_directory(cfg: fs_tools.FsConfig) -> None:
    (cfg.root / "sub").mkdir()
    with pytest.raises(fs_tools.FsError, match="directory"):
        fs_tools.patch_anchor(cfg, "sub", "a", "b")


def test_rejects_path_escape(cfg: fs_tools.FsConfig) -> None:
    with pytest.raises(fs_tools.FsError, match="escapes"):
        fs_tools.patch_anchor(cfg, "../etc/passwd", "x", "y")


def test_rejects_oversized_result(tmp_path: Path) -> None:
    cfg = fs_tools.FsConfig(root=tmp_path, max_write_bytes=10)
    p = tmp_path / "a.txt"
    p.write_text("hi", encoding="utf-8")
    with pytest.raises(fs_tools.FsError, match="too large"):
        fs_tools.patch_anchor(cfg, "a.txt", "hi", "y" * 100)


def test_handles_multiline_anchor(cfg: fs_tools.FsConfig) -> None:
    body = "def f():\n    return 1\n\ndef g():\n    return 2\n"
    (cfg.root / "m.py").write_text(body, encoding="utf-8")
    res = fs_tools.patch_anchor(
        cfg, "m.py", "def f():\n    return 1\n", "def f():\n    return 11\n"
    )
    assert res["replaced"] == 1
    assert (
        (cfg.root / "m.py").read_text(encoding="utf-8")
        == "def f():\n    return 11\n\ndef g():\n    return 2\n"
    )


def test_no_partial_write_on_failure(cfg: fs_tools.FsConfig) -> None:
    """If the resolve fails the original file must be untouched."""
    (cfg.root / "a.txt").write_text("orig", encoding="utf-8")
    with pytest.raises(fs_tools.FsError):
        fs_tools.patch_anchor(cfg, "a.txt", "missing", "x")
    assert (cfg.root / "a.txt").read_text(encoding="utf-8") == "orig"
    assert not (cfg.root / "a.txt.tmp").exists()
