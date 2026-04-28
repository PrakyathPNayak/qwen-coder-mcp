"""Tests that ``run_agent`` populates ``AgentEvent.latency_s`` on
``tool_result`` events and leaves it ``None`` on every other kind."""
from __future__ import annotations

import time
from pathlib import Path

from qwen_coder_mcp import agent_loop, fs_tools
from qwen_coder_mcp.agent_loop import AgentEvent, run_agent
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


def _slow_tool(_args: dict, _cfg: fs_tools.FsConfig) -> str:
    time.sleep(0.05)
    return "ok"


class TestAgentEventLatency:
    def test_default_is_none(self) -> None:
        ev = AgentEvent(kind="chunk", text="x")
        assert ev.latency_s is None

    def test_tool_result_carries_latency(self, tmp_path: Path) -> None:
        replies = [
            '<tool_call>{"name": "fs_list", "args": {"path": "."}}</tool_call>',
            "done",
        ]
        events = list(
            run_agent(
                history=[],
                user_text="x",
                client=_ScriptedClient(replies),
                fs_cfg=_cfg(tmp_path),
                stream=False,
            )
        )
        results = [e for e in events if e.kind == "tool_result"]
        assert len(results) == 1
        assert results[0].latency_s is not None
        assert 0.0 <= results[0].latency_s < 5.0

    def test_other_kinds_leave_latency_none(self, tmp_path: Path) -> None:
        replies = [
            '<tool_call>{"name": "fs_list", "args": {"path": "."}}</tool_call>',
            "done",
        ]
        events = list(
            run_agent(
                history=[],
                user_text="x",
                client=_ScriptedClient(replies),
                fs_cfg=_cfg(tmp_path),
                stream=False,
            )
        )
        for e in events:
            # tool_result and summary are the two event kinds that
            # legitimately carry timing data.
            if e.kind not in {"tool_result", "summary"}:
                assert e.latency_s is None, f"{e.kind} carried latency"

    def test_latency_reflects_tool_runtime(self, tmp_path: Path) -> None:
        # Inject a custom slow tool and verify latency is at least the
        # sleep duration. Bound is loose to absorb scheduler jitter.
        tools = {**agent_loop.DEFAULT_TOOLS, "slow": _slow_tool}
        replies = [
            '<tool_call>{"name": "slow", "args": {}}</tool_call>',
            "done",
        ]
        events = list(
            run_agent(
                history=[],
                user_text="x",
                client=_ScriptedClient(replies),
                fs_cfg=_cfg(tmp_path),
                stream=False,
                tools=tools,
            )
        )
        result = next(e for e in events if e.kind == "tool_result")
        assert result.latency_s is not None
        assert result.latency_s >= 0.04
