"""Loop 129 tests: filesystem MCP tools."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from qwen_coder_mcp import fs_tools


def _cfg(root: Path, **kw) -> fs_tools.FsConfig:
    return fs_tools.FsConfig(root=root, **kw)


class TestResolveInsideRoot:
    def test_simple(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)
        p = fs_tools._resolve_inside_root(cfg, "a.txt")
        assert p == (tmp_path / "a.txt").resolve()

    def test_rejects_parent_escape(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)
        with pytest.raises(fs_tools.FsError, match="escapes"):
            fs_tools._resolve_inside_root(cfg, "../etc/passwd")

    def test_rejects_absolute_outside(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)
        with pytest.raises(fs_tools.FsError, match="escapes"):
            fs_tools._resolve_inside_root(cfg, "/etc/passwd")

    def test_rejects_symlink_escape(self, tmp_path: Path) -> None:
        outside = tmp_path.parent / "outside_target"
        outside.mkdir(exist_ok=True)
        link = tmp_path / "link"
        os.symlink(outside, link)
        cfg = _cfg(tmp_path)
        with pytest.raises(fs_tools.FsError, match="escapes"):
            fs_tools._resolve_inside_root(cfg, "link/file")

    def test_rejects_empty(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)
        with pytest.raises(fs_tools.FsError):
            fs_tools._resolve_inside_root(cfg, "")


class TestReadFile:
    def test_reads_utf8(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
        res = fs_tools.read_file(_cfg(tmp_path), "a.txt")
        assert res["text"] == "hello"
        assert res["truncated"] is False
        assert res["size"] == 5

    def test_truncates(self, tmp_path: Path) -> None:
        (tmp_path / "big.txt").write_text("x" * 1000, encoding="utf-8")
        res = fs_tools.read_file(_cfg(tmp_path, max_read_bytes=10), "big.txt")
        assert res["truncated"] is True
        assert len(res["text"]) == 10
        assert res["size"] == 1000

    def test_missing(self, tmp_path: Path) -> None:
        with pytest.raises(fs_tools.FsError, match="not found"):
            fs_tools.read_file(_cfg(tmp_path), "nope.txt")

    def test_directory_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "sub").mkdir()
        with pytest.raises(fs_tools.FsError, match="directory"):
            fs_tools.read_file(_cfg(tmp_path), "sub")

    def test_binary_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "b.bin").write_bytes(b"\xff\xfe\x00\x80")
        with pytest.raises(fs_tools.FsError, match="binary"):
            fs_tools.read_file(_cfg(tmp_path), "b.bin")


class TestListDir:
    def test_lists(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("a")
        (tmp_path / "sub").mkdir()
        res = fs_tools.list_dir(_cfg(tmp_path), ".")
        names = {e["name"] for e in res["entries"]}
        assert names == {"a.txt", "sub"}
        kinds = {e["name"]: e["kind"] for e in res["entries"]}
        assert kinds["sub"] == "dir"
        assert kinds["a.txt"] == "file"

    def test_truncates(self, tmp_path: Path) -> None:
        for i in range(20):
            (tmp_path / f"f{i}.txt").write_text("")
        res = fs_tools.list_dir(_cfg(tmp_path, max_list_entries=5), ".")
        assert res["truncated"] is True
        assert len(res["entries"]) == 5

    def test_not_dir(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("x")
        with pytest.raises(fs_tools.FsError, match="not a directory"):
            fs_tools.list_dir(_cfg(tmp_path), "a.txt")


class TestWriteFile:
    def test_writes(self, tmp_path: Path) -> None:
        res = fs_tools.write_file(_cfg(tmp_path), "a.txt", "hello")
        assert res["size"] == 5
        assert (tmp_path / "a.txt").read_text() == "hello"

    def test_too_large(self, tmp_path: Path) -> None:
        with pytest.raises(fs_tools.FsError, match="too large"):
            fs_tools.write_file(_cfg(tmp_path, max_write_bytes=4), "a.txt", "hello")

    def test_no_parent(self, tmp_path: Path) -> None:
        with pytest.raises(fs_tools.FsError, match="parent"):
            fs_tools.write_file(_cfg(tmp_path), "deep/sub/a.txt", "x")

    def test_create_parents(self, tmp_path: Path) -> None:
        res = fs_tools.write_file(
            _cfg(tmp_path), "deep/sub/a.txt", "x", create_parents=True
        )
        assert res["size"] == 1
        assert (tmp_path / "deep/sub/a.txt").read_text() == "x"


class TestApplyPatch:
    def _git_init(self, root: Path) -> None:
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "--allow-empty", "-m", "init"], cwd=root, check=True, capture_output=True)

    def test_applies(self, tmp_path: Path) -> None:
        self._git_init(tmp_path)
        (tmp_path / "a.txt").write_text("hello\n")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
        subprocess.run(["git", "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-m", "add"], cwd=tmp_path, check=True, capture_output=True)
        diff = (
            "diff --git a/a.txt b/a.txt\n"
            "--- a/a.txt\n"
            "+++ b/a.txt\n"
            "@@ -1 +1 @@\n"
            "-hello\n"
            "+world\n"
        )
        res = fs_tools.apply_patch(_cfg(tmp_path), diff)
        assert res["ok"] is True
        assert (tmp_path / "a.txt").read_text() == "world\n"

    def test_check_only(self, tmp_path: Path) -> None:
        self._git_init(tmp_path)
        (tmp_path / "a.txt").write_text("hello\n")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
        subprocess.run(["git", "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-m", "add"], cwd=tmp_path, check=True, capture_output=True)
        diff = (
            "diff --git a/a.txt b/a.txt\n"
            "--- a/a.txt\n"
            "+++ b/a.txt\n"
            "@@ -1 +1 @@\n"
            "-hello\n"
            "+world\n"
        )
        res = fs_tools.apply_patch(_cfg(tmp_path), diff, check_only=True)
        assert res["ok"] is True
        assert (tmp_path / "a.txt").read_text() == "hello\n"

    def test_empty_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(fs_tools.FsError, match="empty"):
            fs_tools.apply_patch(_cfg(tmp_path), "   ")

    def test_bad_diff_returns_not_ok(self, tmp_path: Path) -> None:
        self._git_init(tmp_path)
        diff = "this is not a real diff\n"
        res = fs_tools.apply_patch(_cfg(tmp_path), diff)
        assert res["ok"] is False


class TestFormat:
    def test_format_read(self) -> None:
        out = fs_tools.format_read(
            {"path": "a.txt", "size": 3, "text": "abc", "truncated": False}
        )
        assert "a.txt" in out
        assert "abc" in out

    def test_format_list(self) -> None:
        out = fs_tools.format_list(
            {
                "path": ".",
                "entries": [
                    {"name": "x", "kind": "dir", "size": 0},
                    {"name": "y.txt", "kind": "file", "size": 7},
                ],
                "truncated": False,
            }
        )
        assert "x/" in out
        assert "y.txt" in out


class TestServerDispatch:
    def test_read_file_dispatch(self, tmp_path: Path) -> None:
        from qwen_coder_mcp import server as srv

        (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
        cfg = fs_tools.FsConfig(root=tmp_path)
        out = srv._dispatch(None, "read_file", {"path": "a.txt"}, cfg)  # type: ignore[arg-type]
        assert "hello" in out

    def test_write_then_read(self, tmp_path: Path) -> None:
        from qwen_coder_mcp import server as srv

        cfg = fs_tools.FsConfig(root=tmp_path)
        out = srv._dispatch(
            None, "write_file", {"path": "a.txt", "content": "z"}, cfg
        )  # type: ignore[arg-type]
        assert "wrote" in out
        out = srv._dispatch(None, "read_file", {"path": "a.txt"}, cfg)  # type: ignore[arg-type]
        assert "z" in out

    def test_list_dir_dispatch(self, tmp_path: Path) -> None:
        from qwen_coder_mcp import server as srv

        (tmp_path / "x").mkdir()
        cfg = fs_tools.FsConfig(root=tmp_path)
        out = srv._dispatch(None, "list_dir", {"path": "."}, cfg)  # type: ignore[arg-type]
        assert "x/" in out

    def test_escape_returns_error(self, tmp_path: Path) -> None:
        from qwen_coder_mcp import server as srv

        cfg = fs_tools.FsConfig(root=tmp_path)
        out = srv._dispatch(None, "read_file", {"path": "../etc/passwd"}, cfg)  # type: ignore[arg-type]
        assert "error" in out
        assert "escapes" in out
