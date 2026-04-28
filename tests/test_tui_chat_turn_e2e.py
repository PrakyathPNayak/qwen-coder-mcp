"""End-to-end TUI chat-turn tests against httpx.MockTransport.

The existing tests cover (a) the QwenClient HTTP layer with MockTransport
and (b) the TUI dispatcher with a FakeClient that bypasses HTTP entirely.
What was missing: integration of TUI's `chat_turn` and `chat_turn_stream`
end-to-end through a *real* QwenClient backed by MockTransport. This
catches drift between TUI expectations and the actual HTTP/SSE layer
(payload shape, error-handling, history mutation, @-mention expansion).

This test file was deferred six times across the loop log and is the
final piece of the dry-run-vs-reality safety net begun in loop 211.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from qwen_coder_mcp import fs_tools, tui
from qwen_coder_mcp.qwen_client import ChatMessage


from tests._helpers import make_mock_qwen_client as _make_client


def _completion_body(text: str) -> bytes:
    return json.dumps(
        {"choices": [{"message": {"role": "assistant", "content": text}}]}
    ).encode("utf-8")


def _sse_body(chunks: list[str]) -> bytes:
    lines: list[str] = []
    for chunk in chunks:
        obj = {"choices": [{"delta": {"content": chunk}}]}
        lines.append(f"data: {json.dumps(obj)}\n")
    lines.append("data: [DONE]\n")
    return ("\n".join(lines) + "\n").encode("utf-8")


class TestChatTurnHappyPath:
    def test_history_grows_user_then_assistant(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                content=_completion_body("hello back"),
                headers={"content-type": "application/json"},
            )

        client = _make_client(handler)
        history: list[ChatMessage] = []
        reply = tui.chat_turn(history, "hi there", client=client)
        assert reply == "hello back"
        # System prompt was inserted at index 0.
        assert history[0].role == "system"
        assert history[1].role == "user" and history[1].content == "hi there"
        assert history[2].role == "assistant" and history[2].content == "hello back"
        # Request body shape is OpenAI-compatible.
        body = captured["body"]
        assert body["model"] == "qwen"
        assert body["stream"] is False
        assert body["messages"][-1] == {"role": "user", "content": "hi there"}

    def test_authorization_header_propagates(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["auth"] = request.headers.get("authorization")
            return httpx.Response(200, content=_completion_body("ok"))

        client = _make_client(handler)
        tui.chat_turn([], "ping", client=client)
        assert captured["auth"] == "Bearer k"


class TestChatTurnErrorPaths:
    def test_500_yields_friendly_error_history_user_kept(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        client = _make_client(handler)
        history: list[ChatMessage] = []
        reply = tui.chat_turn(history, "hi", client=client)
        assert "chat error" in reply.lower() or "boom" in reply.lower()
        # User message still in history (the friendly error is returned to
        # the caller; chat_turn does not append a polluted assistant
        # message because client.chat raised).
        roles = [m.role for m in history]
        assert "user" in roles
        # No assistant message was appended on failure.
        assert "assistant" not in roles

    def test_400_fatal_yields_error_string_not_assistant(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, text="bad request")

        client = _make_client(handler)
        history: list[ChatMessage] = []
        reply = tui.chat_turn(history, "hi", client=client)
        assert "chat error" in reply.lower()
        assert all(m.role != "assistant" for m in history)

    def test_connection_error_returns_serve_hint(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        client = _make_client(handler)
        reply = tui.chat_turn([], "hi", client=client)
        # _friendly_chat_error must recognise this and emit the
        # serve_qwen.sh hint that we put there in earlier loops.
        assert "serve_qwen" in reply or "qwen server" in reply.lower()


class TestChatTurnStream:
    def test_yields_chunks_and_commits_final_reply(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert json.loads(request.content)["stream"] is True
            return httpx.Response(
                200,
                content=_sse_body(["he", "llo", " ", "world"]),
                headers={"content-type": "text/event-stream"},
            )

        client = _make_client(handler)
        history: list[ChatMessage] = []
        chunks = []
        accums = []
        for chunk, accum in tui.chat_turn_stream(history, "hi", client=client):
            chunks.append(chunk)
            accums.append(accum)
        assert "".join(chunks) == "hello world"
        assert accums[-1] == "hello world"
        # Final assistant reply committed to history.
        assert history[-1].role == "assistant"
        assert history[-1].content == "hello world"

    def test_stream_error_yields_error_chunk_no_assistant(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="kaboom")

        client = _make_client(handler)
        history: list[ChatMessage] = []
        outputs = list(tui.chat_turn_stream(history, "hi", client=client))
        assert outputs, "expected at least one error chunk yielded"
        last_chunk, _ = outputs[-1]
        assert "stream error" in last_chunk
        # User message stays so the user can retry; assistant message was
        # NOT committed because the stream failed.
        assert all(m.role != "assistant" for m in history)


class TestAtMentionExpansion:
    def test_at_path_expanded_in_request_body(self, tmp_path: Path) -> None:
        # Create a tiny file that should get inlined.
        target = tmp_path / "hello.txt"
        target.write_text("INLINED-CONTENT-MARKER\n", encoding="utf-8")

        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(200, content=_completion_body("ok"))

        client = _make_client(handler)
        cfg = fs_tools.FsConfig(root=tmp_path)
        tui.chat_turn(
            [],
            "look at @hello.txt please",
            client=client,
            fs_cfg=cfg,
        )
        # The user message in the outgoing request should contain the
        # inlined file content, not just the @-mention.
        sent_user = captured["body"]["messages"][-1]["content"]
        assert "INLINED-CONTENT-MARKER" in sent_user, (
            f"@-mention not expanded; sent: {sent_user!r}"
        )


class TestHealthCheck:
    def test_ok_payload_extracts_model_ids(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/models")
            return httpx.Response(
                200,
                content=json.dumps(
                    {"data": [{"id": "qwen"}, {"id": "qwen-coder"}]}
                ).encode(),
            )

        client = _make_client(handler)
        result = client.health_check()
        assert result["ok"] is True
        assert "qwen" in result["models"]

    def test_connect_error_returns_actionable_hint(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("nope")

        client = _make_client(handler)
        result = client.health_check()
        assert result["ok"] is False
        assert "serve_qwen.sh" in (result.get("hint") or "")

    def test_401_suggests_api_key_check(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, text="unauthorized")

        client = _make_client(handler)
        result = client.health_check()
        assert result["ok"] is False
        assert "api key" in (result.get("hint") or "").lower()
