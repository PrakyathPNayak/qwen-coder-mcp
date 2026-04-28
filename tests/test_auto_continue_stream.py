"""Loop 255: auto-continue parity for chat_stream.

Mirrors the loop-254 chat() auto-continue tests but for the SSE
streaming path. The streaming auto-continue driver re-issues the
request when a stream completes with finish_reason="length" and
stitches segments together at the consumer level (the consumer just
sees one continuous yield iterator).
"""
from __future__ import annotations

import json as _json

import httpx
import pytest

from qwen_coder_mcp.qwen_client import (
    ChatMessage,
    TRUNCATION_MARKER,
)
from tests._helpers import make_mock_qwen_client as _make_client


def _sse(chunks: list[str], finish: str | None = None) -> bytes:
    """Build an SSE body. Each chunk is its own delta; the final chunk
    carries the finish_reason if provided."""
    lines: list[str] = []
    for i, chunk in enumerate(chunks):
        obj: dict = {"choices": [{"delta": {"content": chunk}}]}
        if finish is not None and i == len(chunks) - 1:
            obj["choices"][0]["finish_reason"] = finish
        lines.append(f"data: {_json.dumps(obj)}\n")
    lines.append("data: [DONE]\n")
    return ("\n".join(lines) + "\n").encode("utf-8")


def test_stream_two_length_then_stop_concatenates():
    seq = [
        (["alpha", "-one"], "length"),
        (["beta", "-two"], "stop"),
    ]
    calls = {"n": 0}

    def handler(_req):
        i = calls["n"]
        calls["n"] += 1
        return httpx.Response(
            200,
            content=_sse(*seq[i]),
            headers={"content-type": "text/event-stream"},
        )

    c = _make_client(handler)
    out = "".join(c.chat_stream([ChatMessage("user", "go")]))
    assert calls["n"] == 2
    assert "alpha-one" in out and "beta-two" in out
    assert TRUNCATION_MARKER not in out


def test_stream_natural_stop_no_continuation():
    calls = {"n": 0}

    def handler(_req):
        calls["n"] += 1
        return httpx.Response(
            200,
            content=_sse(["done"], "stop"),
            headers={"content-type": "text/event-stream"},
        )

    c = _make_client(handler)
    out = "".join(c.chat_stream([ChatMessage("user", "go")]))
    assert calls["n"] == 1
    assert out == "done"


def test_stream_disabled_via_env_emits_marker(monkeypatch):
    monkeypatch.setenv("QWEN_AUTO_CONTINUE", "0")
    calls = {"n": 0}

    def handler(_req):
        calls["n"] += 1
        return httpx.Response(
            200,
            content=_sse(["partial"], "length"),
            headers={"content-type": "text/event-stream"},
        )

    c = _make_client(handler)
    out = "".join(c.chat_stream([ChatMessage("user", "go")]))
    assert calls["n"] == 1
    assert "partial" in out
    assert TRUNCATION_MARKER in out


def test_stream_round_cap_emits_marker(monkeypatch):
    monkeypatch.setenv("QWEN_AUTO_CONTINUE_MAX_ROUNDS", "2")
    calls = {"n": 0}

    def handler(_req):
        calls["n"] += 1
        return httpx.Response(
            200,
            content=_sse([f"chunk{calls['n']}"], "length"),
            headers={"content-type": "text/event-stream"},
        )

    c = _make_client(handler)
    out = "".join(c.chat_stream([ChatMessage("user", "go")]))
    assert calls["n"] == 2
    assert "chunk1" in out
    assert TRUNCATION_MARKER in out


def test_stream_continuation_payload_contains_assistant_partial():
    seen: list[list[dict]] = []

    def handler(req):
        body = _json.loads(req.content)
        seen.append(body["messages"])
        if len(seen) == 1:
            return httpx.Response(
                200,
                content=_sse(["seg-A"], "length"),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(
            200,
            content=_sse(["seg-B"], "stop"),
            headers={"content-type": "text/event-stream"},
        )

    c = _make_client(handler)
    out = "".join(c.chat_stream([ChatMessage("user", "kickoff")]))
    assert "seg-A" in out and "seg-B" in out
    second = seen[1]
    assert [m["role"] for m in second[-2:]] == ["assistant", "user"]
    assert second[-2]["content"] == "seg-A"
    assert "continue" in second[-1]["content"].lower()


def test_stream_three_length_then_stop():
    seq = [
        (["a"], "length"),
        (["b"], "length"),
        (["c"], "length"),
        (["d"], "stop"),
    ]
    calls = {"n": 0}

    def handler(_req):
        i = calls["n"]
        calls["n"] += 1
        return httpx.Response(
            200,
            content=_sse(*seq[i]),
            headers={"content-type": "text/event-stream"},
        )

    c = _make_client(handler)
    out = "".join(c.chat_stream([ChatMessage("user", "go")]))
    assert out == "abcd"
    assert TRUNCATION_MARKER not in out


def test_stream_empty_partial_breaks_loop():
    """If the stream yields no content but finishes with length (e.g.,
    an entire emission was a stripped think block), continuing would
    spin on empty -- we instead emit the marker and stop."""
    calls = {"n": 0}

    def handler(_req):
        calls["n"] += 1
        # full think block -- stripped to empty by the streaming filter
        return httpx.Response(
            200,
            content=_sse(["<think>thinking only</think>"], "length"),
            headers={"content-type": "text/event-stream"},
        )

    c = _make_client(handler)
    out = "".join(c.chat_stream([ChatMessage("user", "go")]))
    assert calls["n"] == 1
    assert TRUNCATION_MARKER in out
