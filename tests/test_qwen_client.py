"""Tests for `QwenClient.chat` retry / fail-fast behaviour and the
content extractor.

Uses `httpx.MockTransport` so no network is touched and `time.sleep`
is monkey-patched to a no-op so tests stay fast.
"""
from __future__ import annotations

import httpx
import pytest

from qwen_coder_mcp.config import Settings
from qwen_coder_mcp.qwen_client import (
    ChatMessage,
    QwenClient,
    QwenError,
    QwenFatalError,
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


def _ok_response(text: str = "hi") -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"role": "assistant", "content": text}}]},
    )


# ----------------------------------------------------------- happy path
def test_chat_returns_assistant_text():
    def handler(_request):
        return _ok_response("hello world")

    c = _client_with(handler)
    out = c.chat([ChatMessage("user", "hi")])
    assert out == "hello world"


# ----------------------------------------------------- 4xx is fail-fast
def test_400_fails_fast_no_retry():
    calls = {"n": 0}

    def handler(_request):
        calls["n"] += 1
        return httpx.Response(400, json={"error": "bad payload"})

    c = _client_with(handler)
    with pytest.raises(QwenFatalError) as ei:
        c.chat([ChatMessage("user", "hi")])
    assert "400" in str(ei.value)
    assert calls["n"] == 1, "must not retry on non-retriable 4xx"


def test_401_fails_fast():
    calls = {"n": 0}

    def handler(_request):
        calls["n"] += 1
        return httpx.Response(401, text="unauthorized")

    c = _client_with(handler)
    with pytest.raises(QwenFatalError):
        c.chat([ChatMessage("user", "hi")])
    assert calls["n"] == 1


def test_403_fails_fast():
    calls = {"n": 0}

    def handler(_request):
        calls["n"] += 1
        return httpx.Response(403, text="forbidden")

    c = _client_with(handler)
    with pytest.raises(QwenFatalError):
        c.chat([ChatMessage("user", "hi")])
    assert calls["n"] == 1


def test_422_fails_fast():
    calls = {"n": 0}

    def handler(_request):
        calls["n"] += 1
        return httpx.Response(422, text="unprocessable")

    c = _client_with(handler)
    with pytest.raises(QwenFatalError):
        c.chat([ChatMessage("user", "hi")])
    assert calls["n"] == 1


# ------------------------------------------------ 5xx and 408/429 retry
def test_500_retries_and_eventually_raises():
    calls = {"n": 0}

    def handler(_request):
        calls["n"] += 1
        return httpx.Response(500, text="boom")

    c = _client_with(handler)
    with pytest.raises(QwenError):
        c.chat([ChatMessage("user", "hi")], max_retries=3)
    assert calls["n"] == 3


def test_429_retries_then_succeeds():
    calls = {"n": 0}

    def handler(_request):
        calls["n"] += 1
        if calls["n"] < 2:
            return httpx.Response(429, text="rate limit")
        return _ok_response("after backoff")

    c = _client_with(handler)
    out = c.chat([ChatMessage("user", "hi")], max_retries=3)
    assert out == "after backoff"
    assert calls["n"] == 2


def test_408_is_retried():
    calls = {"n": 0}

    def handler(_request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(408, text="timeout")
        return _ok_response("ok")

    c = _client_with(handler)
    out = c.chat([ChatMessage("user", "hi")], max_retries=5)
    assert out == "ok"
    assert calls["n"] == 3


def test_5xx_then_success():
    calls = {"n": 0}

    def handler(_request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="unavailable")
        return _ok_response("recovered")

    c = _client_with(handler)
    out = c.chat([ChatMessage("user", "hi")])
    assert out == "recovered"


# --------------------------------------------- malformed response handling
def test_malformed_response_no_choices_retried_then_raises():
    def handler(_request):
        return httpx.Response(200, json={"choices": []})

    c = _client_with(handler)
    with pytest.raises(QwenError):
        c.chat([ChatMessage("user", "hi")], max_retries=2)


def test_content_as_list_of_blocks_extracted():
    """Some backends return content as a list of `{"type": "text", ...}`."""

    def handler(_request):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": [
                                {"type": "text", "text": "alpha "},
                                {"type": "text", "text": "beta"},
                            ],
                        }
                    }
                ]
            },
        )

    c = _client_with(handler)
    out = c.chat([ChatMessage("user", "hi")])
    assert "alpha" in out and "beta" in out


# --------------------------------------------------- system_user passthrough
def test_system_user_forwards_temperature_max_tokens_top_p_stop_extra():
    seen: dict = {}

    def handler(request):
        import json
        seen.update(json.loads(request.content))
        return _ok_response("ok")

    c = _client_with(handler)
    out = c.system_user(
        "sys", "usr",
        temperature=0.05,
        max_tokens=128,
        top_p=0.42,
        stop=["</s>", "EOF"],
        extra={"presence_penalty": 0.1},
    )
    assert out == "ok"
    assert seen["temperature"] == 0.05
    assert seen["max_tokens"] == 128
    assert seen["top_p"] == 0.42
    assert seen["stop"] == ["</s>", "EOF"]
    assert seen["presence_penalty"] == 0.1
    # And the messages were assembled correctly:
    assert seen["messages"][0] == {"role": "system", "content": "sys"}
    assert seen["messages"][1] == {"role": "user", "content": "usr"}


def test_system_user_default_kwargs_match_chat_defaults():
    seen: dict = {}

    def handler(request):
        import json
        seen.update(json.loads(request.content))
        return _ok_response("ok")

    c = _client_with(handler)
    c.system_user("sys", "usr")
    # Defaults: temperature=0.2, top_p=0.95, max_tokens falls back to settings.max_tokens (64).
    assert seen["temperature"] == 0.2
    assert seen["top_p"] == 0.95
    assert seen["max_tokens"] == 64
    assert "stop" not in seen


def test_system_user_max_retries_forwarded():
    """If max_retries=1 is forwarded, a 5xx burst raises after one attempt."""
    calls = {"n": 0}

    def handler(_request):
        calls["n"] += 1
        return httpx.Response(503, json={"error": "down"})

    c = _client_with(handler)
    with pytest.raises(QwenError):
        c.system_user("sys", "usr", max_retries=1)
    assert calls["n"] == 1


# --------------------------------------------------- empty-content handling
def test_empty_string_content_raises_qwen_error_after_retries():
    """`content=""` is a backend failure, not a clean empty answer. The
    chat() retry loop then surfaces QwenError after exhausting retries."""
    calls = {"n": 0}

    def handler(_request):
        calls["n"] += 1
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": ""}}]},
        )

    c = _client_with(handler)
    with pytest.raises(QwenError):
        c.chat([ChatMessage("user", "hi")])
    # Default max_retries=3
    assert calls["n"] == 3


def test_none_content_raises_qwen_error():
    calls = {"n": 0}

    def handler(_request):
        calls["n"] += 1
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": None}}]},
        )

    c = _client_with(handler)
    with pytest.raises(QwenError):
        c.chat([ChatMessage("user", "hi")], max_retries=2)
    assert calls["n"] == 2


def test_empty_blocks_list_raises_qwen_error():
    def handler(_request):
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": []}}]},
        )

    c = _client_with(handler)
    with pytest.raises(QwenError):
        c.chat([ChatMessage("user", "hi")], max_retries=1)


def test_blocks_list_with_empty_text_raises_qwen_error():
    def handler(_request):
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant",
                                              "content": [{"text": "  "}]}}]},
        )

    c = _client_with(handler)
    with pytest.raises(QwenError):
        c.chat([ChatMessage("user", "hi")], max_retries=1)


def test_whitespace_only_content_raises_qwen_error():
    def handler(_request):
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "   \n\t"}}]},
        )

    c = _client_with(handler)
    with pytest.raises(QwenError):
        c.chat([ChatMessage("user", "hi")], max_retries=1)


# ----------------------------------------- extra-key reserved-key gating
def test_extra_cannot_override_model():
    c = _client_with(lambda r: _ok_response("ok"))
    with pytest.raises(QwenFatalError) as ei:
        c.chat([ChatMessage("user", "hi")], extra={"model": "evil"})
    assert "model" in str(ei.value)


def test_extra_cannot_override_messages():
    c = _client_with(lambda r: _ok_response("ok"))
    with pytest.raises(QwenFatalError) as ei:
        c.chat([ChatMessage("user", "hi")], extra={"messages": []})
    assert "messages" in str(ei.value)


def test_extra_cannot_override_stream():
    c = _client_with(lambda r: _ok_response("ok"))
    with pytest.raises(QwenFatalError) as ei:
        c.chat([ChatMessage("user", "hi")], extra={"stream": True})
    assert "stream" in str(ei.value)


def test_extra_lists_all_conflicting_keys():
    c = _client_with(lambda r: _ok_response("ok"))
    with pytest.raises(QwenFatalError) as ei:
        c.chat(
            [ChatMessage("user", "hi")],
            extra={"model": "x", "stream": True, "presence_penalty": 0.1},
        )
    msg = str(ei.value)
    assert "model" in msg and "stream" in msg
    assert "presence_penalty" not in msg  # non-reserved isn't flagged


def test_extra_with_only_safe_keys_still_works():
    seen: dict = {}

    def handler(request):
        import json
        seen.update(json.loads(request.content))
        return _ok_response("ok")

    c = _client_with(handler)
    out = c.chat(
        [ChatMessage("user", "hi")],
        extra={"presence_penalty": 0.2, "top_k": 40, "repetition_penalty": 1.05},
    )
    assert out == "ok"
    assert seen["presence_penalty"] == 0.2
    assert seen["top_k"] == 40
    assert seen["repetition_penalty"] == 1.05
    # Reserved keys remain client-controlled.
    assert seen["model"] == "qwen3.6-27b"
    assert seen["stream"] is False


# ----------------------------------------- chat() wall-clock budget
def test_chat_budget_helper_default(monkeypatch):
    from qwen_coder_mcp import qwen_client as Q
    monkeypatch.delenv("QWEN_CHAT_BUDGET_S", raising=False)
    assert Q._chat_total_budget_seconds() == 300.0


def test_chat_budget_helper_env_override(monkeypatch):
    from qwen_coder_mcp import qwen_client as Q
    monkeypatch.setenv("QWEN_CHAT_BUDGET_S", "42.5")
    assert Q._chat_total_budget_seconds() == 42.5


def test_chat_budget_helper_invalid(monkeypatch):
    from qwen_coder_mcp import qwen_client as Q
    monkeypatch.setenv("QWEN_CHAT_BUDGET_S", "nope")
    assert Q._chat_total_budget_seconds() == 300.0
    monkeypatch.setenv("QWEN_CHAT_BUDGET_S", "0")
    assert Q._chat_total_budget_seconds() == 300.0
    monkeypatch.setenv("QWEN_CHAT_BUDGET_S", "-1")
    assert Q._chat_total_budget_seconds() == 300.0


def test_chat_aborts_on_budget_after_first_attempt(monkeypatch):
    """Drive monotonic forward past the deadline before the second
    retry. The loop must raise QwenError with `budget exceeded`."""
    from qwen_coder_mcp import qwen_client as Q
    import itertools

    # First read sets the deadline (t0 + 300s); subsequent reads jump
    # far past it so the very first iteration's pre-attempt check
    # AFTER the first retry's sleep tail will trip.
    ticks = itertools.chain(
        [1000.0],   # deadline computation: t0
        [1000.5],   # first iteration's pre-attempt check (under deadline)
        [99999.0],  # remaining-time check before sleep -> remaining <= 0
        itertools.repeat(99999.0),  # second iteration's pre-attempt check
    )
    monkeypatch.setattr(Q.time, "monotonic", lambda: next(ticks))

    def transient_handler(_request):
        return httpx.Response(503, text="transient")

    c = _client_with(transient_handler)
    with pytest.raises(QwenError) as ei:
        c.chat([ChatMessage("user", "hi")], max_retries=5)
    msg = str(ei.value)
    assert "budget exceeded" in msg or "failed after" in msg


def test_chat_succeeds_within_budget():
    """Sanity: a successful first-try call doesn't trip the budget."""
    c = _client_with(lambda _r: _ok_response("ok"))
    assert c.chat([ChatMessage("user", "hi")]) == "ok"
