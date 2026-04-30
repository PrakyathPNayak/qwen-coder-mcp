"""Loop 272 — system prompt reflects ONLY the actually-registered tools.

Previously the static TOOL_PROTOCOL_DOC listed write tools even when
the caller passed a read-only registry, so the model would emit
fs_write calls and the dispatcher would return "unknown tool".
"""
from __future__ import annotations

import pytest

from qwen_coder_mcp import agent_loop


class TestBuildProtocolDoc:
    def test_default_registry_omits_write_tools(self):
        doc = agent_loop.build_tool_protocol_doc(agent_loop.DEFAULT_TOOLS)
        assert "fs_read" in doc and "fs_list" in doc
        # Write tools must NOT appear when registry is read-only.
        for t in ("fs_write", "fs_edit", "fs_regex_edit", "fs_insert",
                  "apply_patch", "run_shell"):
            assert f"- {t}(" not in doc, f"{t} leaked into read-only doc"

    def test_full_registry_lists_all_tools(self):
        doc = agent_loop.build_tool_protocol_doc(agent_loop.ALL_TOOLS)
        for t in ("web_search", "fs_read", "fs_write", "fs_edit",
                  "fs_regex_edit", "run_shell", "apply_patch"):
            assert f"- {t}(" in doc, f"{t} missing from full doc"

    def test_none_falls_back_to_default(self):
        doc = agent_loop.build_tool_protocol_doc(None)
        # Should equal the default-registry doc.
        assert doc == agent_loop.build_tool_protocol_doc(agent_loop.DEFAULT_TOOLS)

    def test_custom_unknown_tool_gets_stub_blurb(self):
        custom = {**agent_loop.DEFAULT_TOOLS,
                  "weird_custom": lambda args, fs_cfg, confirm=None: "ok"}
        doc = agent_loop.build_tool_protocol_doc(custom)
        assert "- weird_custom(...)" in doc
        assert "custom tool registered at runtime" in doc

    def test_doc_keeps_header_and_footer(self):
        doc = agent_loop.build_tool_protocol_doc(agent_loop.DEFAULT_TOOLS)
        assert "<tool_call>" in doc
        assert "Available tools:" in doc
        assert "Rules:" in doc

    def test_legacy_constant_still_full(self):
        # External callers grep TOOL_PROTOCOL_DOC for tool names; keep it
        # as the full-registry doc for backward-compat.
        assert "fs_write" in agent_loop.TOOL_PROTOCOL_DOC
        assert "run_shell" in agent_loop.TOOL_PROTOCOL_DOC

    def test_run_agent_emits_doc_matching_registry(self, monkeypatch):
        # When run_agent is invoked with the read-only registry, the
        # system prompt it injects must NOT mention write tools.
        from qwen_coder_mcp.agent_loop import run_agent, ChatMessage
        from types import SimpleNamespace

        captured = {}

        class _Client:
            task_memory = None
            def chat_stream(self, history):
                captured["sys"] = history[0].content
                yield "done"

        history: list[ChatMessage] = []
        events = list(run_agent(
            history=history,
            user_text="hi",
            client=_Client(),
            fs_cfg=None,  # type: ignore[arg-type]
            max_steps=1,
        ))
        sys = captured.get("sys", "")
        # read-only default registry → write tools must NOT be advertised.
        assert "- fs_write(" not in sys
        assert "- run_shell(" not in sys
        assert "- fs_read(" in sys


class TestToolCatalogReplacement:
    """Loop 286: when the tool registry changes between turns (e.g.
    first turn read-only, second turn write-enabled), run_agent must
    replace the stale catalog in the system message, not leave the
    old read-only list in place.
    """

    def _make_client(self, captured: dict, reply: str = "done"):
        class _Client:
            task_memory = None
            def chat_stream(self, history):
                captured["sys"] = history[0].content
                yield reply
        return _Client()

    def test_write_tools_present_when_all_tools_passed(self):
        from qwen_coder_mcp.agent_loop import run_agent, ALL_TOOLS, ChatMessage
        captured = {}
        history: list[ChatMessage] = []
        list(run_agent(
            history=history,
            user_text="hi",
            client=self._make_client(captured),
            fs_cfg=None,  # type: ignore[arg-type]
            tools=ALL_TOOLS,
            max_steps=1,
        ))
        sys = captured.get("sys", "")
        assert "- fs_write(" in sys
        assert "- run_shell(" in sys
        assert "- python_exec(" in sys

    def test_stale_readonly_catalog_replaced_on_write_turn(self):
        """First turn: read-only. Second turn: ALL_TOOLS. The second
        turn's system message must have fs_write, not just the old catalog."""
        from qwen_coder_mcp.agent_loop import (
            run_agent, ALL_TOOLS, DEFAULT_TOOLS, ChatMessage
        )
        captured_first = {}
        captured_second = {}

        class _ClientFirst:
            task_memory = None
            def chat_stream(self, history):
                captured_first["sys"] = history[0].content
                yield "first done"

        class _ClientSecond:
            task_memory = None
            def chat_stream(self, history):
                captured_second["sys"] = history[0].content
                yield "second done"

        # First turn: read-only
        history: list[ChatMessage] = []
        list(run_agent(
            history=history,
            user_text="read something",
            client=_ClientFirst(),
            fs_cfg=None,  # type: ignore[arg-type]
            tools=DEFAULT_TOOLS,
            max_steps=1,
        ))
        sys_first = captured_first.get("sys", "")
        assert "- fs_write(" not in sys_first, "read-only turn must NOT have fs_write"

        # Second turn (same history): ALL_TOOLS
        list(run_agent(
            history=history,
            user_text="write something",
            client=_ClientSecond(),
            fs_cfg=None,  # type: ignore[arg-type]
            tools=ALL_TOOLS,
            max_steps=1,
        ))
        sys_second = captured_second.get("sys", "")
        assert "- fs_write(" in sys_second, "write turn must have fs_write in sys prompt"
        assert "- run_shell(" in sys_second, "write turn must have run_shell in sys prompt"
        # Old read-only sentinel must be gone (replaced not appended)
        assert sys_second.count("Available tools:") <= 2, (
            "should not have duplicate tool catalog sections"
        )
