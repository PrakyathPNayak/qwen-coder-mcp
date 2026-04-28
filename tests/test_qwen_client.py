"""Tests for `QwenClient.chat` retry / fail-fast behaviour and the
content extractor.

Uses `httpx.MockTransport` so no network is touched and `time.sleep`
is monkey-patched to a no-op so tests stay fast.
"""
from __future__ import annotations

import json

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


def test_chat_budget_clamps_absurd_value(monkeypatch):
    from qwen_coder_mcp import qwen_client as Q
    monkeypatch.setenv("QWEN_CHAT_BUDGET_S", "99999")
    assert Q._chat_total_budget_seconds() == 3600.0


def test_chat_budget_at_cap(monkeypatch):
    from qwen_coder_mcp import qwen_client as Q
    monkeypatch.setenv("QWEN_CHAT_BUDGET_S", "3600")
    assert Q._chat_total_budget_seconds() == 3600.0


# ----------------------------------------------------------- health_check
class TestHealthCheck:
    def test_ok_returns_models(self):
        def handler(request):
            assert request.url.path.endswith("/models")
            return httpx.Response(
                200,
                json={"data": [{"id": "qwen3.6-27b"}, {"id": "qwen-int4"}]},
            )
        c = _client_with(handler)
        res = c.health_check()
        assert res["ok"] is True
        assert "qwen3.6-27b" in res["models"]

    def test_connection_refused_has_hint(self):
        def handler(_request):
            raise httpx.ConnectError("[Errno 111] Connection refused")
        c = _client_with(handler)
        res = c.health_check()
        assert res["ok"] is False
        assert "connection refused" in res["error"].lower()
        assert "serve_qwen" in (res.get("hint") or "")

    def test_401_suggests_api_key(self):
        def handler(_request):
            return httpx.Response(401, text="unauthorized")
        c = _client_with(handler)
        res = c.health_check()
        assert res["ok"] is False
        assert "401" in res["error"]
        assert "api key" in (res.get("hint") or "").lower()

    def test_500_no_hint(self):
        def handler(_request):
            return httpx.Response(500, text="boom")
        c = _client_with(handler)
        res = c.health_check()
        assert res["ok"] is False
        assert res.get("hint") is None

    def test_malformed_json_does_not_crash(self):
        def handler(_request):
            return httpx.Response(200, text="not json")
        c = _client_with(handler)
        res = c.health_check()
        assert res["ok"] is True
        assert res["models"] == []

    def test_connection_refused_includes_base_url(self):
        def handler(_request):
            raise httpx.ConnectError("[Errno 111] Connection refused")
        c = _client_with(handler)
        res = c.health_check()
        assert res["ok"] is False
        assert "http://test/v1" in res["error"], (
            "ConnectError must include the configured base_url so the user "
            "knows which host/port refused the connection"
        )
        assert "http://test/v1/models" in (res.get("hint") or ""), (
            "hint should suggest a curl probe against the same base_url"
        )

    def test_timeout_error_includes_base_url(self):
        def handler(_request):
            raise httpx.ConnectTimeout("timed out")
        c = _client_with(handler)
        res = c.health_check()
        assert res["ok"] is False
        assert "http://test/v1" in res["error"]

    def test_generic_http_error_includes_base_url(self):
        def handler(_request):
            raise httpx.ReadError("read failed")
        c = _client_with(handler)
        res = c.health_check()
        assert res["ok"] is False
        assert "http://test/v1" in res["error"]
        assert "ReadError" in res["error"]


# ----------------------------------------------------- max_tokens clamping
class TestResolveMaxTokens:
    """vLLM raises VLLMValidationError when max_tokens > max_model_len.
    The client clamps the requested completion budget against
    settings.server_max_len so the request goes through with a smaller
    completion instead of a 400 from upstream. See loop 158."""

    def _client(self, server_max_len: int, max_tokens: int = 4096) -> QwenClient:
        settings = Settings(
            base_url="http://test/v1",
            api_key="EMPTY",
            model="qwen3.6-27b",
            timeout=5,
            max_tokens=max_tokens,
            server_max_len=server_max_len,
            loop_interval_seconds=1,
            loop_max_file_bytes=1000,
            loop_push=False,
        )
        c = QwenClient(settings=settings)
        c._client = httpx.Client(
            transport=httpx.MockTransport(lambda r: _ok_response("ok")),
            base_url=settings.base_url,
            timeout=settings.timeout,
        )
        return c

    def test_clamps_against_server_max_len(self):
        c = self._client(server_max_len=2048, max_tokens=4096)
        out = c._resolve_max_tokens([ChatMessage(role="user", content="hi")], None)
        assert out <= 2048
        assert out > 0

    def test_short_prompt_keeps_most_of_budget(self):
        c = self._client(server_max_len=2048, max_tokens=1024)
        # "hi" is one estimated token, headroom is 64, so the cap from the
        # server side is 2048 - 1 - 64 ~= 1983. Requested budget is 1024.
        # min(1024, 1983) == 1024.
        out = c._resolve_max_tokens(
            [ChatMessage(role="user", content="hi")], None
        )
        assert out == 1024

    def test_long_prompt_eats_into_completion_budget(self):
        c = self._client(server_max_len=2048, max_tokens=4096)
        big = "x" * 8000  # ~2000 tokens, larger than the 2048 cap minus 64 headroom
        out = c._resolve_max_tokens(
            [ChatMessage(role="user", content=big)], None
        )
        # Must be at least 1 (we always send something) and well below 4096.
        assert 1 <= out < 100

    def test_explicit_request_still_clamped(self):
        c = self._client(server_max_len=2048, max_tokens=1024)
        out = c._resolve_max_tokens(
            [ChatMessage(role="user", content="hi")], requested=4096
        )
        assert out <= 2048

    def test_zero_server_max_len_disables_clamp(self):
        c = self._client(server_max_len=0, max_tokens=4096)
        out = c._resolve_max_tokens(
            [ChatMessage(role="user", content="hi")], None
        )
        assert out == 4096

    def test_dict_messages_estimate_tokens_too(self):
        c = self._client(server_max_len=2048, max_tokens=4096)
        out = c._resolve_max_tokens(
            [{"role": "user", "content": "x" * 8000}], None
        )
        assert 1 <= out < 100

    def test_chat_payload_sends_clamped_max_tokens(self):
        seen: dict = {}

        def handler(request):
            import json as _j
            seen.update(_j.loads(request.content))
            return _ok_response("ok")

        settings = Settings(
            base_url="http://test/v1",
            api_key="EMPTY",
            model="qwen3.6-27b",
            timeout=5,
            max_tokens=4096,
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
        c.chat([ChatMessage(role="user", content="hi")])
        assert seen["max_tokens"] <= 2048, (
            "client must clamp max_tokens before sending so vllm does "
            "not raise VLLMValidationError"
        )


# ============================================================ Loop 236
# finish_reason="length" must be surfaced, not silently truncated.
class TestTruncationLoop236:
    """Loop 236: when vLLM returns finish_reason='length' the model
    has hit max_tokens mid-completion. The prior code silently returned
    the partial text, which the user perceived as 'query stops
    prematurely'. Now we append a marker and log a warning. When the
    truncation falls inside an unclosed <think> block (Qwen3-Next),
    _strip_think_blocks would have eaten the entire response; we
    return the marker alone instead of raising QwenError so the
    caller doesn't burn retries on the same budget."""

    @staticmethod
    def _length_response(content: str) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "length",
                    }
                ]
            },
        )

    def test_truncation_marker_appended_when_finish_reason_length(self):
        from qwen_coder_mcp.qwen_client import TRUNCATION_MARKER

        def handler(_req):
            return self._length_response("here is a partial answer that ran out")

        c = _client_with(handler)
        out = c.chat([ChatMessage("user", "tell me everything")])
        assert "here is a partial answer" in out
        assert TRUNCATION_MARKER in out

    def test_truncation_marker_not_appended_when_finish_reason_stop(self):
        from qwen_coder_mcp.qwen_client import TRUNCATION_MARKER

        def handler(_req):
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ]
                },
            )

        c = _client_with(handler)
        out = c.chat([ChatMessage("user", "hi")])
        assert TRUNCATION_MARKER not in out
        assert out == "ok"

    def test_unclosed_think_at_length_returns_marker_not_qwen_error(self):
        from qwen_coder_mcp.qwen_client import TRUNCATION_MARKER, QwenError

        # Model emits open <think> then runs out before closing tag.
        # _strip_think_blocks finds no </think>, so it returns the raw
        # text -- but downstream parsers see a useless prefix. With
        # finish_reason=length we instead return the marker so retry
        # logic and human-readable surfaces both get a clear signal.
        # However: when text has no </think> the strip is a no-op and
        # raw_text is non-empty, so we currently fall through to the
        # "truncated, append marker" branch. Pin that contract.
        def handler(_req):
            return self._length_response("<think>\nstill thinking when budget hit")

        c = _client_with(handler)
        # No QwenError because finish_reason=length AND text non-empty.
        try:
            out = c.chat([ChatMessage("user", "explain")])
        except QwenError:
            pytest.fail("should not raise on truncated unclosed-think")
        assert TRUNCATION_MARKER in out

    def test_truncation_inside_closed_think_returns_marker_only_if_empty(self):
        """When the entire emitted span was a complete <think>...</think>
        that gets stripped to empty AND finish_reason=length, return the
        marker alone instead of raising QwenError."""
        from qwen_coder_mcp.qwen_client import TRUNCATION_MARKER

        def handler(_req):
            return self._length_response("<think>only thinking</think>")

        c = _client_with(handler)
        out = c.chat([ChatMessage("user", "x")])
        assert out == TRUNCATION_MARKER

    def test_truncation_marker_idempotent_on_repeated_extraction(self):
        from qwen_coder_mcp.qwen_client import (
            QwenClient,
            TRUNCATION_MARKER,
        )

        # Calling _extract_text twice on same dict shouldn't double-append.
        data = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": f"answer {TRUNCATION_MARKER}",
                    },
                    "finish_reason": "length",
                }
            ]
        }
        out = QwenClient._extract_text(data)
        assert out.count(TRUNCATION_MARKER) == 1

    def test_finish_reason_absent_treated_as_stop(self):
        """Older vLLM payloads / mocks that omit finish_reason must NOT
        get the truncation marker."""
        from qwen_coder_mcp.qwen_client import TRUNCATION_MARKER

        def handler(_req):
            return _ok_response("complete answer")

        c = _client_with(handler)
        out = c.chat([ChatMessage("user", "hi")])
        assert TRUNCATION_MARKER not in out


# ============================================================ Loop 237
# Streaming-path parity for finish_reason=length truncation marker.
class TestStreamTruncationLoop237:
    """Loop 237: stream_chat now mirrors loop 236's non-stream behaviour
    by yielding TRUNCATION_MARKER after the final flush when the SSE
    stream's last finish_reason was 'length'. Without this, streaming
    consumers (TUI, agent loop streaming path) silently saw a partial
    answer that looked like a premature stop."""

    @staticmethod
    def _sse_response(chunks: list[str]) -> httpx.Response:
        body = "\n".join(chunks) + "\n"
        return httpx.Response(
            200,
            text=body,
            headers={"content-type": "text/event-stream"},
        )

    @staticmethod
    def _delta(content: str, finish_reason: str | None = None) -> str:
        choice: dict = {"delta": {"content": content}}
        if finish_reason is not None:
            choice["finish_reason"] = finish_reason
        return "data: " + json.dumps({"choices": [choice]})

    def test_stream_yields_marker_when_finish_reason_length(self):
        from qwen_coder_mcp.qwen_client import TRUNCATION_MARKER

        chunks = [
            self._delta("partial answer "),
            self._delta("that was cut off", finish_reason="length"),
            "data: [DONE]",
        ]

        def handler(_req):
            return self._sse_response(chunks)

        c = _client_with(handler)
        out = "".join(c.chat_stream([ChatMessage("user", "hi")]))
        assert "partial answer that was cut off" in out
        assert TRUNCATION_MARKER in out

    def test_stream_no_marker_when_finish_reason_stop(self):
        from qwen_coder_mcp.qwen_client import TRUNCATION_MARKER

        chunks = [
            self._delta("complete "),
            self._delta("answer", finish_reason="stop"),
            "data: [DONE]",
        ]

        def handler(_req):
            return self._sse_response(chunks)

        c = _client_with(handler)
        out = "".join(c.chat_stream([ChatMessage("user", "hi")]))
        assert TRUNCATION_MARKER not in out
        assert "complete answer" in out

    def test_stream_no_marker_when_no_finish_reason(self):
        from qwen_coder_mcp.qwen_client import TRUNCATION_MARKER

        chunks = [
            self._delta("answer"),
            "data: [DONE]",
        ]

        def handler(_req):
            return self._sse_response(chunks)

        c = _client_with(handler)
        out = "".join(c.chat_stream([ChatMessage("user", "hi")]))
        assert TRUNCATION_MARKER not in out

    def test_stream_marker_emitted_even_without_done_sentinel(self):
        """If the server closes the connection without sending [DONE]
        we must still emit the marker after the final flush."""
        from qwen_coder_mcp.qwen_client import TRUNCATION_MARKER

        chunks = [
            self._delta("truncated"),
            self._delta("", finish_reason="length"),
        ]

        def handler(_req):
            return self._sse_response(chunks)

        c = _client_with(handler)
        out = "".join(c.chat_stream([ChatMessage("user", "hi")]))
        assert TRUNCATION_MARKER in out

    def test_stream_finish_reason_latched_not_overwritten_by_null(self):
        """finish_reason='length' on chunk N must not be overwritten by
        a missing/null finish_reason on chunk N+1."""
        from qwen_coder_mcp.qwen_client import TRUNCATION_MARKER

        chunks = [
            self._delta("a", finish_reason="length"),
            self._delta(""),  # null finish_reason
            "data: [DONE]",
        ]

        def handler(_req):
            return self._sse_response(chunks)

        c = _client_with(handler)
        out = "".join(c.chat_stream([ChatMessage("user", "hi")]))
        assert TRUNCATION_MARKER in out


# ============================================================ Loop 238
# repetition_penalty defaults so Qwen3-Next doesn't degenerate into
# n-gram loops at low temperature.
class TestRepetitionPenaltyLoop238:
    """Loop 238: user reported the model was repeating itself after a
    while. Root cause: the codebase pinned temperature=0.2 everywhere
    but never set any repetition control. Qwen3-Next's own
    generation_config.json recommends temp=1.0 + top_k=20 + top_p=0.95
    precisely because the model loops at low temperature without a rep
    penalty. We add a default repetition_penalty=1.05 to every chat
    request to break loops without distorting code-generation output."""

    def test_chat_default_payload_includes_repetition_penalty(self):
        seen: dict = {}

        def handler(req):
            seen.update(json.loads(req.content.decode("utf-8")))
            return _ok_response("ok")

        c = _client_with(handler)
        c.chat([ChatMessage("user", "hi")])
        assert "repetition_penalty" in seen
        assert seen["repetition_penalty"] == pytest.approx(1.05)

    def test_chat_stream_payload_includes_repetition_penalty(self):
        seen: dict = {}

        def handler(req):
            seen.update(json.loads(req.content.decode("utf-8")))
            return httpx.Response(
                200,
                text='data: {"choices":[{"delta":{"content":"x"},"finish_reason":"stop"}]}\ndata: [DONE]\n',
                headers={"content-type": "text/event-stream"},
            )

        c = _client_with(handler)
        list(c.chat_stream([ChatMessage("user", "hi")]))
        assert seen.get("repetition_penalty") == pytest.approx(1.05)

    def test_explicit_repetition_penalty_kwarg_overrides_default(self):
        seen: dict = {}

        def handler(req):
            seen.update(json.loads(req.content.decode("utf-8")))
            return _ok_response("ok")

        c = _client_with(handler)
        c.chat([ChatMessage("user", "hi")], repetition_penalty=1.15)
        assert seen["repetition_penalty"] == pytest.approx(1.15)

    def test_extra_can_override_repetition_penalty(self):
        seen: dict = {}

        def handler(req):
            seen.update(json.loads(req.content.decode("utf-8")))
            return _ok_response("ok")

        c = _client_with(handler)
        c.chat([ChatMessage("user", "hi")], extra={"repetition_penalty": 1.0})
        assert seen["repetition_penalty"] == pytest.approx(1.0)

    def test_env_var_overrides_default(self, monkeypatch):
        from qwen_coder_mcp import qwen_client as qc

        monkeypatch.setenv("QWEN_REPETITION_PENALTY", "1.10")
        assert qc._default_repetition_penalty() == pytest.approx(1.10)

    def test_env_var_invalid_falls_back_to_safe_default(self, monkeypatch):
        from qwen_coder_mcp import qwen_client as qc

        monkeypatch.setenv("QWEN_REPETITION_PENALTY", "not-a-number")
        assert qc._default_repetition_penalty() == pytest.approx(1.05)

    def test_env_var_zero_or_negative_falls_back(self, monkeypatch):
        from qwen_coder_mcp import qwen_client as qc

        monkeypatch.setenv("QWEN_REPETITION_PENALTY", "0")
        assert qc._default_repetition_penalty() == pytest.approx(1.05)
        monkeypatch.setenv("QWEN_REPETITION_PENALTY", "-1")
        assert qc._default_repetition_penalty() == pytest.approx(1.05)

    def test_system_user_forwards_repetition_penalty(self):
        seen: dict = {}

        def handler(req):
            seen.update(json.loads(req.content.decode("utf-8")))
            return _ok_response("ok")

        c = _client_with(handler)
        c.system_user("sys", "usr", repetition_penalty=1.20)
        assert seen["repetition_penalty"] == pytest.approx(1.20)


# ============================================================ Loop 239
# README drift tests for the env knobs added in loops 236-238.
class TestReadmeKnobsLoop239:
    """Loop 239: ensure the env knobs introduced/changed in loops
    236-238 stay discoverable in the README. If a future commit
    silently drops them the docs go stale and operators won't know
    they can crank repetition_penalty when the model loops."""

    @staticmethod
    def _readme() -> str:
        from pathlib import Path

        return (Path(__file__).resolve().parents[1] / "README.md").read_text()

    def test_readme_documents_qwen_max_tokens(self):
        text = self._readme()
        assert "`QWEN_MAX_TOKENS`" in text
        # Must reflect the loop-236 default, not the stale 4096/8192.
        assert "16384" in text

    def test_readme_documents_repetition_penalty(self):
        text = self._readme()
        assert "`QWEN_REPETITION_PENALTY`" in text
        # Default value must be discoverable.
        assert "1.05" in text

    def test_readme_documents_truncation_marker_behavior(self):
        text = self._readme()
        # The exact marker string the client appends.
        assert "[truncated: model hit max_tokens]" in text

    def test_readme_documents_disable_think_strip(self):
        text = self._readme()
        assert "`QWEN_DISABLE_THINK_STRIP`" in text

    # Loop 240 readme drift coverage (kept in this class so the readme
    # docs-pass tests stay co-located).
    def test_readme_documents_auto_compress_knob(self):
        text = self._readme()
        assert "`QWEN_AUTO_COMPRESS`" in text

    def test_readme_documents_context_reserve_knob(self):
        text = self._readme()
        assert "`QWEN_CONTEXT_RESERVE`" in text
        assert "256" in text  # default value

    def test_readme_documents_chars_per_token_knob(self):
        text = self._readme()
        assert "`QWEN_CHARS_PER_TOKEN`" in text


# ============================================================ Loop 240
# Context compression: drop oldest non-protected messages so prompt +
# completion fits inside the server's context cap.
class TestContextCompressionLoop240:
    """Loop 240: user reported "context compression still don't seem to
    be there" after vLLM rejected requests with::

        This model's maximum context length is 65536 tokens. However,
        you requested 16384 output tokens and your prompt contains at
        least 49153 input tokens, for a total of at least 65537 tokens.

    Two root causes:
      1. Token estimator was 4 chars/token; reality is ~3 for code, so
         we under-counted by ~25% and the client clamp let the request
         through.
      2. There was no message-history compression at all -- only the
         max_tokens cap was clamped. With a long agent history the
         prompt itself could exceed the server cap.

    Fix: tighter estimator (3 chars/token by default), drop oldest
    non-system / non-last-user messages until the request fits, then
    final-clamp max_tokens to whatever room is left."""

    @staticmethod
    def _settings_with_cap(cap: int = 1000, max_tokens: int = 100):
        return Settings(
            base_url="http://test/v1",
            api_key="EMPTY",
            model="qwen3.6-27b",
            timeout=5,
            max_tokens=max_tokens,
            server_max_len=cap,
            loop_interval_seconds=1,
            loop_max_file_bytes=1000,
            loop_push=False,
        )

    @staticmethod
    def _client(handler, settings) -> QwenClient:
        c = QwenClient(settings=settings)
        c._client = httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url=settings.base_url,
            timeout=settings.timeout,
        )
        return c

    # ---------------------------------------------------- Estimator
    def test_estimator_uses_three_chars_per_token_by_default(self):
        from qwen_coder_mcp.qwen_client import _estimate_tokens

        # 300 chars / 3 chars-per-token = 100 tokens.
        assert _estimate_tokens("x" * 300) == 100

    def test_estimator_rounds_up_for_partial_token(self):
        from qwen_coder_mcp.qwen_client import _estimate_tokens

        # 301 chars at 3 cpt is 100.33 -> ceil to 101.
        assert _estimate_tokens("x" * 301) == 101

    def test_estimator_empty_is_zero(self):
        from qwen_coder_mcp.qwen_client import _estimate_tokens

        assert _estimate_tokens("") == 0
        assert _estimate_tokens(None) == 0  # type: ignore[arg-type]

    def test_estimator_env_override(self, monkeypatch):
        from qwen_coder_mcp.qwen_client import _estimate_tokens

        monkeypatch.setenv("QWEN_CHARS_PER_TOKEN", "4")
        # 400 chars / 4 cpt = 100.
        assert _estimate_tokens("x" * 400) == 100

    def test_estimator_env_invalid_falls_back(self, monkeypatch):
        from qwen_coder_mcp.qwen_client import _chars_per_token

        monkeypatch.setenv("QWEN_CHARS_PER_TOKEN", "not-a-number")
        assert _chars_per_token() == pytest.approx(3.0)
        monkeypatch.setenv("QWEN_CHARS_PER_TOKEN", "0")
        assert _chars_per_token() == pytest.approx(3.0)
        monkeypatch.setenv("QWEN_CHARS_PER_TOKEN", "-1")
        assert _chars_per_token() == pytest.approx(3.0)

    # ---------------------------------------------------- Reserve knob
    def test_context_reserve_default_256(self):
        from qwen_coder_mcp.qwen_client import _context_reserve_tokens

        assert _context_reserve_tokens() == 256

    def test_context_reserve_env_override(self, monkeypatch):
        from qwen_coder_mcp.qwen_client import _context_reserve_tokens

        monkeypatch.setenv("QWEN_CONTEXT_RESERVE", "512")
        assert _context_reserve_tokens() == 512

    def test_context_reserve_invalid_or_negative_falls_back(self, monkeypatch):
        from qwen_coder_mcp.qwen_client import _context_reserve_tokens

        monkeypatch.setenv("QWEN_CONTEXT_RESERVE", "junk")
        assert _context_reserve_tokens() == 256
        monkeypatch.setenv("QWEN_CONTEXT_RESERVE", "-5")
        assert _context_reserve_tokens() == 256

    # ---------------------------------------------------- Compression
    def test_no_compression_when_under_budget(self):
        c = self._client(lambda r: _ok_response("ok"), self._settings_with_cap(cap=10000))
        msgs = [
            ChatMessage("system", "you are helpful"),
            ChatMessage("user", "hi"),
            ChatMessage("assistant", "hello"),
            ChatMessage("user", "bye"),
        ]
        out, mt = c._compress_messages_to_fit(msgs, requested_max_tokens=100)
        assert len(out) == 4  # nothing dropped
        assert mt == 100

    def test_drops_oldest_non_protected_when_overflow(self, monkeypatch):
        monkeypatch.setenv("QWEN_CONTEXT_RESERVE", "0")
        # cap=200 tokens, target completion=50, reserve=0.
        # Each message content "x"*150 -> 50 tokens at 3 cpt.
        c = self._client(
            lambda r: _ok_response("ok"),
            self._settings_with_cap(cap=200, max_tokens=50),
        )
        msgs = [
            ChatMessage("system", "x" * 150),       # 50 tok protected
            ChatMessage("user", "x" * 150),         # 50 tok droppable
            ChatMessage("assistant", "x" * 150),    # 50 tok droppable
            ChatMessage("user", "current query"),   # ~5 tok protected (last user)
        ]
        # Prompt total ~155 tok; +50 completion = 205 > 200 cap.
        # Drop one droppable to get under.
        out, _mt = c._compress_messages_to_fit(msgs, requested_max_tokens=50)
        roles = [m.role if isinstance(m, ChatMessage) else m["role"] for m in out]
        contents = [
            m.content if isinstance(m, ChatMessage) else m["content"] for m in out
        ]
        # System and last user must survive.
        assert "system" in roles
        assert roles[-1] == "user"
        assert contents[-1] == "current query"
        # Some droppable must have been removed.
        assert len(out) < len(msgs)

    def test_preserves_all_system_messages(self, monkeypatch):
        monkeypatch.setenv("QWEN_CONTEXT_RESERVE", "0")
        c = self._client(
            lambda r: _ok_response("ok"),
            self._settings_with_cap(cap=300, max_tokens=50),
        )
        # Two system messages (rare but valid).
        msgs = [
            ChatMessage("system", "x" * 90),  # 30 tok, protected
            ChatMessage("user", "x" * 300),   # 100 tok, droppable
            ChatMessage("assistant", "x" * 300),  # 100 tok, droppable
            ChatMessage("system", "x" * 90),  # 30 tok, protected
            ChatMessage("user", "now"),       # ~1 tok, protected (last user)
        ]
        out, _mt = c._compress_messages_to_fit(msgs, requested_max_tokens=50)
        roles = [m.role if isinstance(m, ChatMessage) else m["role"] for m in out]
        # Both system messages must survive.
        assert roles.count("system") == 2

    def test_preserves_last_user_even_if_oldest_was_user(self, monkeypatch):
        monkeypatch.setenv("QWEN_CONTEXT_RESERVE", "0")
        c = self._client(
            lambda r: _ok_response("ok"),
            self._settings_with_cap(cap=200, max_tokens=20),
        )
        msgs = [
            ChatMessage("user", "x" * 300),   # 100 tok, droppable (NOT last user)
            ChatMessage("assistant", "x" * 300),
            ChatMessage("user", "x" * 300),   # 100 tok, droppable
            ChatMessage("assistant", "x" * 300),
            ChatMessage("user", "tail"),      # ~1 tok, protected
        ]
        out, _ = c._compress_messages_to_fit(msgs, requested_max_tokens=20)
        # Last user must survive even with no system at all.
        last = out[-1]
        last_role = last.role if isinstance(last, ChatMessage) else last["role"]
        last_content = last.content if isinstance(last, ChatMessage) else last["content"]
        assert last_role == "user"
        assert last_content == "tail"

    def test_clamps_max_tokens_when_protected_msgs_alone_overflow(self, monkeypatch):
        monkeypatch.setenv("QWEN_CONTEXT_RESERVE", "0")
        c = self._client(
            lambda r: _ok_response("ok"),
            self._settings_with_cap(cap=100, max_tokens=80),
        )
        # System + last user already 90 tokens: target=80 -> can't fit;
        # drop nothing (both protected); clamp max_tokens to room.
        msgs = [
            ChatMessage("system", "x" * 240),  # 80 tok
            ChatMessage("user", "x" * 30),     # 10 tok
        ]
        out, mt = c._compress_messages_to_fit(msgs, requested_max_tokens=80)
        assert len(out) == 2
        # Room = 100 - 90 - 0 = 10. So max_tokens clamped to 10.
        assert mt == 10

    def test_returns_one_when_protected_msgs_alone_exceed_cap(self, monkeypatch):
        monkeypatch.setenv("QWEN_CONTEXT_RESERVE", "0")
        c = self._client(
            lambda r: _ok_response("ok"),
            self._settings_with_cap(cap=50, max_tokens=80),
        )
        # System alone is already bigger than cap.
        msgs = [
            ChatMessage("system", "x" * 300),  # 100 tok > cap
            ChatMessage("user", "hi"),
        ]
        out, mt = c._compress_messages_to_fit(msgs, requested_max_tokens=80)
        assert mt == 1  # degenerate, server may still 400 but at least we tried.

    def test_disabled_via_env(self, monkeypatch):
        monkeypatch.setenv("QWEN_AUTO_COMPRESS", "0")
        c = self._client(
            lambda r: _ok_response("ok"),
            self._settings_with_cap(cap=200, max_tokens=50),
        )
        msgs = [
            ChatMessage("system", "x" * 150),
            ChatMessage("user", "x" * 150),
            ChatMessage("assistant", "x" * 150),
            ChatMessage("user", "now"),
        ]
        out, _mt = c._compress_messages_to_fit(msgs, requested_max_tokens=50)
        # Compression disabled -> no messages dropped.
        assert len(out) == len(msgs)

    def test_caller_messages_list_not_mutated(self, monkeypatch):
        monkeypatch.setenv("QWEN_CONTEXT_RESERVE", "0")
        c = self._client(
            lambda r: _ok_response("ok"),
            self._settings_with_cap(cap=200, max_tokens=20),
        )
        msgs = [
            ChatMessage("user", "x" * 300),
            ChatMessage("assistant", "x" * 300),
            ChatMessage("user", "tail"),
        ]
        original_len = len(msgs)
        c._compress_messages_to_fit(msgs, requested_max_tokens=20)
        # Caller's list must be untouched (we work on a copy).
        assert len(msgs) == original_len

    # ---------------------------------------------------- Wired into chat
    def test_chat_sends_compressed_messages_to_server(self, monkeypatch):
        monkeypatch.setenv("QWEN_CONTEXT_RESERVE", "0")
        seen: dict = {}

        def handler(req):
            seen.update(json.loads(req.content.decode("utf-8")))
            return _ok_response("ok")

        c = self._client(handler, self._settings_with_cap(cap=200, max_tokens=20))
        msgs = [
            ChatMessage("system", "x" * 90),       # 30 tok, protected
            ChatMessage("user", "x" * 300),        # 100 tok, droppable (1st)
            ChatMessage("assistant", "x" * 300),   # 100 tok, droppable (2nd)
            ChatMessage("user", "tail"),           # ~1 tok, protected
        ]
        c.chat(msgs, max_tokens=20)
        sent_roles = [m["role"] for m in seen["messages"]]
        # Some droppable was removed from the wire payload.
        assert len(seen["messages"]) < len(msgs)
        assert "system" in sent_roles
        assert sent_roles[-1] == "user"

    def test_chat_stream_also_compresses(self, monkeypatch):
        monkeypatch.setenv("QWEN_CONTEXT_RESERVE", "0")
        seen: dict = {}

        def handler(req):
            seen.update(json.loads(req.content.decode("utf-8")))
            return httpx.Response(
                200,
                text='data: {"choices":[{"delta":{"content":"x"},"finish_reason":"stop"}]}\ndata: [DONE]\n',
                headers={"content-type": "text/event-stream"},
            )

        c = self._client(handler, self._settings_with_cap(cap=200, max_tokens=20))
        msgs = [
            ChatMessage("system", "x" * 90),
            ChatMessage("user", "x" * 300),
            ChatMessage("assistant", "x" * 300),
            ChatMessage("user", "tail"),
        ]
        list(c.chat_stream(msgs, max_tokens=20))
        assert len(seen["messages"]) < len(msgs)

    def test_realistic_overflow_repro(self, monkeypatch):
        """Reproduce the user's bug report: 49k-token prompt + 16k
        max_tokens vs 65k cap. With compression the request must fit;
        without it (the pre-loop-240 codepath) the server would 400."""
        monkeypatch.setenv("QWEN_CONTEXT_RESERVE", "256")
        seen: dict = {}

        def handler(req):
            seen.update(json.loads(req.content.decode("utf-8")))
            return _ok_response("ok")

        c = self._client(handler, self._settings_with_cap(cap=65536, max_tokens=16384))
        # 49k tokens at 3 cpt -> 147k chars of history
        big_assistant = "x" * 147000
        msgs = [
            ChatMessage("system", "you are helpful"),
            ChatMessage("user", "earlier question"),
            ChatMessage("assistant", big_assistant),  # 49k tok
            ChatMessage("user", "now answer this"),
        ]
        c.chat(msgs, max_tokens=16384)
        # Verify what actually went on the wire fits.
        from qwen_coder_mcp.qwen_client import _estimate_tokens

        sent_prompt_tokens = sum(
            _estimate_tokens(m["content"]) for m in seen["messages"]
        )
        sent_max = seen["max_tokens"]
        # 65536 - sent_prompt - sent_max must be >= reserve(256)
        # to satisfy the server's cap.
        assert sent_prompt_tokens + sent_max + 256 <= 65536
