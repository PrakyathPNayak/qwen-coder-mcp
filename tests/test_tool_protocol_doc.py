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
