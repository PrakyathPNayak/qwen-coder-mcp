"""Tests for the ``summary`` AgentEvent emitted right before the
terminal ``final`` / ``limit`` event by ``run_agent``."""
from __future__ import annotations

from pathlib import Path

from qwen_coder_mcp import agent_loop, fs_tools
from qwen_coder_mcp.agent_loop import run_agent
from qwen_coder_mcp.qwen_client import ChatMessage


def _cfg(tmp_path: Path) -> fs_tools.FsConfig:
    return fs_tools.FsConfig(root=tmp_path)


class _ScriptedClient:
    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)

    def chat(self, _h: list[ChatMessage]) -> str:
        if not self._replies:
            return "done"
        return self._replies.pop(0)


def _events(client, tmp_path: Path, **kw):
    return list(
        run_agent(
            history=[],
            user_text="x",
            client=client,
            fs_cfg=_cfg(tmp_path),
            stream=False,
            **kw,
        )
    )


class TestSummaryEvent:
    def test_emitted_before_final_on_no_tool_path(self, tmp_path: Path) -> None:
        events = _events(_ScriptedClient(["just a plain answer"]), tmp_path)
        kinds = [e.kind for e in events]
        assert "summary" in kinds
        assert kinds.index("summary") < kinds.index("final")

    def test_zero_tools_summary_text(self, tmp_path: Path) -> None:
        events = _events(_ScriptedClient(["plain answer"]), tmp_path)
        summary = next(e for e in events if e.kind == "summary")
        assert summary.text == "0 tool calls"
        assert summary.latency_s == 0.0

    def test_singular_phrasing(self, tmp_path: Path) -> None:
        client = _ScriptedClient([
            '<tool_call>{"name": "fs_list", "args": {"path": "."}}</tool_call>',
            "done",
        ])
        events = _events(client, tmp_path)
        summary = next(e for e in events if e.kind == "summary")
        assert summary.text.startswith("1 tool call,")  # singular, no 's'
        assert "tool calls" not in summary.text

    def test_plural_phrasing_and_running_total(self, tmp_path: Path) -> None:
        # Two-step run: tool call, then plain final.
        client = _ScriptedClient([
            '<tool_call>{"name": "fs_list", "args": {"path": "."}}</tool_call>'
            '<tool_call>{"name": "fs_list", "args": {"path": "."}}</tool_call>',
            "done",
        ])
        events = _events(client, tmp_path)
        summary = next(e for e in events if e.kind == "summary")
        assert summary.text.startswith("2 tool calls,")
        assert summary.latency_s is not None and summary.latency_s >= 0.0

    def test_emitted_before_limit_on_max_steps(self, tmp_path: Path) -> None:
        # Always emit a tool call so the loop hits max_steps.
        replies = ['<tool_call>{"name": "fs_list", "args": {"path": "."}}</tool_call>'] * 10
        events = _events(_ScriptedClient(replies), tmp_path, max_steps=2)
        kinds = [e.kind for e in events]
        assert "summary" in kinds
        assert "limit" in kinds
        assert kinds.index("summary") < kinds.index("limit")
        summary = next(e for e in events if e.kind == "summary")
        # Two iterations × one tool each = two tool calls.
        assert summary.text.startswith("2 tool calls,")

    def test_summary_emitted_exactly_once(self, tmp_path: Path) -> None:
        client = _ScriptedClient([
            '<tool_call>{"name": "fs_list", "args": {"path": "."}}</tool_call>',
            "done",
        ])
        events = _events(client, tmp_path)
        assert sum(1 for e in events if e.kind == "summary") == 1
