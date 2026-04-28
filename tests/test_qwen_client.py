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
