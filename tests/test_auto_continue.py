"""Loop 254: auto-continue across finish_reason="length" boundaries.

When the backend reports the model hit its max_tokens budget, ``chat()``
now appends the partial output back as an assistant turn and re-issues
the request, transparently stitching the segments together until the
model finishes naturally, the round cap fires, or the chat budget
expires. These tests pin the contract.
"""
from __future__ import annotations

import httpx
import pytest

from qwen_coder_mcp.config import Settings
from qwen_coder_mcp.qwen_client import (
    ChatMessage,
    QwenClient,
    TRUNCATION_MARKER,
)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(
        "qwen_coder_mcp.qwen_client.time.sleep", lambda _s: None
    )


def _client_with(handler) -> QwenClient:
    settings = Settings(
        base_url="http://test/v1",
        api_key="EMPTY",
        model="qwen3.6-27b",
        timeout=5,
        max_tokens=64,
        server_max_len=2048,
        loop_interval_seconds=1,
        loop_max_file_bytes=1000,
        loop_push=False,
    )
    c = QwenClient(settings=settings)
    c._client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url=settings.base_url,
        timeout=settings.timeout,
    )
    return c


def _resp(content: str, finish: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": finish,
                }
            ]
        },
    )


def test_two_length_then_stop_concatenates():
    seq = [
        ("part one", "length"),
        ("part two", "length"),
        ("part three", "stop"),
    ]
    calls = {"n": 0}

    def handler(_req):
        i = calls["n"]
        calls["n"] += 1
        return _resp(*seq[i])

    c = _client_with(handler)
    out = c.chat([ChatMessage("user", "go")])
    assert calls["n"] == 3
    assert "part one" in out and "part two" in out and "part three" in out
    assert TRUNCATION_MARKER not in out


def test_natural_stop_no_continuation():
    calls = {"n": 0}

    def handler(_req):
        calls["n"] += 1
        return _resp("done in one shot", "stop")

    c = _client_with(handler)
    out = c.chat([ChatMessage("user", "go")])
    assert out == "done in one shot"
    assert calls["n"] == 1


def test_disabled_via_env_preserves_marker(monkeypatch):
    monkeypatch.setenv("QWEN_AUTO_CONTINUE", "0")
    calls = {"n": 0}

    def handler(_req):
        calls["n"] += 1
        return _resp("partial", "length")

    c = _client_with(handler)
    out = c.chat([ChatMessage("user", "go")])
    assert calls["n"] == 1
    assert TRUNCATION_MARKER in out
    assert "partial" in out


def test_round_cap_emits_marker(monkeypatch):
    monkeypatch.setenv("QWEN_AUTO_CONTINUE_MAX_ROUNDS", "2")
    calls = {"n": 0}

    def handler(_req):
        calls["n"] += 1
        return _resp(f"chunk{calls['n']}", "length")

    c = _client_with(handler)
    out = c.chat([ChatMessage("user", "go")])
    # 2 rounds means 2 continuations after the first call -> 3 total? No:
    # rounds_done counts each truncated response. We bail when
    # rounds_done >= max_rounds, so calls == max_rounds.
    assert calls["n"] == 2
    assert "chunk1" in out
    assert TRUNCATION_MARKER in out


def test_max_rounds_zero_disables(monkeypatch):
    monkeypatch.setenv("QWEN_AUTO_CONTINUE_MAX_ROUNDS", "0")
    calls = {"n": 0}

    def handler(_req):
        calls["n"] += 1
        return _resp("only", "length")

    c = _client_with(handler)
    out = c.chat([ChatMessage("user", "go")])
    assert calls["n"] == 1
    assert "only" in out
    assert TRUNCATION_MARKER in out


def test_invalid_max_rounds_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("QWEN_AUTO_CONTINUE_MAX_ROUNDS", "not-a-number")
    from qwen_coder_mcp.qwen_client import _auto_continue_max_rounds

    assert _auto_continue_max_rounds() == 8


def test_negative_max_rounds_clamps_to_zero(monkeypatch):
    monkeypatch.setenv("QWEN_AUTO_CONTINUE_MAX_ROUNDS", "-5")
    from qwen_coder_mcp.qwen_client import _auto_continue_max_rounds

    assert _auto_continue_max_rounds() == 0


def test_continuation_appends_assistant_and_user_nudge():
    """Verify request payload of the 2nd call contains the assistant
    partial plus the continuation nudge."""
    seen: list[list[dict]] = []

    def handler(req):
        import json as _json

        body = _json.loads(req.content)
        seen.append(body["messages"])
        if len(seen) == 1:
            return _resp("partial-A", "length")
        return _resp("part-B", "stop")

    c = _client_with(handler)
    out = c.chat([ChatMessage("user", "kickoff")])
    assert "partial-A" in out and "part-B" in out
    # Second request has the partial appended as assistant + nudge.
    second = seen[1]
    roles = [m["role"] for m in second]
    assert roles[-2:] == ["assistant", "user"]
    assert second[-2]["content"] == "partial-A"
    assert "continue" in second[-1]["content"].lower()


def test_intermediate_marker_stripped_between_segments():
    """If the backend wraps a partial in an explicit marker, we strip
    it before stitching to the next segment."""
    seq = [
        (f"chunk-1\n\n{TRUNCATION_MARKER}", "length"),
        ("chunk-2", "stop"),
    ]
    calls = {"n": 0}

    def handler(_req):
        i = calls["n"]
        calls["n"] += 1
        return _resp(*seq[i])

    c = _client_with(handler)
    out = c.chat([ChatMessage("user", "go")])
    assert "chunk-1" in out and "chunk-2" in out
    # No leftover marker since model finished naturally on round 2.
    assert TRUNCATION_MARKER not in out


def test_empty_continuation_segment_breaks_loop(monkeypatch):
    """If a truncated segment is empty (e.g., entire span was a stripped
    think block), we stop continuing rather than spin forever."""
    calls = {"n": 0}

    def handler(_req):
        calls["n"] += 1
        return _resp("<think>only thinking</think>", "length")

    c = _client_with(handler)
    out = c.chat([ChatMessage("user", "go")])
    assert calls["n"] == 1
    assert out == TRUNCATION_MARKER


def test_custom_continuation_prompt_used(monkeypatch):
    monkeypatch.setenv("QWEN_AUTO_CONTINUE_PROMPT", "KEEP-GOING-MARKER")
    seen: list[list[dict]] = []

    def handler(req):
        import json as _json

        seen.append(_json.loads(req.content)["messages"])
        if len(seen) == 1:
            return _resp("a", "length")
        return _resp("b", "stop")

    c = _client_with(handler)
    c.chat([ChatMessage("user", "go")])
    assert seen[1][-1]["content"] == "KEEP-GOING-MARKER"


def test_three_truncations_then_stop():
    seq = [
        ("seg1", "length"),
        ("seg2", "length"),
        ("seg3", "length"),
        ("seg4", "stop"),
    ]
    calls = {"n": 0}

    def handler(_req):
        i = calls["n"]
        calls["n"] += 1
        return _resp(*seq[i])

    c = _client_with(handler)
    out = c.chat([ChatMessage("user", "go")])
    for s in ("seg1", "seg2", "seg3", "seg4"):
        assert s in out
    assert TRUNCATION_MARKER not in out


def test_first_call_natural_stop_with_auto_continue_off(monkeypatch):
    monkeypatch.setenv("QWEN_AUTO_CONTINUE", "0")

    def handler(_req):
        return _resp("hello", "stop")

    c = _client_with(handler)
    assert c.chat([ChatMessage("user", "hi")]) == "hello"


def test_auto_continue_enabled_default():
    from qwen_coder_mcp.qwen_client import _auto_continue_enabled

    assert _auto_continue_enabled() is True


def test_auto_continue_off_values(monkeypatch):
    from qwen_coder_mcp.qwen_client import _auto_continue_enabled

    for v in ("0", "false", "False", "no", "NO"):
        monkeypatch.setenv("QWEN_AUTO_CONTINUE", v)
        assert _auto_continue_enabled() is False, v


def test_continuation_does_not_mutate_caller_messages():
    seq = [("p1", "length"), ("p2", "stop")]
    calls = {"n": 0}

    def handler(_req):
        i = calls["n"]
        calls["n"] += 1
        return _resp(*seq[i])

    c = _client_with(handler)
    msgs = [ChatMessage("user", "go")]
    c.chat(msgs)
    assert len(msgs) == 1 and msgs[0].role == "user"
