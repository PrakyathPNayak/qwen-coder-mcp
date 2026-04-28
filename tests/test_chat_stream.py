"""Loop 132 tests: QwenClient.chat_stream SSE parsing."""
from __future__ import annotations

import httpx
import pytest

from qwen_coder_mcp.qwen_client import (
    ChatMessage,
    QwenClient,
    QwenError,
    QwenFatalError,
)
from qwen_coder_mcp.config import Settings


def _make_client(handler) -> QwenClient:
    transport = httpx.MockTransport(handler)
    settings = Settings(
        base_url="http://x/v1",
        api_key="k",
        model="qwen",
        timeout=10.0,
        max_tokens=128,
        server_max_len=2048,
        loop_interval_seconds=60,
        loop_max_file_bytes=200_000,
        loop_push=False,
    )
    c = QwenClient(settings)
    c._client.close()
    c._client = httpx.Client(
        base_url=settings.base_url, transport=transport, timeout=10.0
    )
    return c


def _sse_body(chunks: list[str], done: bool = True) -> bytes:
    import json as _json
    lines: list[str] = []
    for chunk in chunks:
        obj = {"choices": [{"delta": {"content": chunk}}]}
        lines.append(f"data: {_json.dumps(obj)}\n")
    if done:
        lines.append("data: [DONE]\n")
    return ("\n".join(lines) + "\n").encode("utf-8")


class TestChatStream:
    def test_yields_chunks(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=_sse_body(["hel", "lo ", "world"]),
                headers={"content-type": "text/event-stream"},
            )

        c = _make_client(handler)
        out = list(c.chat_stream([ChatMessage(role="user", content="hi")]))
        assert "".join(out) == "hello world"

    def test_skips_done_marker(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = b'data: {"choices":[{"delta":{"content":"a"}}]}\n\ndata: [DONE]\n\n'
            return httpx.Response(
                200, content=body, headers={"content-type": "text/event-stream"}
            )

        c = _make_client(handler)
        out = list(c.chat_stream([{"role": "user", "content": "hi"}]))
        assert out == ["a"]

    def test_skips_malformed_lines(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = (
                b": comment line\n"
                b"data: not-json\n"
                b'data: {"choices":[{"delta":{"content":"ok"}}]}\n'
                b"\n"
                b"data: [DONE]\n"
            )
            return httpx.Response(
                200, content=body, headers={"content-type": "text/event-stream"}
            )

        c = _make_client(handler)
        out = list(c.chat_stream([{"role": "user", "content": "hi"}]))
        assert out == ["ok"]

    def test_5xx_raises_qwenerror(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="bad")

        c = _make_client(handler)
        with pytest.raises(QwenError):
            list(c.chat_stream([{"role": "user", "content": "hi"}]))

    def test_4xx_raises_fatal(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, text="bad request")

        c = _make_client(handler)
        with pytest.raises(QwenFatalError):
            list(c.chat_stream([{"role": "user", "content": "hi"}]))

    def test_429_retriable_raises_qwenerror(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, text="slow down")

        c = _make_client(handler)
        with pytest.raises(QwenError) as exc_info:
            list(c.chat_stream([{"role": "user", "content": "hi"}]))
        assert not isinstance(exc_info.value, QwenFatalError)

    def test_extra_reserved_rejected(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=_sse_body(["x"]))

        c = _make_client(handler)
        with pytest.raises(QwenFatalError, match="reserved"):
            list(
                c.chat_stream(
                    [{"role": "user", "content": "hi"}],
                    extra={"model": "other"},
                )
            )

    def test_content_block_list(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            import json as _json
            obj = {
                "choices": [
                    {"delta": {"content": [{"text": "hello"}, {"text": " world"}]}}
                ]
            }
            body = f"data: {_json.dumps(obj)}\n\ndata: [DONE]\n"
            return httpx.Response(
                200, content=body.encode("utf-8"),
                headers={"content-type": "text/event-stream"},
            )

        c = _make_client(handler)
        out = list(c.chat_stream([{"role": "user", "content": "hi"}]))
        assert out == ["hello", " world"]

    def test_request_payload_has_stream_true(self) -> None:
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json as _json
            seen["payload"] = _json.loads(request.content)
            return httpx.Response(
                200, content=_sse_body(["ok"]),
                headers={"content-type": "text/event-stream"},
            )

        c = _make_client(handler)
        list(c.chat_stream([{"role": "user", "content": "hi"}]))
        assert seen["payload"]["stream"] is True
