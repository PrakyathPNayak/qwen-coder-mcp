"""Tests for the ``ttft`` (time-to-first-token) event ``run_agent``
emits exactly once per streaming model turn."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Iterator

from qwen_coder_mcp import fs_tools
from qwen_coder_mcp.agent_loop import run_agent
from qwen_coder_mcp.qwen_client import ChatMessage


def _cfg(tmp_path: Path) -> fs_tools.FsConfig:
    return fs_tools.FsConfig(root=tmp_path)


class _StreamingClient:
    """Streams a queued sequence of replies chunk-by-chunk. Each call to
    ``chat_stream`` pops the next reply and yields it one token at a
    time with optional first-token delay."""

    def __init__(self, replies: list[str], first_chunk_delay: float = 0.0) -> None:
        self._replies = list(replies)
        self.first_chunk_delay = first_chunk_delay

    def chat_stream(self, _h: list[ChatMessage]) -> Iterator[str]:
        if not self._replies:
            yield "done"
            return
        text = self._replies.pop(0)
        # Two-character chunks so we get more than one yield.
        for i in range(0, len(text), 2):
            if i == 0 and self.first_chunk_delay:
                time.sleep(self.first_chunk_delay)
            yield text[i : i + 2]


class _BlockingClient:
    """No ``chat_stream`` attr → ``run_agent`` falls back to ``chat``."""

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
            **kw,
        )
    )


class TestTTFTEvent:
    def test_emitted_before_first_chunk(self, tmp_path: Path) -> None:
        events = _events(_StreamingClient(["hello"]), tmp_path)
        kinds = [e.kind for e in events]
        # ttft must precede the first chunk.
        assert "ttft" in kinds and "chunk" in kinds
        assert kinds.index("ttft") < kinds.index("chunk")

    def test_one_per_model_turn(self, tmp_path: Path) -> None:
        # Two-step run: tool call then plain final → two model turns →
        # exactly two ttft events.
        replies = [
            '<tool_call>{"name": "fs_list", "args": {"path": "."}}</tool_call>',
            "all done",
        ]
        events = _events(_StreamingClient(replies), tmp_path)
        assert sum(1 for e in events if e.kind == "ttft") == 2

    def test_latency_reflects_first_chunk_delay(
        self, tmp_path: Path
    ) -> None:
        events = _events(
            _StreamingClient(["hello"], first_chunk_delay=0.05),
            tmp_path,
        )
        ttft = next(e for e in events if e.kind == "ttft")
        assert ttft.latency_s is not None
        assert ttft.latency_s >= 0.04

    def test_blocking_client_emits_no_ttft(self, tmp_path: Path) -> None:
        # No chat_stream → no streaming path → no ttft events.
        events = _events(_BlockingClient(["just an answer"]), tmp_path)
        assert all(e.kind != "ttft" for e in events)

    def test_stream_disabled_emits_no_ttft(self, tmp_path: Path) -> None:
        events = _events(_StreamingClient(["x"]), tmp_path, stream=False)
        assert all(e.kind != "ttft" for e in events)

    def test_empty_chunks_dont_trigger_ttft(self, tmp_path: Path) -> None:
        # A client that yields only empty strings should never emit ttft.
        class _EmptyOnly:
            def chat_stream(self, _h):
                yield ""
                yield ""

            def chat(self, _h):
                return ""

        events = _events(_EmptyOnly(), tmp_path)
        assert all(e.kind != "ttft" for e in events)
