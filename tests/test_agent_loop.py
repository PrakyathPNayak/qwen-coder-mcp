"""Tests for the tool-calling agent loop (loop 164)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from qwen_coder_mcp import agent_loop, fs_tools, web_tools
from qwen_coder_mcp.qwen_client import ChatMessage


# ----------------------------------------------------------- helpers
class _ScriptedClient:
    """Yields a fixed sequence of replies on successive .chat() calls."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls: list[list[ChatMessage]] = []

    def chat(self, history):
        self.calls.append([ChatMessage(role=m.role, content=m.content) for m in history])
        if not self._replies:
            return ""
        return self._replies.pop(0)


# ----------------------------------------------------------- parser
class TestParseToolCalls:
    def test_xml_block(self) -> None:
        text = (
            'sure\n<tool_call>\n{"name": "web_search", "args": {"query": "foo"}}'
            "\n</tool_call>\nthat is all"
        )
        calls = agent_loop.parse_tool_calls(text)
        assert len(calls) == 1
        assert calls[0].name == "web_search"
        assert calls[0].args == {"query": "foo"}

    def test_fenced_block(self) -> None:
        text = '```tool_call\n{"name": "fs_read", "args": {"path": "x.py"}}\n```'
        calls = agent_loop.parse_tool_calls(text)
        assert len(calls) == 1
        assert calls[0].name == "fs_read"
        assert calls[0].args["path"] == "x.py"

    def test_multiple_calls_in_order(self) -> None:
        text = (
            '<tool_call>{"name": "a", "args": {}}</tool_call>'
            '<tool_call>{"name": "b", "args": {"x": 1}}</tool_call>'
        )
        calls = agent_loop.parse_tool_calls(text)
        assert [c.name for c in calls] == ["a", "b"]
        assert calls[1].args == {"x": 1}

    def test_no_calls(self) -> None:
        assert agent_loop.parse_tool_calls("just prose") == []

    def test_malformed_json_dropped(self) -> None:
        text = '<tool_call>not json</tool_call><tool_call>{"name": "ok", "args": {}}</tool_call>'
        calls = agent_loop.parse_tool_calls(text)
        assert len(calls) == 1
        assert calls[0].name == "ok"

    def test_arguments_alias(self) -> None:
        # Some models prefer "arguments" over "args" -- accept both.
        text = '<tool_call>{"name": "fs_read", "arguments": {"path": "y"}}</tool_call>'
        calls = agent_loop.parse_tool_calls(text)
        assert calls[0].args == {"path": "y"}

    def test_strip_tool_calls_removes_blocks(self) -> None:
        text = (
            'before\n<tool_call>{"name": "x", "args": {}}</tool_call>\nafter'
        )
        out = agent_loop.strip_tool_calls(text)
        assert "tool_call" not in out
        assert "before" in out and "after" in out


# ----------------------------------------------------------- registry
class TestToolRegistry:
    def test_unknown_tool_returns_error(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        result = agent_loop.run_tool(
            agent_loop.ToolCall(name="nope", args={}, raw=""), fs_cfg=cfg
        )
        assert result.error is True
        assert "unknown tool" in result.output

    def test_fs_read(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("hello world\n")
        cfg = fs_tools.FsConfig(root=tmp_path)
        result = agent_loop.run_tool(
            agent_loop.ToolCall(
                name="fs_read", args={"path": "a.py"}, raw=""
            ),
            fs_cfg=cfg,
        )
        assert "hello world" in result.output

    def test_fs_read_truncates(self, tmp_path: Path) -> None:
        (tmp_path / "big.txt").write_text("y" * 5000)
        cfg = fs_tools.FsConfig(root=tmp_path, max_read_bytes=200_000)
        result = agent_loop.run_tool(
            agent_loop.ToolCall(
                name="fs_read", args={"path": "big.txt", "max_bytes": 100}, raw=""
            ),
            fs_cfg=cfg,
        )
        assert "[truncated]" in result.output

    def test_fs_list(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("x")
        (tmp_path / "sub").mkdir()
        cfg = fs_tools.FsConfig(root=tmp_path)
        result = agent_loop.run_tool(
            agent_loop.ToolCall(name="fs_list", args={"path": "."}, raw=""),
            fs_cfg=cfg,
        )
        assert "a.py" in result.output
        assert "sub" in result.output

    def test_grep(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("hit\nmiss\n")
        cfg = fs_tools.FsConfig(root=tmp_path)
        result = agent_loop.run_tool(
            agent_loop.ToolCall(
                name="grep", args={"pattern": "hit"}, raw=""
            ),
            fs_cfg=cfg,
        )
        assert "hit" in result.output

    def test_grep_missing_pattern(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        result = agent_loop.run_tool(
            agent_loop.ToolCall(name="grep", args={}, raw=""), fs_cfg=cfg
        )
        assert "needs a 'pattern'" in result.output

    def test_web_search(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from qwen_coder_mcp.web_tools import SearchResult

        monkeypatch.setattr(
            web_tools,
            "web_search",
            lambda q, max_results=5: [
                SearchResult(title="t", url="https://u", snippet="s")
            ],
        )
        cfg = fs_tools.FsConfig(root=tmp_path)
        result = agent_loop.run_tool(
            agent_loop.ToolCall(
                name="web_search", args={"query": "x"}, raw=""
            ),
            fs_cfg=cfg,
        )
        assert "https://u" in result.output

    def test_web_fetch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            web_tools,
            "fetch_url",
            lambda url: {"text": "BODY", "status": 200, "url": url},
        )
        cfg = fs_tools.FsConfig(root=tmp_path)
        result = agent_loop.run_tool(
            agent_loop.ToolCall(
                name="web_fetch",
                args={"url": "https://example.com"},
                raw="",
            ),
            fs_cfg=cfg,
        )
        assert "BODY" in result.output
        assert "https://example.com" in result.output

    def test_tool_exception_caught(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)

        def _boom(_args, _cfg):
            raise RuntimeError("boom")

        result = agent_loop.run_tool(
            agent_loop.ToolCall(name="x", args={}, raw=""),
            fs_cfg=cfg,
            tools={"x": _boom},
        )
        assert result.error is True
        assert "RuntimeError" in result.output


# ----------------------------------------------------------- driver
class TestRunAgent:
    def test_no_tool_calls_returns_immediately(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        client = _ScriptedClient(["just an answer"])
        events = list(
            agent_loop.run_agent(
                [], "say hi", client=client, fs_cfg=cfg
            )
        )
        kinds = [e.kind for e in events]
        assert kinds == ["assistant", "final"]
        assert events[-1].text == "just an answer"
        # Exactly one model call.
        assert len(client.calls) == 1

    def test_single_tool_call_then_final(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("def f(): return 1\n")
        cfg = fs_tools.FsConfig(root=tmp_path)
        client = _ScriptedClient(
            [
                '<tool_call>{"name": "fs_read", "args": {"path": "a.py"}}</tool_call>',
                "the function returns 1",
            ]
        )
        history: list[ChatMessage] = []
        events = list(
            agent_loop.run_agent(
                history, "what does a.py do?", client=client, fs_cfg=cfg
            )
        )
        kinds = [e.kind for e in events]
        assert "tool_call" in kinds
        assert "tool_result" in kinds
        assert kinds[-1] == "final"
        assert events[-1].text == "the function returns 1"
        # History got: system, user, assistant(call), user(tool_result), assistant(final)
        roles = [m.role for m in history]
        assert roles == ["system", "user", "assistant", "user", "assistant"]
        assert "tool_result" in history[-2].content

    def test_max_steps_limit(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        # Always emits a tool_call so the loop never naturally finishes.
        looping = (
            '<tool_call>{"name": "fs_list", "args": {"path": "."}}</tool_call>'
        )
        client = _ScriptedClient([looping] * 10)
        events = list(
            agent_loop.run_agent(
                [], "go", client=client, fs_cfg=cfg, max_steps=3
            )
        )
        kinds = [e.kind for e in events]
        assert kinds.count("assistant") == 3
        assert kinds[-2] == "limit"
        assert kinds[-1] == "final"
        assert "stopped after 3 steps" in events[-1].text

    def test_client_exception_handled(self, tmp_path: Path) -> None:
        class _Boom:
            def chat(self, _h):
                raise RuntimeError("network down")

        cfg = fs_tools.FsConfig(root=tmp_path)
        events = list(
            agent_loop.run_agent([], "go", client=_Boom(), fs_cfg=cfg)
        )
        assert events[-1].kind == "final"
        assert "agent error" in events[-1].text
        assert "network down" in events[-1].text

    def test_protocol_doc_in_system_prompt(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        client = _ScriptedClient(["ok"])
        history: list[ChatMessage] = []
        list(agent_loop.run_agent(history, "x", client=client, fs_cfg=cfg))
        assert history[0].role == "system"
        assert "tool_call" in history[0].content
        assert "web_search" in history[0].content

    def test_existing_system_prompt_appended_with_protocol(
        self, tmp_path: Path
    ) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        client = _ScriptedClient(["done"])
        history = [ChatMessage(role="system", content="be terse")]
        list(agent_loop.run_agent(history, "x", client=client, fs_cfg=cfg))
        # Original prompt preserved AND tool protocol added.
        assert history[0].content.startswith("be terse")
        assert "tool_call" in history[0].content

    def test_multiple_tools_one_step(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("a")
        (tmp_path / "b.py").write_text("b")
        cfg = fs_tools.FsConfig(root=tmp_path)
        client = _ScriptedClient(
            [
                '<tool_call>{"name": "fs_read", "args": {"path": "a.py"}}</tool_call>'
                '<tool_call>{"name": "fs_read", "args": {"path": "b.py"}}</tool_call>',
                "I read both",
            ]
        )
        history: list[ChatMessage] = []
        events = list(
            agent_loop.run_agent(
                history, "read both", client=client, fs_cfg=cfg
            )
        )
        tool_calls = [e for e in events if e.kind == "tool_call"]
        assert len(tool_calls) == 2
        # Single tool_result feedback message containing both blocks.
        feedback_msgs = [m for m in history if m.role == "user"]
        # First user msg is the prompt; second is the tool feedback.
        assert len(feedback_msgs) == 2
        feedback = feedback_msgs[1].content
        assert feedback.count("<tool_result") == 2


class TestFormatToolResults:
    def test_renders_each_result(self) -> None:
        out = agent_loop.format_tool_results(
            [
                agent_loop.ToolResult(name="a", output="first"),
                agent_loop.ToolResult(name="b", output="second"),
            ]
        )
        assert '<tool_result name="a">' in out
        assert '<tool_result name="b">' in out
        assert "first" in out
        assert "second" in out
        assert out.count("</tool_result>") == 2
