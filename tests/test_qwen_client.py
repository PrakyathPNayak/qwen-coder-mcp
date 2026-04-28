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
