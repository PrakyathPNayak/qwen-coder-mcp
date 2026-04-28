"""Loop 262: regression tests for two operator-reported issues.

(1) RichLog ``MarkupError`` when assistant output contained literal
    bracketed sequences such as ``[/▍]`` (a box-drawing block char that
    Rich tried to parse as a closing markup tag). The TUI now escapes
    dynamic content via ``rich.markup.escape`` before interpolating it
    into a markup-templated write.

(2) Models often emit tool names by analogy (``run_command`` instead of
    ``run_shell``, ``read_file`` instead of ``fs_read``, etc.). The
    parser now normalises a small alias table so those calls dispatch
    instead of failing with "unknown tool".
"""
from __future__ import annotations

import json

import pytest

from qwen_coder_mcp import agent_loop, tui


# --------------------------------------------------------- markup safety
class TestSafeMarkupHelper:
    def test_escapes_closing_tag_lookalike(self):
        # The exact pattern from the operator's traceback:
        out = tui._safe_markup("[/▍] progress")
        # rich.markup.escape replaces "[" with "\[" so the parser sees
        # a literal bracket, not a tag. The escaped form must NOT
        # contain a bare "[/" prefix that Rich would close-match.
        assert "\\[/" in out or "[/" not in out

    def test_passthrough_for_plain_text(self):
        assert tui._safe_markup("hello world") == "hello world"

    def test_escapes_open_tag_lookalike(self):
        out = tui._safe_markup("foo [bar] baz")
        assert "\\[" in out

    def test_handles_non_string_input(self):
        # callers occasionally pass exception instances; coerce safely.
        assert tui._safe_markup(123) == "123"


class TestRichLogMarkupSafe:
    """Render the offending payload through Rich's actual markup parser
    and assert it no longer raises. We exercise the same code path the
    TUI uses (interpolating into ``[green]qwen>[/green] {reply}``) and
    confirm the escape helper makes it survive.
    """

    def test_offending_payload_no_longer_raises(self):
        rich_text = pytest.importorskip("rich.text")
        payload = "qwen> [agent error: MarkupError: closing tag '[/▍]' does not matc"
        safe = tui._safe_markup(payload)
        # Should parse cleanly now -- no MarkupError.
        rendered = rich_text.Text.from_markup(f"[green]qwen>[/green] {safe}")
        assert "▍" in rendered.plain

    def test_unescaped_payload_would_have_raised(self):
        """Sanity check that without the escape we'd still crash --
        guards against silent regressions if ``_safe_markup`` is ever
        accidentally turned into a no-op.
        """
        rich_text = pytest.importorskip("rich.text")
        rich_errors = pytest.importorskip("rich.errors")
        payload = "[/▍]"
        with pytest.raises(rich_errors.MarkupError):
            rich_text.Text.from_markup(f"[green]qwen>[/green] {payload}")


# --------------------------------------------------------- alias parser
class TestToolNameAliases:
    def test_run_command_resolves_to_run_shell(self):
        body = json.dumps({"name": "run_command", "args": {"cmd": "ls"}})
        calls = agent_loop.parse_tool_calls(f"<tool_call>{body}</tool_call>")
        assert len(calls) == 1
        assert calls[0].name == "run_shell"
        assert calls[0].args == {"cmd": "ls"}

    @pytest.mark.parametrize(
        "alias,canonical",
        [
            ("bash", "run_shell"),
            ("shell", "run_shell"),
            ("sh", "run_shell"),
            ("exec", "run_shell"),
            ("read_file", "fs_read"),
            ("write_file", "fs_write"),
            ("edit_file", "fs_edit"),
            ("insert_file", "fs_insert"),
            ("list_dir", "fs_list"),
            ("ls", "fs_list"),
            ("search", "grep"),
            ("rg", "grep"),
            ("glob", "find"),
        ],
    )
    def test_alias_table(self, alias, canonical):
        body = json.dumps({"name": alias, "args": {}})
        calls = agent_loop.parse_tool_calls(f"<tool_call>{body}</tool_call>")
        assert calls and calls[0].name == canonical

    def test_aliases_case_insensitive(self):
        body = json.dumps({"name": "RUN_COMMAND", "args": {}})
        calls = agent_loop.parse_tool_calls(f"<tool_call>{body}</tool_call>")
        assert calls and calls[0].name == "run_shell"

    def test_unknown_name_passes_through(self):
        body = json.dumps({"name": "frobnicate", "args": {}})
        calls = agent_loop.parse_tool_calls(f"<tool_call>{body}</tool_call>")
        assert calls and calls[0].name == "frobnicate"

    def test_canonical_name_unchanged(self):
        body = json.dumps({"name": "run_shell", "args": {"cmd": "echo hi"}})
        calls = agent_loop.parse_tool_calls(f"<tool_call>{body}</tool_call>")
        assert calls and calls[0].name == "run_shell"

    def test_alias_dispatches_through_run_tool(self, tmp_path):
        """End-to-end: a ``run_command`` call from the model lands on the
        ``run_shell`` tool and produces a normal shell-result block.
        """
        from qwen_coder_mcp import fs_tools

        cfg = fs_tools.FsConfig(root=tmp_path)
        body = json.dumps({"name": "run_command", "args": {"cmd": "echo loop262"}})
        calls = agent_loop.parse_tool_calls(f"<tool_call>{body}</tool_call>")
        result = agent_loop.run_tool(
            calls[0],
            fs_cfg=cfg,
            tools=agent_loop.WRITE_TOOLS,
            confirm=lambda _c: True,
        )
        assert not result.error
        assert "loop262" in result.output
