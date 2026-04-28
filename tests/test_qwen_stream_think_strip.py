"""Pin streaming-mode <think>...</think> stripping in QwenClient.

Loop 217 stripped think blocks from the non-streaming chat() path.
The streaming path (chat_stream) has tags potentially split across
chunks, so it needs a stateful filter. This module pins both the
filter unit-tests and end-to-end behaviour through a mocked SSE
stream.
"""
from __future__ import annotations

import json

import httpx
import pytest

from qwen_coder_mcp.qwen_client import _StreamingThinkStripFilter
from tests._helpers import make_mock_qwen_client


def _accumulate(filt: _StreamingThinkStripFilter, chunks: list[str]) -> str:
    out: list[str] = []
    for c in chunks:
        out.append(filt.feed(c))
    out.append(filt.flush())
    return "".join(out)


class TestStreamingThinkStripFilter:
    def test_passthrough_no_tags(self) -> None:
        f = _StreamingThinkStripFilter()
        assert _accumulate(f, ["hello ", "world"]) == "hello world"

    def test_complete_block_in_one_chunk(self) -> None:
        f = _StreamingThinkStripFilter()
        chunks = ["pre <think>secret</think> post"]
        assert _accumulate(f, chunks) == "pre  post"

    def test_open_tag_split_across_chunks(self) -> None:
        f = _StreamingThinkStripFilter()
        # The open tag itself is split: "<thi" + "nk>x</think>after"
        chunks = ["pre <thi", "nk>secret</think>after"]
        out = _accumulate(f, chunks)
        assert "secret" not in out
        assert "after" in out
        assert "<thi" not in out

    def test_close_tag_split_across_chunks(self) -> None:
        f = _StreamingThinkStripFilter()
        chunks = ["<think>some thinking</thi", "nk>final answer"]
        out = _accumulate(f, chunks)
        assert "thinking" not in out
        assert out.endswith("final answer")

    def test_block_spans_many_chunks(self) -> None:
        f = _StreamingThinkStripFilter()
        chunks = [
            "before ",
            "<think>",
            "step 1\n",
            "step 2\n",
            "step 3\n",
            "</think>",
            " visible",
        ]
        assert _accumulate(f, chunks) == "before  visible"

    def test_no_leak_when_tag_is_at_chunk_boundary(self) -> None:
        f = _StreamingThinkStripFilter()
        # The naive (per-chunk) regex misses this. Verifies the
        # tail-buffer logic actually works.
        chunks = ["safe text <", "think>secret</think> more"]
        out = _accumulate(f, chunks)
        assert "secret" not in out
        assert "<" not in out
        assert "safe text " in out
        assert " more" in out

    def test_lone_lt_at_end_held(self) -> None:
        # A trailing '<' must not be released until we know it isn't
        # part of an open tag. The flush() at end releases it cleanly.
        f = _StreamingThinkStripFilter()
        out_mid = f.feed("text<")
        assert "<" not in out_mid
        assert f.flush() == "<"

    def test_truncated_mid_block_drops_tail(self) -> None:
        # Stream ends inside a think block (no close tag arrived).
        # The thinking content must be dropped, not flushed.
        f = _StreamingThinkStripFilter()
        chunks = ["<think>partial thought without close"]
        out = _accumulate(f, chunks)
        assert out == ""

    def test_disabled_via_env(self, monkeypatch) -> None:
        monkeypatch.setenv("QWEN_DISABLE_THINK_STRIP", "1")
        f = _StreamingThinkStripFilter()
        chunks = ["<think>raw</think>after"]
        assert _accumulate(f, chunks) == "<think>raw</think>after"

    def test_multiple_blocks(self) -> None:
        f = _StreamingThinkStripFilter()
        chunks = ["<think>a</think>X", "<think>b</think>Y"]
        assert _accumulate(f, chunks) == "XY"

    def test_case_insensitive(self) -> None:
        f = _StreamingThinkStripFilter()
        assert _accumulate(f, ["<THINK>x</THINK>visible"]) == "visible"

    def test_lt_in_normal_text_safe(self) -> None:
        # '<' in normal code (e.g., HTML, comparisons) must not stick.
        f = _StreamingThinkStripFilter()
        assert _accumulate(f, ["if x < y: print(x)"]) == "if x < y: print(x)"

    def test_partial_tag_followed_by_non_tag(self) -> None:
        # '<thingy>' should NOT be stripped (not a think tag).
        f = _StreamingThinkStripFilter()
        chunks = ["a<thi", "ngy>b"]
        assert _accumulate(f, chunks) == "a<thingy>b"


class TestChatStreamThinkStripping:
    """Drive QwenClient.chat_stream through MockTransport with a
    fabricated SSE response and assert the consumer never sees
    think-block content."""

    def _make_sse(self, chunks: list[str]) -> bytes:
        lines: list[str] = []
        for c in chunks:
            payload = {
                "choices": [{"delta": {"content": c}}],
            }
            lines.append(f"data: {json.dumps(payload)}")
        lines.append("data: [DONE]")
        return ("\n\n".join(lines) + "\n\n").encode("utf-8")

    def _client(self, body: bytes) -> object:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=body,
                headers={"Content-Type": "text/event-stream"},
            )

        return make_mock_qwen_client(handler)

    def test_streaming_strips_think_block(self) -> None:
        body = self._make_sse(
            [
                "Reading the file. ",
                "<think>",
                "The user asked me to ",
                "read /etc/hostname.",
                "</think>",
                "Final answer: ",
                "ubuntu",
            ]
        )
        qc = self._client(body)
        out = "".join(
            qc.chat_stream([{"role": "user", "content": "x"}])
        )
        assert "<think>" not in out
        assert "</think>" not in out
        assert "user asked" not in out
        assert "Final answer: ubuntu" in out
        assert out.startswith("Reading the file. ")

    def test_streaming_passthrough_no_thinking(self) -> None:
        body = self._make_sse(["plain ", "answer ", "ok"])
        qc = self._client(body)
        out = "".join(
            qc.chat_stream([{"role": "user", "content": "x"}])
        )
        assert out == "plain answer ok"

    def test_streaming_handles_split_open_tag(self) -> None:
        body = self._make_sse(
            ["before <thi", "nk>secret</think> after"]
        )
        qc = self._client(body)
        out = "".join(
            qc.chat_stream([{"role": "user", "content": "x"}])
        )
        assert "secret" not in out
        assert out.startswith("before ")
        assert "after" in out

    def test_streaming_disabled_lets_think_through(self, monkeypatch) -> None:
        monkeypatch.setenv("QWEN_DISABLE_THINK_STRIP", "1")
        body = self._make_sse(["<think>raw</think>visible"])
        qc = self._client(body)
        out = "".join(
            qc.chat_stream([{"role": "user", "content": "x"}])
        )
        assert "<think>raw</think>visible" == out
