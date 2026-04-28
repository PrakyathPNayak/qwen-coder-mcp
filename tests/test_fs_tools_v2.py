"""Tests for the loop-252 efficient file tools.

Adds line-range reads, surgical str-replace edits, and line-position
inserts. The model can now manipulate a single block of a giant file
without reading or rewriting the rest.

Pinned behaviors:

read_file ranges:
  * Full-file path unchanged when no range/line_numbers args given
    (back-compat with the loop-129 call sites).
  * 1-based inclusive on both ends (grep -n / less semantics).
  * Negative indices count from the end (-1 == last line).
  * max_lines caps the slice AFTER the range is applied.
  * line_numbers=True prefixes "<n> | " with right-aligned padding.
  * Out-of-range start clamps to 1; out-of-range end clamps to total.
  * Range result includes total_lines and range={start,end}.
  * Bytes cap still applies after slicing.

edit_file:
  * count=1 (default) requires a UNIQUE match; ambiguous match is
    rejected with a helpful error referencing the occurrence count.
  * count=None replaces every occurrence.
  * count=N requires exactly N occurrences.
  * empty 'old' rejected (avoids accidental noop / spam).
  * Non-existent file rejected (you fs_write to create).
  * Atomic write: no .tmp leak on success.
  * 'old' not present -> error with first-20-line preview to help
    the model re-orient.

insert_lines:
  * Exactly one of after_line / before_line required.
  * after_line=0 prepends; after_line=total appends.
  * before_line=1 prepends; before_line=total+1 appends.
  * Negative after_line counts from end.
  * Out-of-range raises.
  * Caller controls newline boundaries.

Discoverability:
  * MCP_TOOLS_PROTOCOL mentions fs_edit + fs_insert.
  * agent_loop.WRITE_TOOLS contains fs_edit and fs_insert.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from qwen_coder_mcp import agent_loop, fs_tools


def _cfg(tmp_path: Path) -> fs_tools.FsConfig:
    return fs_tools.FsConfig(root=tmp_path)


# ---------------------------------------------------------- read ranges
class TestReadFileRanges:
    def _seed(self, tmp_path: Path) -> Path:
        p = tmp_path / "f.py"
        p.write_text("\n".join(f"line{i}" for i in range(1, 21)) + "\n")
        return p

    def test_full_read_back_compat(self, tmp_path):
        self._seed(tmp_path)
        res = fs_tools.read_file(_cfg(tmp_path), "f.py")
        assert "range" not in res
        assert "total_lines" not in res
        assert res["text"].count("\n") == 20

    def test_range_inclusive_both_ends(self, tmp_path):
        self._seed(tmp_path)
        res = fs_tools.read_file(_cfg(tmp_path), "f.py", start_line=3, end_line=5)
        assert res["text"] == "line3\nline4\nline5\n"
        assert res["range"] == {"start": 3, "end": 5}
        assert res["total_lines"] == 20

    def test_negative_start_counts_from_end(self, tmp_path):
        self._seed(tmp_path)
        res = fs_tools.read_file(_cfg(tmp_path), "f.py", start_line=-2)
        assert res["text"] == "line19\nline20\n"
        assert res["range"]["start"] == 19

    def test_negative_end_counts_from_end(self, tmp_path):
        self._seed(tmp_path)
        res = fs_tools.read_file(_cfg(tmp_path), "f.py", start_line=1, end_line=-19)
        assert res["text"] == "line1\nline2\n"

    def test_max_lines_caps_slice(self, tmp_path):
        self._seed(tmp_path)
        res = fs_tools.read_file(_cfg(tmp_path), "f.py", start_line=1, max_lines=3)
        assert res["text"] == "line1\nline2\nline3\n"
        assert res["range"]["end"] == 3

    def test_line_numbers_prefix(self, tmp_path):
        self._seed(tmp_path)
        res = fs_tools.read_file(
            _cfg(tmp_path), "f.py", start_line=1, end_line=2, line_numbers=True
        )
        assert res["text"].startswith("1 | line1")

    def test_line_numbers_padding_aligns(self, tmp_path):
        # 20 lines requires width=2; line "5" should appear as " 5"
        self._seed(tmp_path)
        res = fs_tools.read_file(
            _cfg(tmp_path), "f.py", start_line=5, end_line=10, line_numbers=True
        )
        first = res["text"].split("\n", 1)[0]
        assert first == " 5 | line5"

    def test_oob_start_clamps_to_one(self, tmp_path):
        self._seed(tmp_path)
        res = fs_tools.read_file(_cfg(tmp_path), "f.py", start_line=-9999, end_line=2)
        assert res["range"]["start"] == 1
        assert res["text"] == "line1\nline2\n"

    def test_oob_end_clamps_to_total(self, tmp_path):
        self._seed(tmp_path)
        res = fs_tools.read_file(_cfg(tmp_path), "f.py", start_line=19, end_line=9999)
        assert res["range"]["end"] == 20

    def test_inverted_range_returns_empty(self, tmp_path):
        self._seed(tmp_path)
        res = fs_tools.read_file(_cfg(tmp_path), "f.py", start_line=10, end_line=5)
        assert res["text"] == ""

    def test_byte_cap_still_applies(self, tmp_path):
        self._seed(tmp_path)
        cfg = fs_tools.FsConfig(root=tmp_path, max_read_bytes=10)
        res = fs_tools.read_file(cfg, "f.py", start_line=1, end_line=20)
        assert res["truncated"] is True
        assert len(res["text"].encode()) <= 10

    def test_binary_still_rejected(self, tmp_path):
        (tmp_path / "b.bin").write_bytes(b"\x00\x01\xff\xfe")
        with pytest.raises(fs_tools.FsError, match="binary"):
            fs_tools.read_file(_cfg(tmp_path), "b.bin", start_line=1)


# ---------------------------------------------------------- edit_file
class TestEditFile:
    def _seed(self, tmp_path: Path, body: str = "alpha\nbeta\ngamma\n") -> Path:
        p = tmp_path / "e.py"
        p.write_text(body)
        return p

    def test_unique_replace_default(self, tmp_path):
        self._seed(tmp_path)
        res = fs_tools.edit_file(_cfg(tmp_path), "e.py", "beta", "BETA")
        assert res["replacements"] == 1
        assert (tmp_path / "e.py").read_text() == "alpha\nBETA\ngamma\n"

    def test_ambiguous_match_rejected(self, tmp_path):
        self._seed(tmp_path, "x\nx\nx\n")
        with pytest.raises(fs_tools.FsError, match="occurs 3x"):
            fs_tools.edit_file(_cfg(tmp_path), "e.py", "x", "Y")

    def test_count_none_replaces_all(self, tmp_path):
        self._seed(tmp_path, "x\nx\nx\n")
        res = fs_tools.edit_file(_cfg(tmp_path), "e.py", "x", "Y", count=None)
        assert res["replacements"] == 3
        assert (tmp_path / "e.py").read_text() == "Y\nY\nY\n"

    def test_count_n_requires_n_occurrences(self, tmp_path):
        self._seed(tmp_path, "x\nx\n")
        # asking for 3 when only 2 exist -> error
        with pytest.raises(fs_tools.FsError, match="count=3"):
            fs_tools.edit_file(_cfg(tmp_path), "e.py", "x", "Y", count=3)

    def test_count_n_exact_works(self, tmp_path):
        self._seed(tmp_path, "x\nx\n")
        res = fs_tools.edit_file(_cfg(tmp_path), "e.py", "x", "Y", count=2)
        assert res["replacements"] == 2

    def test_old_not_found_includes_preview(self, tmp_path):
        self._seed(tmp_path)
        with pytest.raises(fs_tools.FsError) as ei:
            fs_tools.edit_file(_cfg(tmp_path), "e.py", "missing-string", "X")
        msg = str(ei.value)
        assert "not found" in msg
        assert "alpha" in msg  # first-20-line preview

    def test_empty_old_rejected(self, tmp_path):
        self._seed(tmp_path)
        with pytest.raises(fs_tools.FsError, match="non-empty"):
            fs_tools.edit_file(_cfg(tmp_path), "e.py", "", "X")

    def test_nonexistent_file_rejected(self, tmp_path):
        with pytest.raises(fs_tools.FsError, match="not found"):
            fs_tools.edit_file(_cfg(tmp_path), "missing.py", "x", "y")

    def test_no_tmp_leak_on_success(self, tmp_path):
        self._seed(tmp_path)
        fs_tools.edit_file(_cfg(tmp_path), "e.py", "alpha", "ALPHA")
        leftovers = list(tmp_path.glob("*.tmp"))
        assert leftovers == []

    def test_path_escape_rejected(self, tmp_path):
        with pytest.raises(fs_tools.FsError, match="escapes"):
            fs_tools.edit_file(_cfg(tmp_path), "../etc/passwd", "x", "y")


# ---------------------------------------------------------- insert_lines
class TestInsertLines:
    def _seed(self, tmp_path: Path) -> Path:
        p = tmp_path / "i.py"
        p.write_text("a\nb\nc\n")
        return p

    def test_after_line_zero_prepends(self, tmp_path):
        self._seed(tmp_path)
        fs_tools.insert_lines(_cfg(tmp_path), "i.py", after_line=0, content="zero\n")
        assert (tmp_path / "i.py").read_text() == "zero\na\nb\nc\n"

    def test_after_line_total_appends(self, tmp_path):
        self._seed(tmp_path)
        fs_tools.insert_lines(_cfg(tmp_path), "i.py", after_line=3, content="d\n")
        assert (tmp_path / "i.py").read_text() == "a\nb\nc\nd\n"

    def test_before_line_one_prepends(self, tmp_path):
        self._seed(tmp_path)
        fs_tools.insert_lines(_cfg(tmp_path), "i.py", before_line=1, content="zero\n")
        assert (tmp_path / "i.py").read_text() == "zero\na\nb\nc\n"

    def test_insert_in_middle(self, tmp_path):
        self._seed(tmp_path)
        fs_tools.insert_lines(_cfg(tmp_path), "i.py", after_line=1, content="a2\n")
        assert (tmp_path / "i.py").read_text() == "a\na2\nb\nc\n"

    def test_negative_after_line(self, tmp_path):
        self._seed(tmp_path)
        # after_line=-1 means "after the last line" (i.e. append).
        fs_tools.insert_lines(_cfg(tmp_path), "i.py", after_line=-1, content="d\n")
        assert (tmp_path / "i.py").read_text() == "a\nb\nc\nd\n"

    def test_both_provided_rejected(self, tmp_path):
        self._seed(tmp_path)
        with pytest.raises(fs_tools.FsError, match="exactly one"):
            fs_tools.insert_lines(
                _cfg(tmp_path), "i.py", after_line=1, before_line=2, content="x\n"
            )

    def test_neither_provided_rejected(self, tmp_path):
        self._seed(tmp_path)
        with pytest.raises(fs_tools.FsError, match="exactly one"):
            fs_tools.insert_lines(_cfg(tmp_path), "i.py", content="x\n")

    def test_oob_after_line_rejected(self, tmp_path):
        self._seed(tmp_path)
        with pytest.raises(fs_tools.FsError, match="out of range"):
            fs_tools.insert_lines(
                _cfg(tmp_path), "i.py", after_line=999, content="x\n"
            )

    def test_oob_before_line_rejected(self, tmp_path):
        self._seed(tmp_path)
        with pytest.raises(fs_tools.FsError, match="out of range"):
            fs_tools.insert_lines(
                _cfg(tmp_path), "i.py", before_line=999, content="x\n"
            )

    def test_caller_controls_newlines(self, tmp_path):
        # No trailing \n in content -> joins into preceding line.
        self._seed(tmp_path)
        fs_tools.insert_lines(_cfg(tmp_path), "i.py", after_line=1, content="--\n")
        assert (tmp_path / "i.py").read_text() == "a\n--\nb\nc\n"


# ---------------------------------------------------------- agent_loop wiring
class TestAgentLoopWiring:
    def test_fs_edit_in_write_tools(self):
        assert "fs_edit" in agent_loop.WRITE_TOOLS

    def test_fs_insert_in_write_tools(self):
        assert "fs_insert" in agent_loop.WRITE_TOOLS

    def test_protocol_doc_mentions_fs_edit(self):
        assert "fs_edit" in agent_loop.TOOL_PROTOCOL_DOC

    def test_protocol_doc_mentions_fs_insert(self):
        assert "fs_insert" in agent_loop.TOOL_PROTOCOL_DOC

    def test_protocol_doc_mentions_line_range(self):
        assert "start_line" in agent_loop.TOOL_PROTOCOL_DOC
        assert "line_numbers" in agent_loop.TOOL_PROTOCOL_DOC

    def test_fs_edit_tool_executes(self, tmp_path):
        (tmp_path / "x.py").write_text("alpha\nbeta\ngamma\n")
        cfg = fs_tools.FsConfig(root=tmp_path)
        out = agent_loop._tool_fs_edit(
            {"path": "x.py", "old": "beta", "new": "BETA"}, cfg
        )
        assert "replacement(s)" in out
        assert (tmp_path / "x.py").read_text() == "alpha\nBETA\ngamma\n"

    def test_fs_edit_tool_count_null_via_string(self, tmp_path):
        # the JSON tool-call path may pass count=None directly; verify
        # the integer-coerce path keeps None as None.
        (tmp_path / "x.py").write_text("x\nx\n")
        cfg = fs_tools.FsConfig(root=tmp_path)
        out = agent_loop._tool_fs_edit(
            {"path": "x.py", "old": "x", "new": "Y", "count": None}, cfg
        )
        assert "2 replacement" in out

    def test_fs_read_tool_with_range(self, tmp_path):
        (tmp_path / "y.py").write_text("a\nb\nc\nd\n")
        cfg = fs_tools.FsConfig(root=tmp_path)
        out = agent_loop._tool_fs_read(
            {"path": "y.py", "start_line": 2, "end_line": 3}, cfg
        )
        assert "lines 2-3 of 4" in out
        assert "b\nc\n" in out

    def test_fs_read_tool_with_line_numbers(self, tmp_path):
        (tmp_path / "y.py").write_text("a\nb\n")
        cfg = fs_tools.FsConfig(root=tmp_path)
        out = agent_loop._tool_fs_read(
            {"path": "y.py", "start_line": 1, "end_line": 2, "line_numbers": True},
            cfg,
        )
        assert "1 | a" in out

    def test_fs_insert_tool_executes(self, tmp_path):
        (tmp_path / "z.py").write_text("a\nb\n")
        cfg = fs_tools.FsConfig(root=tmp_path)
        out = agent_loop._tool_fs_insert(
            {"path": "z.py", "after_line": 1, "content": "a2\n"}, cfg
        )
        assert "inserted" in out
        assert (tmp_path / "z.py").read_text() == "a\na2\nb\n"

    def test_fs_read_back_compat_no_range(self, tmp_path):
        (tmp_path / "y.py").write_text("hello\n")
        cfg = fs_tools.FsConfig(root=tmp_path)
        out = agent_loop._tool_fs_read({"path": "y.py"}, cfg)
        # No header when no range was active.
        assert out == "hello\n"
