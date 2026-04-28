"""Pin <think>...</think> stripping in QwenClient response extraction.

Live testing of Qwen3.6-27B against vLLM 0.11 confirmed the model
inlines its chain-of-thought into message.content rather than the
separate reasoning_content channel some other models use. Without
stripping, the agent loop's tool-call regex would match speculative
tool calls the model was *reasoning about* mid-thought.
"""
from __future__ import annotations

import httpx
import pytest

from qwen_coder_mcp.qwen_client import (
    QwenClient,
    QwenError,
    _strip_think_blocks,
)
from tests._helpers import make_mock_qwen_client


def _make_client(handler) -> QwenClient:
    return make_mock_qwen_client(handler)


def _resp(text: str) -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {},
    }


class TestStripThinkBlocksUnit:
    def test_removes_complete_block(self) -> None:
        out = _strip_think_blocks(
            "<think>step1\nstep2</think>\nFinal answer."
        )
        assert out == "Final answer."

    def test_removes_multiple_blocks(self) -> None:
        out = _strip_think_blocks(
            "<think>a</think> middle <think>b</think> tail"
        )
        assert "<think>" not in out
        assert "</think>" not in out
        assert "middle" in out and "tail" in out

    def test_case_insensitive(self) -> None:
        out = _strip_think_blocks("<THINK>x</THINK>final")
        assert out == "final"

    def test_unwrapped_close_only(self) -> None:
        # Qwen3.6 sometimes starts thinking before emitting the open tag
        # and only closes it. Drop everything up to the close.
        text = (
            "Let me think about this.\n"
            "1. analyze input\n"
            "2. plan\n"
            "</think>\n"
            "<tool_call>\n"
            '{"name": "read_file", "arguments": {"path": "/etc/hostname"}}\n'
            "</tool_call>"
        )
        out = _strip_think_blocks(text)
        assert "analyze input" not in out
        assert "<tool_call>" in out

    def test_no_think_tag_passthrough(self) -> None:
        assert _strip_think_blocks("plain text") == "plain text"
        assert _strip_think_blocks("") == ""

    def test_disable_via_env(self, monkeypatch) -> None:
        monkeypatch.setenv("QWEN_DISABLE_THINK_STRIP", "1")
        text = "<think>secret</think>visible"
        assert _strip_think_blocks(text) == text

    def test_dotall_across_newlines(self) -> None:
        out = _strip_think_blocks(
            "<think>\nline1\nline2\nline3\n</think>\nDONE"
        )
        assert out == "DONE"

    def test_strips_surrounding_whitespace(self) -> None:
        assert _strip_think_blocks("  <think>x</think>  Y  ") == "Y"


class TestExtractTextStripsThinking:
    def test_chat_strips_inline_think_block(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=_resp(
                    "<think>The user asked X. I should reply with Y.</think>\nY"
                ),
            )

        qc = _make_client(handler)
        out = qc.chat([{"role": "user", "content": "hi"}], max_retries=1)
        assert out == "Y"
        assert "<think>" not in out
        assert "should reply" not in out

    def test_chat_strips_unwrapped_thinking_with_tool_call(self) -> None:
        # The exact shape live Qwen3.6-27B produced when probed.
        thinking = (
            "The user wants to read the file `/etc/hostname`.\n"
            "I need to use the `read_file` tool.\n"
            "</think>\n\n"
            "<tool_call>\n"
            '{"name": "read_file", "arguments": {"path": "/etc/hostname"}}\n'
            "</tool_call>"
        )

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_resp(thinking))

        qc = _make_client(handler)
        out = qc.chat([{"role": "user", "content": "read it"}], max_retries=1)
        assert out.startswith("<tool_call>")
        assert "I need to use" not in out

    def test_empty_after_strip_raises(self) -> None:
        # Pure-thinking response with no actual answer must not look
        # like a successful empty completion -- the retry loop should
        # see it as a transient failure.
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json=_resp("<think>thinking only</think>")
            )

        qc = _make_client(handler)
        with pytest.raises(QwenError, match="empty assistant content"):
            qc.chat([{"role": "user", "content": "hi"}], max_retries=1)

    def test_passthrough_when_no_think(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_resp("Plain answer."))

        qc = _make_client(handler)
        assert (
            qc.chat([{"role": "user", "content": "hi"}], max_retries=1)
            == "Plain answer."
        )
