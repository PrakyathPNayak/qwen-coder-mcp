"""Tests for the loop-253 fs_edit dry_run + /view slash command.

dry_run behavior:
  * dry_run=True validates the match and returns the would-be content
    without mutating the file on disk.
  * Result includes dry_run=True and a 'preview' string.
  * Errors (ambiguous match, missing file) still raise.
  * Subsequent fs_edit without dry_run still works.

/view slash command:
  * /view <path>            -> full file with line-numbers
  * /view <path> 5          -> 5..54 (50-line default window)
  * /view <path> 3 7        -> inclusive range 3..7
  * /view <path> 3 7 --plain -> no line-number prefix
  * Returns a header summarizing range/of-total
  * Friendly errors on bad ints / missing file
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from qwen_coder_mcp import agent_loop, fs_tools, tui


def _cfg(p: Path) -> fs_tools.FsConfig:
    return fs_tools.FsConfig(root=p)


# ---------------------------------------------------------- dry_run
class TestEditDryRun:
    def test_dry_run_does_not_mutate(self, tmp_path):
        f = tmp_path / "x.py"
        f.write_text("alpha\nbeta\ngamma\n")
        before = f.read_text()
        res = fs_tools.edit_file(
            _cfg(tmp_path), "x.py", "beta", "BETA", dry_run=True
        )
        assert res["dry_run"] is True
        assert f.read_text() == before

    def test_dry_run_preview_contains_change(self, tmp_path):
        f = tmp_path / "x.py"
        f.write_text("alpha\nbeta\ngamma\n")
        res = fs_tools.edit_file(
            _cfg(tmp_path), "x.py", "beta", "BETA", dry_run=True
        )
        assert "BETA" in res["preview"]
        assert "alpha" in res["preview"]

    def test_dry_run_replacements_count(self, tmp_path):
        f = tmp_path / "x.py"
        f.write_text("x\nx\nx\n")
        res = fs_tools.edit_file(
            _cfg(tmp_path), "x.py", "x", "Y", count=None, dry_run=True
        )
        assert res["replacements"] == 3
        assert f.read_text() == "x\nx\nx\n"  # unchanged

    def test_dry_run_ambiguous_still_raises(self, tmp_path):
        (tmp_path / "x.py").write_text("x\nx\nx\n")
        with pytest.raises(fs_tools.FsError, match="occurs 3x"):
            fs_tools.edit_file(_cfg(tmp_path), "x.py", "x", "Y", dry_run=True)

    def test_dry_run_then_real_edit(self, tmp_path):
        f = tmp_path / "x.py"
        f.write_text("alpha\nbeta\n")
        fs_tools.edit_file(_cfg(tmp_path), "x.py", "beta", "BETA", dry_run=True)
        assert "BETA" not in f.read_text()
        fs_tools.edit_file(_cfg(tmp_path), "x.py", "beta", "BETA")
        assert "BETA" in f.read_text()

    def test_real_edit_has_dry_run_false(self, tmp_path):
        (tmp_path / "x.py").write_text("alpha\n")
        res = fs_tools.edit_file(_cfg(tmp_path), "x.py", "alpha", "ALPHA")
        assert res["dry_run"] is False

    def test_agent_tool_dry_run(self, tmp_path):
        f = tmp_path / "x.py"
        f.write_text("alpha\nbeta\n")
        out = agent_loop._tool_fs_edit(
            {"path": "x.py", "old": "beta", "new": "BETA", "dry_run": True},
            _cfg(tmp_path),
        )
        assert "dry-run" in out
        assert f.read_text() == "alpha\nbeta\n"  # unchanged

    def test_agent_tool_real_says_edited(self, tmp_path):
        (tmp_path / "x.py").write_text("alpha\nbeta\n")
        out = agent_loop._tool_fs_edit(
            {"path": "x.py", "old": "beta", "new": "BETA"}, _cfg(tmp_path)
        )
        assert out.startswith("edited ")


# ---------------------------------------------------------- /view
class TestViewSlash:
    def _seed(self, tmp_path):
        (tmp_path / "f.py").write_text("\n".join(f"l{i}" for i in range(1, 11)) + "\n")

    def test_view_full_file(self, tmp_path):
        self._seed(tmp_path)
        out = tui._render_view(_cfg(tmp_path), ["f.py"])
        # full file with default line-numbering
        assert "of 10" in out
        assert "1 | l1" in out
        assert "10 | l10" in out

    def test_view_inclusive_range(self, tmp_path):
        self._seed(tmp_path)
        out = tui._render_view(_cfg(tmp_path), ["f.py", "3", "5"])
        assert "lines 3-5 of 10" in out
        assert "3 | l3" in out
        assert "5 | l5" in out
        assert "l2" not in out
        assert "l6" not in out

    def test_view_single_int_uses_default_window(self, tmp_path):
        self._seed(tmp_path)
        out = tui._render_view(_cfg(tmp_path), ["f.py", "5"])
        # default window = 50 lines, file has 10 -> clamps to end=10
        assert "lines 5-10 of 10" in out

    def test_view_plain_drops_prefix(self, tmp_path):
        self._seed(tmp_path)
        out = tui._render_view(_cfg(tmp_path), ["f.py", "3", "5", "--plain"])
        # No " | " marker means plain mode worked
        assert "3 | l3" not in out
        assert "l3" in out

    def test_view_bad_int_friendly_error(self, tmp_path):
        self._seed(tmp_path)
        out = tui._render_view(_cfg(tmp_path), ["f.py", "abc"])
        assert "invalid start line" in out

    def test_view_missing_file(self, tmp_path):
        out = tui._render_view(_cfg(tmp_path), ["nope.py"])
        assert "view error" in out

    def test_view_no_args_returns_usage(self, tmp_path):
        out = tui._render_view(_cfg(tmp_path), [])
        assert out.startswith("usage:")

    def test_view_only_plain_flag_returns_usage(self, tmp_path):
        out = tui._render_view(_cfg(tmp_path), ["--plain"])
        assert out.startswith("usage:")

    def test_view_dispatcher(self, tmp_path):
        self._seed(tmp_path)
        cmd = tui.parse_slash("/view f.py 1 3")
        out, _ = tui.dispatch_slash(
            cmd,
            client=SimpleNamespace(settings=None),
            fs_cfg=_cfg(tmp_path),
        )
        assert "lines 1-3 of 10" in out

    def test_view_in_completion(self):
        comps = tui.slash_completions("/v")
        assert "/view" in comps

    def test_help_documents_view(self):
        assert "/view" in tui.HELP_TEXT
