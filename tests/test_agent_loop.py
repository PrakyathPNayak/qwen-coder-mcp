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


class _StreamingScriptedClient(_ScriptedClient):
    """Yields fixed replies through .chat_stream() to exercise stream-only paths."""

    def chat_stream(self, history):
        self.calls.append([ChatMessage(role=m.role, content=m.content) for m in history])
        if not self._replies:
            return
        reply = self._replies.pop(0)
        # Deliberately chunk in small pieces so think-tag filtering paths
        # see split tags, not just one perfectly aligned payload.
        for i in range(0, len(reply), 5):
            chunk = reply[i : i + 5]
            if chunk:
                yield chunk


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
        assert kinds == ["assistant", "summary", "final"]
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
        assert "model_start" in kinds
        assert kinds.index("model_start") > kinds.index("tool_result")
        assert kinds[-1] == "final"
        assert events[-1].text == "the function returns 1"
        # History got: system, user, assistant(call), user(tool_result), assistant(final)
        roles = [m.role for m in history]
        assert roles == ["system", "user", "assistant", "user", "assistant"]
        assert "tool_result" in history[-2].content

    def test_streaming_dangling_think_is_stripped_before_final(
        self, tmp_path: Path
    ) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        client = _StreamingScriptedClient(
            ["hidden reasoning that should not render</think>\n\nvisible answer"]
        )
        history: list[ChatMessage] = []
        events = list(
            agent_loop.run_agent(history, "go", client=client, fs_cfg=cfg)
        )
        assert events[-1].kind == "final"
        assert events[-1].text == "visible answer"
        assert "hidden reasoning" not in history[-1].content
        assert "</think>" not in history[-1].content

    def test_streaming_empty_visible_reply_retries(
        self, tmp_path: Path
    ) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        client = _StreamingScriptedClient(
            ["<think>only hidden reasoning</think>", "visible final"]
        )
        history: list[ChatMessage] = []
        events = list(
            agent_loop.run_agent(
                history, "go", client=client, fs_cfg=cfg, max_steps=3
            )
        )
        kinds = [e.kind for e in events]
        assert "empty_retry" in kinds
        assert events[-1].kind == "final"
        assert events[-1].text == "visible final"
        assert len(client.calls) == 2

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
        # Tail order is now: ..., summary, limit, final.
        assert kinds[-3] == "summary"
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


# ----------------------------------------------------------- streaming
class _StreamingClient:
    """Scripted client that supports both chat() and chat_stream().

    chat_stream yields the reply broken into 4-char chunks; chat returns
    the same reply as a single string. Tracks which entrypoint was used.
    """

    def __init__(self, replies):
        self._replies = list(replies)
        self.stream_calls = 0
        self.blocking_calls = 0

    def chat(self, history):
        self.blocking_calls += 1
        if not self._replies:
            return ""
        return self._replies.pop(0)

    def chat_stream(self, history):
        self.stream_calls += 1
        if not self._replies:
            return iter([])
        full = self._replies.pop(0)
        return iter([full[i : i + 4] for i in range(0, len(full), 4)])


class TestRunAgentStreaming:
    def test_emits_chunks_when_stream_true(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        client = _StreamingClient(["hello world from qwen"])
        events = list(
            agent_loop.run_agent(
                [], "ping", client=client, fs_cfg=cfg, stream=True
            )
        )
        chunks = [e for e in events if e.kind == "chunk"]
        assert chunks, "expected per-chunk events when streaming"
        joined = "".join(e.text for e in chunks)
        assert joined == "hello world from qwen"
        assert client.stream_calls == 1 and client.blocking_calls == 0
        finals = [e for e in events if e.kind == "final"]
        assert finals[-1].text == "hello world from qwen"

    def test_falls_back_to_blocking_when_stream_false(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        client = _StreamingClient(["plain reply"])
        events = list(
            agent_loop.run_agent(
                [], "ping", client=client, fs_cfg=cfg, stream=False
            )
        )
        assert client.stream_calls == 0 and client.blocking_calls == 1
        assert not [e for e in events if e.kind == "chunk"]

    def test_streaming_with_tool_call(self, tmp_path: Path) -> None:
        (tmp_path / "x.txt").write_text("hi", encoding="utf-8")
        cfg = fs_tools.FsConfig(root=tmp_path)
        client = _StreamingClient(
            [
                '<tool_call>{"name":"fs_read","args":{"path":"x.txt"}}</tool_call>',
                "done reading",
            ]
        )
        events = list(
            agent_loop.run_agent(
                [], "read", client=client, fs_cfg=cfg, stream=True
            )
        )
        kinds = [e.kind for e in events]
        assert "chunk" in kinds and "tool_call" in kinds and "tool_result" in kinds
        # Two model turns -> chat_stream invoked twice
        assert client.stream_calls == 2


# ----------------------------------------------------------- write tools
class TestWriteTools:
    def test_fs_write_creates_file(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        client = _ScriptedClient(
            [
                '<tool_call>{"name":"fs_write","args":{"path":"new.txt","content":"hello"}}</tool_call>',
                "wrote it",
            ]
        )
        events = list(
            agent_loop.run_agent(
                [],
                "write a file",
                client=client,
                fs_cfg=cfg,
                tools=agent_loop.ALL_TOOLS,
            )
        )
        results = [e for e in events if e.kind == "tool_result"]
        assert results and "wrote" in results[0].text
        assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "hello"

    def test_default_tools_excludes_write(self) -> None:
        assert "fs_write" not in agent_loop.DEFAULT_TOOLS
        assert "apply_patch" not in agent_loop.DEFAULT_TOOLS
        assert "fs_write" in agent_loop.ALL_TOOLS
        assert "apply_patch" in agent_loop.ALL_TOOLS

    def test_write_tool_unavailable_in_default_registry(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        client = _ScriptedClient(
            [
                '<tool_call>{"name":"fs_write","args":{"path":"x.txt","content":"y"}}</tool_call>',
                "ok",
            ]
        )
        events = list(
            agent_loop.run_agent([], "write", client=client, fs_cfg=cfg)
        )
        results = [e for e in events if e.kind == "tool_result"]
        assert results and "unknown tool" in results[0].text.lower()
        assert not (tmp_path / "x.txt").exists()

    def test_apply_patch_check_only(self, tmp_path: Path) -> None:
        # Initialise a tiny git repo so `git apply` has something to chew on.
        import subprocess
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        (tmp_path / "a.txt").write_text("one\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "-m", "init"],
            cwd=tmp_path, check=True,
        )
        diff = (
            "diff --git a/a.txt b/a.txt\n"
            "--- a/a.txt\n+++ b/a.txt\n"
            "@@ -1 +1 @@\n-one\n+two\n"
        )
        cfg = fs_tools.FsConfig(root=tmp_path)
        out = agent_loop._tool_apply_patch(
            {"diff": diff, "check_only": True}, cfg
        )
        assert "ok" in out.lower() or "check" in out.lower()
        # File unchanged because check_only=True
        assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "one\n"


# ----------------------------------------------------------- confirm hook
class TestConfirmHook:
    def test_destructive_set_matches_write_tools(self) -> None:
        assert agent_loop.DESTRUCTIVE_TOOLS == frozenset(
            agent_loop.WRITE_TOOLS.keys()
        )

    def test_default_confirm_allows(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        call = agent_loop.ToolCall(
            name="fs_write", args={"path": "a.txt", "content": "hi"}, raw=""
        )
        # No confirm passed -> destructive call still runs.
        res = agent_loop.run_tool(call, fs_cfg=cfg, tools=agent_loop.ALL_TOOLS)
        assert not res.error
        assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "hi"

    def test_confirm_can_deny(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        seen: list[str] = []

        def deny(call: agent_loop.ToolCall) -> bool:
            seen.append(call.name)
            return False

        call = agent_loop.ToolCall(
            name="fs_write", args={"path": "x.txt", "content": "y"}, raw=""
        )
        res = agent_loop.run_tool(
            call, fs_cfg=cfg, tools=agent_loop.ALL_TOOLS, confirm=deny
        )
        assert res.error
        assert "denied" in res.output.lower()
        assert seen == ["fs_write"]
        assert not (tmp_path / "x.txt").exists()

    def test_confirm_skipped_for_readonly_tools(self, tmp_path: Path) -> None:
        (tmp_path / "r.txt").write_text("data", encoding="utf-8")
        cfg = fs_tools.FsConfig(root=tmp_path)
        seen: list[str] = []

        def watch(call: agent_loop.ToolCall) -> bool:
            seen.append(call.name)
            return False  # would deny if asked

        call = agent_loop.ToolCall(
            name="fs_read", args={"path": "r.txt"}, raw=""
        )
        res = agent_loop.run_tool(
            call, fs_cfg=cfg, tools=agent_loop.ALL_TOOLS, confirm=watch
        )
        # Read tool was not destructive, confirm not consulted.
        assert seen == []
        assert not res.error
        assert "data" in res.output

    def test_confirm_propagates_through_run_agent(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        client = _ScriptedClient(
            [
                '<tool_call>{"name":"fs_write","args":{"path":"z.txt","content":"q"}}</tool_call>',
                "noted",
            ]
        )
        seen: list[str] = []

        def watch(call: agent_loop.ToolCall) -> bool:
            seen.append(call.name)
            return False

        events = list(
            agent_loop.run_agent(
                [],
                "write z",
                client=client,
                fs_cfg=cfg,
                tools=agent_loop.ALL_TOOLS,
                confirm=watch,
                stream=False,
            )
        )
        results = [e for e in events if e.kind == "tool_result"]
        assert results and "denied" in results[0].text.lower()
        assert seen == ["fs_write"]
        assert not (tmp_path / "z.txt").exists()


# ----------------------------------------------------------- run_shell tool
class TestRunShellTool:
    def test_run_shell_in_write_registry_only(self) -> None:
        assert "run_shell" in agent_loop.WRITE_TOOLS
        assert "run_shell" in agent_loop.ALL_TOOLS
        assert "run_shell" in agent_loop.DESTRUCTIVE_TOOLS
        assert "run_shell" not in agent_loop.DEFAULT_TOOLS

    def test_run_shell_basic(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        out = agent_loop._tool_run_shell({"cmd": "echo hello"}, cfg)
        assert "hello" in out
        assert "$ echo hello" in out

    def test_run_shell_empty_cmd(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        out = agent_loop._tool_run_shell({"cmd": ""}, cfg)
        assert out.startswith("error:")

    def test_run_shell_denylist_blocks(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        out = agent_loop._tool_run_shell({"cmd": "rm -rf /"}, cfg)
        # ShellError is caught and returned as a denied message.
        assert out.startswith("denied:")

    def test_run_shell_through_run_tool_with_confirm(
        self, tmp_path: Path
    ) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        seen: list[str] = []

        def deny(call: agent_loop.ToolCall) -> bool:
            seen.append(call.name)
            return False

        call = agent_loop.ToolCall(
            name="run_shell", args={"cmd": "echo nope"}, raw=""
        )
        res = agent_loop.run_tool(
            call, fs_cfg=cfg, tools=agent_loop.ALL_TOOLS, confirm=deny
        )
        assert res.error
        assert "denied" in res.output.lower()
        assert seen == ["run_shell"]

    def test_run_shell_unavailable_in_default_registry(
        self, tmp_path: Path
    ) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        client = _ScriptedClient(
            [
                '<tool_call>{"name":"run_shell","args":{"cmd":"echo x"}}</tool_call>',
                "ok",
            ]
        )
        events = list(
            agent_loop.run_agent([], "run", client=client, fs_cfg=cfg)
        )
        results = [e for e in events if e.kind == "tool_result"]
        assert results and "unknown tool" in results[0].text.lower()


# ---------------------------------------------------------------------------
# Loop 290: new tools -- http_request, json_query, env_get, cp
# ---------------------------------------------------------------------------

class TestHttpRequestTool:
    def test_get_returns_status(self, tmp_path: Path) -> None:
        from unittest.mock import patch, MagicMock
        import io
        cfg = fs_tools.FsConfig(root=tmp_path)
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.status = 200
        mock_resp.headers = {"Content-Type": "text/plain"}
        mock_resp.read.return_value = b"hello world"
        with patch("urllib.request.urlopen", return_value=mock_resp):
            r = agent_loop._tool_http_request({"url": "http://example.com"}, cfg)
        assert "status=200" in r
        assert "hello world" in r

    def test_missing_url_returns_error(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        r = agent_loop._tool_http_request({}, cfg)
        assert "error" in r

    def test_bad_method_returns_error(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        r = agent_loop._tool_http_request({"url": "http://example.com", "method": "ZORK"}, cfg)
        assert "unsupported method" in r

    def test_in_default_tools(self) -> None:
        assert "http_request" in agent_loop.DEFAULT_TOOLS

    def test_in_write_tools_for_mutating_methods(self) -> None:
        assert "http_request" in agent_loop.WRITE_TOOLS
        assert "http_request" in agent_loop.DESTRUCTIVE_TOOLS

    def test_default_registry_blocks_post_before_network(
        self, tmp_path: Path
    ) -> None:
        from unittest.mock import patch

        cfg = fs_tools.FsConfig(root=tmp_path)
        call = agent_loop.ToolCall(
            name="http_request",
            args={"url": "http://example.com", "method": "POST", "body": "x"},
            raw="",
        )
        with patch("urllib.request.urlopen") as urlopen:
            res = agent_loop.run_tool(
                call, fs_cfg=cfg, tools=agent_loop.DEFAULT_TOOLS
            )
        assert res.error is False
        assert "requires write-mode" in res.output
        urlopen.assert_not_called()

    def test_safe_http_method_skips_confirm_in_write_registry(
        self, tmp_path: Path
    ) -> None:
        from unittest.mock import MagicMock, patch

        cfg = fs_tools.FsConfig(root=tmp_path)
        seen: list[str] = []
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.status = 200
        mock_resp.headers = {"Content-Type": "text/plain"}
        mock_resp.read.return_value = b"ok"

        def deny(call: agent_loop.ToolCall) -> bool:
            seen.append(call.name)
            return False

        call = agent_loop.ToolCall(
            name="http_request",
            args={"url": "http://example.com", "method": "GET"},
            raw="",
        )
        with patch("urllib.request.urlopen", return_value=mock_resp):
            res = agent_loop.run_tool(
                call, fs_cfg=cfg, tools=agent_loop.ALL_TOOLS, confirm=deny
            )
        assert seen == []
        assert "status=200" in res.output

    def test_mutating_http_method_uses_confirm(
        self, tmp_path: Path
    ) -> None:
        from unittest.mock import patch

        cfg = fs_tools.FsConfig(root=tmp_path)
        seen: list[str] = []

        def deny(call: agent_loop.ToolCall) -> bool:
            seen.append(call.name)
            return False

        call = agent_loop.ToolCall(
            name="http_request",
            args={"url": "http://example.com", "method": "POST", "body": "x"},
            raw="",
        )
        with patch("urllib.request.urlopen") as urlopen:
            res = agent_loop.run_tool(
                call, fs_cfg=cfg, tools=agent_loop.ALL_TOOLS, confirm=deny
            )
        assert res.error
        assert "denied" in res.output
        assert seen == ["http_request"]
        urlopen.assert_not_called()

    def test_has_blurb(self) -> None:
        assert "http_request" in agent_loop.TOOL_BLURBS


class TestJsonQueryTool:
    def _cfg(self, tmp_path: Path) -> fs_tools.FsConfig:
        return fs_tools.FsConfig(root=tmp_path)

    def test_simple_path(self, tmp_path: Path) -> None:
        r = agent_loop._tool_json_query({"json": '{"a":{"b":42}}', "path": "a.b"}, self._cfg(tmp_path))
        assert r.strip() == "42"

    def test_list_index(self, tmp_path: Path) -> None:
        r = agent_loop._tool_json_query({"json": '[10,20,30]', "path": "1"}, self._cfg(tmp_path))
        assert r.strip() == "20"

    def test_root_path(self, tmp_path: Path) -> None:
        r = agent_loop._tool_json_query({"json": '{"x":1}', "path": "."}, self._cfg(tmp_path))
        import json
        assert json.loads(r) == {"x": 1}

    def test_invalid_json_returns_error(self, tmp_path: Path) -> None:
        r = agent_loop._tool_json_query({"json": "not json"}, self._cfg(tmp_path))
        assert "error" in r

    def test_missing_key_returns_error(self, tmp_path: Path) -> None:
        r = agent_loop._tool_json_query({"json": '{"a":1}', "path": "b"}, self._cfg(tmp_path))
        assert "error" in r

    def test_in_default_tools(self) -> None:
        assert "json_query" in agent_loop.DEFAULT_TOOLS


class TestEnvGetTool:
    def _cfg(self, tmp_path: Path) -> fs_tools.FsConfig:
        return fs_tools.FsConfig(root=tmp_path)

    def test_read_home(self, tmp_path: Path) -> None:
        r = agent_loop._tool_env_get({"name": "HOME"}, self._cfg(tmp_path))
        assert r and r != "[not set]"

    def test_sensitive_redacted(self, tmp_path: Path) -> None:
        import os
        orig = os.environ.get("GITHUB_TOKEN")
        os.environ["GITHUB_TOKEN"] = "ghp_secret123"
        try:
            r = agent_loop._tool_env_get({"name": "GITHUB_TOKEN"}, self._cfg(tmp_path))
            assert "REDACTED" in r
            assert "ghp_secret" not in r
        finally:
            if orig is None:
                os.environ.pop("GITHUB_TOKEN", None)
            else:
                os.environ["GITHUB_TOKEN"] = orig

    def test_missing_name_returns_error(self, tmp_path: Path) -> None:
        r = agent_loop._tool_env_get({}, self._cfg(tmp_path))
        assert "error" in r

    def test_unset_returns_marker(self, tmp_path: Path) -> None:
        r = agent_loop._tool_env_get({"name": "QWEN_TEST_UNSET_XYZ"}, self._cfg(tmp_path))
        assert r == "[not set]"

    def test_multi_returns_json(self, tmp_path: Path) -> None:
        import json
        r = agent_loop._tool_env_get({"names": ["HOME", "PATH"]}, self._cfg(tmp_path))
        obj = json.loads(r)
        assert "HOME" in obj and "PATH" in obj

    def test_in_default_tools(self) -> None:
        assert "env_get" in agent_loop.DEFAULT_TOOLS


class TestCpTool:
    def _cfg(self, tmp_path: Path) -> fs_tools.FsConfig:
        return fs_tools.FsConfig(root=tmp_path)

    def test_basic_copy(self, tmp_path: Path) -> None:
        src = tmp_path / "a.txt"
        src.write_text("hello")
        r = agent_loop._tool_cp({"src": "a.txt", "dst": "b.txt"}, self._cfg(tmp_path))
        assert "copied" in r
        assert (tmp_path / "b.txt").read_text() == "hello"

    def test_overwrite_false_blocks(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("orig")
        (tmp_path / "b.txt").write_text("existing")
        r = agent_loop._tool_cp({"src": "a.txt", "dst": "b.txt"}, self._cfg(tmp_path))
        assert "error" in r

    def test_overwrite_true_replaces(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("new")
        (tmp_path / "b.txt").write_text("old")
        r = agent_loop._tool_cp({"src": "a.txt", "dst": "b.txt", "overwrite": True}, self._cfg(tmp_path))
        assert "copied" in r
        assert (tmp_path / "b.txt").read_text() == "new"

    def test_missing_src_returns_error(self, tmp_path: Path) -> None:
        r = agent_loop._tool_cp({"src": "nope.txt", "dst": "b.txt"}, self._cfg(tmp_path))
        assert "error" in r

    def test_in_write_tools(self) -> None:
        assert "cp" in agent_loop.WRITE_TOOLS
        assert "cp" not in agent_loop.DEFAULT_TOOLS
