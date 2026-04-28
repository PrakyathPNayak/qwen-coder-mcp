"""Loop 267 -- whitespace-tolerant fs_regex_edit.

Sibling of fs_edit that copes with the model emitting code at the
right level but with subtly different indentation or newline style
than the file on disk -- the most common reason fs_edit fails with
"old not found" mid-loop and the model wastes tokens re-reading.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from qwen_coder_mcp import agent_loop, fs_tools


def _cfg(tmp_path: Path) -> fs_tools.FsConfig:
    return fs_tools.FsConfig(root=tmp_path)


# ----------------------------------------------------------- fs_tools layer

class TestRegexEditFile:
    def test_exact_match_replaces(self, tmp_path):
        p = tmp_path / "a.py"
        p.write_text("def foo():\n    return 1\n")
        res = fs_tools.regex_edit_file(
            _cfg(tmp_path), "a.py", "return 1", "return 2"
        )
        assert res["replacements"] == 1
        assert "return 2" in p.read_text()

    def test_whitespace_run_tolerant(self, tmp_path):
        p = tmp_path / "a.py"
        # File on disk uses 4 spaces; model emits 2 spaces.
        p.write_text("def foo():\n    x = 1\n    y = 2\n")
        res = fs_tools.regex_edit_file(
            _cfg(tmp_path),
            "a.py",
            "def foo():\n  x = 1\n  y = 2",  # 2-space indent
            "def foo():\n    x = 99\n    y = 99",
        )
        assert res["replacements"] == 1
        out = p.read_text()
        assert "x = 99" in out
        assert "y = 99" in out

    def test_newline_style_tolerant(self, tmp_path):
        p = tmp_path / "a.py"
        # File has CRLF; model emits LF.
        p.write_bytes(b"a = 1\r\nb = 2\r\n")
        res = fs_tools.regex_edit_file(
            _cfg(tmp_path), "a.py", "a = 1\nb = 2", "a = 9\nb = 9"
        )
        assert res["replacements"] == 1

    def test_count_one_enforces_unique(self, tmp_path):
        p = tmp_path / "a.py"
        p.write_text("x = 1\nx = 1\n")
        with pytest.raises(fs_tools.FsError) as ei:
            fs_tools.regex_edit_file(
                _cfg(tmp_path), "a.py", "x = 1", "x = 2", count=1
            )
        assert "matched 2x" in str(ei.value)

    def test_count_null_replaces_all(self, tmp_path):
        p = tmp_path / "a.py"
        p.write_text("x = 1\nx = 1\n")
        res = fs_tools.regex_edit_file(
            _cfg(tmp_path), "a.py", "x = 1", "x = 2", count=None
        )
        assert res["replacements"] == 2

    def test_zero_match_raises_with_orientation(self, tmp_path):
        p = tmp_path / "a.py"
        p.write_text("hello\n" * 30)
        with pytest.raises(fs_tools.FsError) as ei:
            fs_tools.regex_edit_file(
                _cfg(tmp_path), "a.py", "completely missing", "x"
            )
        msg = str(ei.value)
        assert "first 20 lines" in msg

    def test_dry_run_does_not_mutate(self, tmp_path):
        p = tmp_path / "a.py"
        p.write_text("x = 1\n")
        original = p.read_text()
        res = fs_tools.regex_edit_file(
            _cfg(tmp_path), "a.py", "x = 1", "x = 9", dry_run=True
        )
        assert res["dry_run"] is True
        assert "preview" in res
        assert p.read_text() == original

    def test_raw_regex_mode(self, tmp_path):
        p = tmp_path / "a.py"
        p.write_text("DEBUG=1\nVERSION=1\n")
        res = fs_tools.regex_edit_file(
            _cfg(tmp_path),
            "a.py",
            r"^([A-Z]+)=1$",
            r"\1=2",
            count=2,
            raw_regex=True,
        )
        assert res["replacements"] == 2

    def test_raw_regex_invalid_raises(self, tmp_path):
        p = tmp_path / "a.py"
        p.write_text("x\n")
        with pytest.raises(fs_tools.FsError) as ei:
            fs_tools.regex_edit_file(
                _cfg(tmp_path), "a.py", "[unclosed", "x", raw_regex=True
            )
        assert "invalid regex" in str(ei.value)

    def test_empty_old_rejected(self, tmp_path):
        p = tmp_path / "a.py"
        p.write_text("x\n")
        with pytest.raises(fs_tools.FsError):
            fs_tools.regex_edit_file(_cfg(tmp_path), "a.py", "", "y")

    def test_size_cap_respected(self, tmp_path):
        cfg = fs_tools.FsConfig(root=tmp_path, max_write_bytes=100)
        p = tmp_path / "a.py"
        p.write_text("xxx\n")
        with pytest.raises(fs_tools.FsError) as ei:
            fs_tools.regex_edit_file(
                cfg, "a.py", "xxx", "y" * 500
            )
        assert "too large" in str(ei.value)

    def test_pattern_field_in_result(self, tmp_path):
        p = tmp_path / "a.py"
        p.write_text("hello world\n")
        res = fs_tools.regex_edit_file(
            _cfg(tmp_path), "a.py", "hello world", "bye"
        )
        assert "pattern" in res

    def test_regex_specials_in_old_escaped(self, tmp_path):
        # Brackets and dots in 'old' must match literally, not as regex.
        p = tmp_path / "a.py"
        p.write_text("x[0].foo\n")
        res = fs_tools.regex_edit_file(
            _cfg(tmp_path), "a.py", "x[0].foo", "x[0].bar"
        )
        assert res["replacements"] == 1
        assert "x[0].bar" in p.read_text()

    def test_backslash_in_new_literal(self, tmp_path):
        # The 'new' value must be inserted literally; a stray \1 in
        # the model's reply must not interpolate as a back-ref.
        p = tmp_path / "a.py"
        p.write_text("alpha\n")
        res = fs_tools.regex_edit_file(
            _cfg(tmp_path), "a.py", "alpha", r"beta\1gamma"
        )
        assert res["replacements"] == 1
        assert r"beta\1gamma" in p.read_text()


# ----------------------------------------------------------- agent dispatch

class TestAgentDispatch:
    def test_canonical_alias_routes(self):
        for name in ("fs_edit_regex", "regex_edit", "edit_regex"):
            assert agent_loop._canonical_tool_name(name) == "fs_regex_edit"

    def test_in_write_tools(self):
        assert "fs_regex_edit" in agent_loop.WRITE_TOOLS
        assert "fs_regex_edit" in agent_loop.ALL_TOOLS

    def test_destructive_set_includes_it(self):
        assert "fs_regex_edit" in agent_loop.DESTRUCTIVE_TOOLS

    def test_tool_invocation_via_run_tool(self, tmp_path):
        p = tmp_path / "a.py"
        p.write_text("def foo():\n    return 1\n")
        cfg = _cfg(tmp_path)
        call = agent_loop.ToolCall(
            name="fs_regex_edit",
            args={"path": "a.py", "old": "return  1", "new": "return 99"},
            raw="",
        )
        res = agent_loop.run_tool(
            call,
            fs_cfg=cfg,
            tools=agent_loop.WRITE_TOOLS,
            confirm=agent_loop.always_allow,
        )
        assert "edited" in (res.output or "")
        assert "return 99" in p.read_text()

    def test_alias_dispatch_via_run_tool(self, tmp_path):
        p = tmp_path / "a.py"
        p.write_text("hello\n")
        cfg = _cfg(tmp_path)
        call = agent_loop.ToolCall(
            name="regex_edit",
            args={"path": "a.py", "old": "hello", "new": "world"},
            raw="",
        )
        res = agent_loop.run_tool(
            call,
            fs_cfg=cfg,
            tools=agent_loop.WRITE_TOOLS,
            confirm=agent_loop.always_allow,
        )
        assert "edited" in (res.output or "")
        assert p.read_text() == "world\n"

    def test_system_prompt_documents_tool(self):
        # The model needs to see the new tool in the agent's system prompt.
        from qwen_coder_mcp.agent_loop import TOOL_PROTOCOL_DOC
        assert "fs_regex_edit" in TOOL_PROTOCOL_DOC
        assert "whitespace-tolerant" in TOOL_PROTOCOL_DOC.lower()


class TestPatternBuilder:
    def test_collapses_whitespace_runs(self):
        pat = fs_tools._whitespace_tolerant_pattern("a   b\n c")
        assert pat.search("a b c") is not None
        assert pat.search("a\t\nb\n  c") is not None

    def test_escapes_regex_metas(self):
        pat = fs_tools._whitespace_tolerant_pattern("x.*+?[](){}^$|")
        assert pat.search("x.*+?[](){}^$|") is not None
        assert pat.search("xY") is None
