"""Tests for src/qwen_coder_mcp/perplexity_tools.py.

Mirrors the test patterns used by tests/test_web_tools.py: an injected
``httpx.Client`` backed by ``httpx.MockTransport`` so no real network
calls are made. Environment-variable handling is exercised via
``monkeypatch``.
"""
from __future__ import annotations

import json

import httpx
import pytest

from qwen_coder_mcp import perplexity_tools as P


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Most calls need a key set. Tests that explicitly delete it have
    their own monkeypatch."""
    monkeypatch.setenv("PERPLEXITY_API_KEY", "test-key-xyz")


def _client(handler) -> httpx.Client:
    """Build an httpx.Client backed by MockTransport."""
    return httpx.Client(
        transport=httpx.MockTransport(handler),
        timeout=10.0,
        headers={"User-Agent": "test"},
    )


# ----------------------------------------------------------- env helpers
class TestEnvHelpers:
    def test_resolve_proxy_precedence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in ("PERPLEXITY_PROXY", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            monkeypatch.delenv(var, raising=False)
        assert P._resolve_proxy() is None
        monkeypatch.setenv("HTTP_PROXY", "http://x:1")
        assert P._resolve_proxy() == "http://x:1"
        monkeypatch.setenv("HTTPS_PROXY", "http://y:2")
        assert P._resolve_proxy() == "http://y:2"
        monkeypatch.setenv("PERPLEXITY_PROXY", "http://z:3")
        assert P._resolve_proxy() == "http://z:3"

    def test_resolve_timeout_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PERPLEXITY_TIMEOUT_MS", raising=False)
        assert P._resolve_timeout() == P.DEFAULT_TIMEOUT_SECONDS

    def test_resolve_timeout_parses_ms(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PERPLEXITY_TIMEOUT_MS", "60000")
        assert P._resolve_timeout() == 60.0

    def test_resolve_timeout_rejects_garbage_and_nonpositive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PERPLEXITY_TIMEOUT_MS", "abc")
        assert P._resolve_timeout() == P.DEFAULT_TIMEOUT_SECONDS
        monkeypatch.setenv("PERPLEXITY_TIMEOUT_MS", "-1")
        assert P._resolve_timeout() == P.DEFAULT_TIMEOUT_SECONDS

    def test_missing_api_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
        with pytest.raises(P.PerplexityError, match="PERPLEXITY_API_KEY"):
            P._resolve_api_key()

    def test_base_url_strips_trailing_slash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PERPLEXITY_BASE_URL", "https://x.example/")
        assert P._resolve_base_url() == "https://x.example"


# ------------------------------------------------------ validate_messages
class TestValidateMessages:
    def test_rejects_non_list(self) -> None:
        with pytest.raises(P.PerplexityError):
            P.validate_messages("not a list")

    def test_rejects_empty(self) -> None:
        with pytest.raises(P.PerplexityError):
            P.validate_messages([])

    def test_rejects_bad_role(self) -> None:
        with pytest.raises(P.PerplexityError):
            P.validate_messages([{"role": "bot", "content": "x"}])

    def test_rejects_non_string_content(self) -> None:
        with pytest.raises(P.PerplexityError):
            P.validate_messages([{"role": "user", "content": 42}])

    def test_normalises_to_dicts(self) -> None:
        out = P.validate_messages(
            [{"role": "user", "content": "hi", "extra": "ignored"}]
        )
        assert out == [{"role": "user", "content": "hi"}]


# ----------------------------------------------------------- /search
class TestPerplexitySearch:
    def test_empty_query_raises(self) -> None:
        with pytest.raises(P.PerplexityError):
            P.perplexity_search("   ")

    def test_happy_path(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["headers"] = dict(request.headers)
            captured["body"] = json.loads(request.content.decode())
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "Hello",
                            "url": "https://h.example/",
                            "snippet": "World",
                            "date": "2026-01-01",
                        },
                        {"title": "No url", "url": "", "snippet": "schema-valid empty url"},
                        {"title": "Solo", "url": "https://s.example/"},
                        # Following rows are intentionally malformed --
                        # they exercise the parser's skip behaviour:
                        # missing url field, non-dict, non-string title.
                        {"title": "Has no url field"},
                        "not-a-dict",
                        {"title": 123, "url": "https://nope.example/"},
                    ]
                },
            )

        c = _client(handler)
        try:
            results = P.perplexity_search("python", max_results=5, client=c)
        finally:
            c.close()
        assert captured["url"].endswith("/search")
        assert captured["headers"]["authorization"] == "Bearer test-key-xyz"
        assert captured["body"]["query"] == "python"
        assert captured["body"]["max_results"] == 5
        assert captured["body"]["max_tokens_per_page"] == 1024
        assert "country" not in captured["body"]
        # Schema-valid rows (incl. empty-url) are kept; non-dict rows
        # and rows with a non-string title/url are dropped.
        assert len(results) == 3
        assert results[0].title == "Hello"
        assert results[0].url == "https://h.example/"
        assert results[1].title == "No url"  # empty url is schema-valid
        assert results[1].url == ""
        assert results[2].title == "Solo"
        assert results[0].snippet == "World"
        assert results[0].date == "2026-01-01"
        assert results[1].snippet == "schema-valid empty url"
        assert results[2].snippet == ""

    def test_clamps_bounds(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content.decode())
            return httpx.Response(200, json={"results": []})

        c = _client(handler)
        try:
            P.perplexity_search(
                "q", max_results=99, max_tokens_per_page=10, country="US", client=c
            )
        finally:
            c.close()
        assert captured["body"]["max_results"] == 20  # clamped down
        assert captured["body"]["max_tokens_per_page"] == 256  # clamped up
        assert captured["body"]["country"] == "US"

    def test_api_error_surfaces_with_status_and_snippet(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom" * 500)

        c = _client(handler)
        try:
            with pytest.raises(P.PerplexityError, match="500"):
                P.perplexity_search("q", client=c)
        finally:
            c.close()

    def test_invalid_json_surfaces(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=b"not json",
                headers={"content-type": "application/json"},
            )

        c = _client(handler)
        try:
            with pytest.raises(P.PerplexityError, match="invalid JSON"):
                P.perplexity_search("q", client=c)
        finally:
            c.close()

    def test_format_search_results_empty(self) -> None:
        assert P.format_search_results([]) == "(no results)"

    def test_format_search_results_renders_all_fields(self) -> None:
        out = P.format_search_results(
            [
                P.PerplexitySearchResult(
                    title="T", url="https://u/", snippet="S", date="2026-01-01"
                )
            ]
        )
        assert "1. T" in out
        assert "https://u/" in out
        assert "S" in out
        assert "(2026-01-01)" in out


# --------------------------------------------------- /chat/completions
def _chat_response(content: str, citations: list[str] | None = None) -> dict:
    payload: dict = {
        "id": "x",
        "model": "sonar-pro",
        "created": 1,
        "choices": [
            {"message": {"content": content}, "finish_reason": "stop", "index": 0}
        ],
    }
    if citations:
        payload["citations"] = citations
    return payload


class TestPerplexityChat:
    def test_ask_happy_path(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content.decode())
            captured["url"] = str(request.url)
            return httpx.Response(
                200,
                json=_chat_response("Paris.", citations=["https://wp/Paris"]),
            )

        c = _client(handler)
        try:
            out = P.perplexity_ask(
                [{"role": "user", "content": "Capital of France?"}],
                search_recency_filter="week",
                search_domain_filter=["wikipedia.org"],
                search_context_size="high",
                client=c,
            )
        finally:
            c.close()
        assert captured["url"].endswith("/chat/completions")
        assert captured["body"]["model"] == P.ASK_MODEL
        assert captured["body"]["messages"] == [
            {"role": "user", "content": "Capital of France?"}
        ]
        assert captured["body"]["search_recency_filter"] == "week"
        assert captured["body"]["search_domain_filter"] == ["wikipedia.org"]
        assert captured["body"]["web_search_options"] == {
            "search_context_size": "high"
        }
        assert "stream" not in captured["body"]
        assert "Paris." in out
        assert "Citations:" in out
        assert "[1] https://wp/Paris" in out

    def test_ask_rejects_bad_filter(self) -> None:
        c = _client(lambda _r: httpx.Response(200, json=_chat_response("x")))
        try:
            with pytest.raises(P.PerplexityError, match="search_recency_filter"):
                P.perplexity_ask(
                    [{"role": "user", "content": "x"}],
                    search_recency_filter="forever",
                    client=c,
                )
        finally:
            c.close()

    def test_research_streams_sse(self) -> None:
        captured: dict = {}
        sse = (
            'data: {"id":"a","choices":[{"delta":{"content":"Hello "}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"world"}}],"citations":["https://c/1"]}\n\n'
            'data: [DONE]\n\n'
        )

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content.decode())
            return httpx.Response(
                200,
                content=sse.encode(),
                headers={"content-type": "text/event-stream"},
            )

        c = _client(handler)
        try:
            out = P.perplexity_research(
                [{"role": "user", "content": "Q"}],
                strip_thinking=False,
                reasoning_effort="high",
                client=c,
            )
        finally:
            c.close()
        assert captured["body"]["model"] == P.RESEARCH_MODEL
        assert captured["body"]["stream"] is True
        assert captured["body"]["reasoning_effort"] == "high"
        assert "Hello world" in out
        assert "[1] https://c/1" in out

    def test_reason_strip_thinking(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=_chat_response("<think>secret reasoning</think>The answer is 42."),
            )

        c = _client(handler)
        try:
            out = P.perplexity_reason(
                [{"role": "user", "content": "Q"}],
                strip_thinking=True,
                client=c,
            )
        finally:
            c.close()
        assert "secret reasoning" not in out
        assert "The answer is 42." in out

    def test_chat_missing_choices_raises(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"choices": []})

        c = _client(handler)
        try:
            with pytest.raises(P.PerplexityError, match="choices"):
                P.perplexity_ask(
                    [{"role": "user", "content": "Q"}], client=c
                )
        finally:
            c.close()

    def test_chat_missing_content_raises(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"choices": [{"message": {}, "finish_reason": "stop", "index": 0}]},
            )

        c = _client(handler)
        try:
            with pytest.raises(P.PerplexityError, match="content"):
                P.perplexity_ask(
                    [{"role": "user", "content": "Q"}], client=c
                )
        finally:
            c.close()

    def test_chat_no_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
        with pytest.raises(P.PerplexityError, match="PERPLEXITY_API_KEY"):
            P.perplexity_ask([{"role": "user", "content": "Q"}])


# ===================== embeddings =====================
class TestPerplexityEmbed:
    def test_happy_path_single_string(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content.decode())
            return httpx.Response(
                200,
                json={
                    "data": [{"embedding": [0.1, 0.2, 0.3], "index": 0}],
                    "model": "pplx-embed-v1-0.6b",
                    "usage": {"prompt_tokens": 4, "total_tokens": 4},
                },
            )

        c = _client(handler)
        try:
            res = P.perplexity_embed(
                "hello world", model="pplx-embed-v1-0.6b", client=c
            )
        finally:
            c.close()
        assert captured["url"].endswith("/v1/embeddings")
        assert captured["body"]["input"] == "hello world"
        assert captured["body"]["model"] == "pplx-embed-v1-0.6b"
        assert "dimensions" not in captured["body"]
        assert len(res.data) == 1
        assert res.data[0]["embedding"] == [0.1, 0.2, 0.3]
        assert res.model == "pplx-embed-v1-0.6b"
        assert res.usage and res.usage.get("total_tokens") == 4

    def test_batch_input_with_dim_and_encoding(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content.decode())
            return httpx.Response(200, json={"data": []})

        c = _client(handler)
        try:
            P.perplexity_embed(
                ["a", "b"],
                model="pplx-embed-v1-4b",
                dimensions=512,
                encoding_format="base64_int8",
                client=c,
            )
        finally:
            c.close()
        assert captured["body"]["input"] == ["a", "b"]
        assert captured["body"]["dimensions"] == 512
        assert captured["body"]["encoding_format"] == "base64_int8"

    def test_empty_string_rejected(self) -> None:
        with pytest.raises(P.PerplexityError, match="non-empty"):
            P.perplexity_embed("   ", model="pplx-embed-v1-0.6b")

    def test_empty_list_rejected(self) -> None:
        with pytest.raises(P.PerplexityError):
            P.perplexity_embed([], model="pplx-embed-v1-0.6b")

    def test_missing_model_rejected(self) -> None:
        with pytest.raises(P.PerplexityError, match="model"):
            P.perplexity_embed("x", model="")

    def test_oversize_batch_rejected(self) -> None:
        with pytest.raises(P.PerplexityError, match="512"):
            P.perplexity_embed(
                ["x"] * 513, model="pplx-embed-v1-0.6b"
            )

    def test_bad_encoding_rejected(self) -> None:
        with pytest.raises(P.PerplexityError, match="encoding_format"):
            P.perplexity_embed(
                "x", model="pplx-embed-v1-0.6b", encoding_format="bogus"
            )

    def test_negative_dimensions_rejected(self) -> None:
        with pytest.raises(P.PerplexityError, match="positive"):
            P.perplexity_embed(
                "x", model="pplx-embed-v1-0.6b", dimensions=0
            )

    def test_format_summary_floats(self) -> None:
        res = P.PerplexityEmbeddingsResult(
            data=[{"embedding": [0.1, 0.2, 0.3, 0.4, 0.5], "index": 0}],
            model="m",
            usage={"total_tokens": 5},
        )
        out = P.format_embeddings_result(res)
        assert "Generated 1 embedding" in out
        assert "dim=5" in out
        assert "0.1000" in out

    def test_format_summary_base64(self) -> None:
        res = P.PerplexityEmbeddingsResult(
            data=[{"embedding": "AAAAAAAA", "index": 0}],
            model="m",
            encoding_format="base64_int8",
        )
        out = P.format_embeddings_result(res)
        assert "base64_int8" in out

    def test_format_summary_empty(self) -> None:
        res = P.PerplexityEmbeddingsResult(data=[])
        assert P.format_embeddings_result(res) == "(no embeddings returned)"


# ===================== async chat =====================
class TestPerplexityAsync:
    def test_create_wraps_request(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content.decode())
            return httpx.Response(
                200,
                json={
                    "id": "j-1",
                    "model": "sonar-deep-research",
                    "status": "CREATED",
                    "created_at": 100,
                },
            )

        c = _client(handler)
        try:
            payload = P.perplexity_async_create(
                [{"role": "user", "content": "Q"}],
                model="sonar-deep-research",
                idempotency_key="abc",
                temperature=0.5,
                client=c,
            )
        finally:
            c.close()
        assert captured["url"].endswith("/async/chat/completions")
        # Crucial: request body must be wrapped under "request".
        assert "request" in captured["body"]
        assert captured["body"]["idempotency_key"] == "abc"
        inner = captured["body"]["request"]
        assert inner["model"] == "sonar-deep-research"
        assert inner["temperature"] == 0.5
        assert inner["messages"] == [{"role": "user", "content": "Q"}]
        assert payload["status"] == "CREATED"
        assert payload["id"] == "j-1"

    def test_get_uses_path_id(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["method"] = request.method
            return httpx.Response(
                200,
                json={
                    "id": "j-7",
                    "model": "sonar-pro",
                    "status": "COMPLETED",
                    "created_at": 100,
                    "completed_at": 200,
                    "response": {
                        "choices": [
                            {
                                "message": {
                                    "content": "hello",
                                },
                                "finish_reason": "stop",
                                "index": 0,
                            }
                        ],
                    },
                },
            )

        c = _client(handler)
        try:
            payload = P.perplexity_async_get("j-7", client=c)
        finally:
            c.close()
        assert captured["url"].endswith("/async/chat/completions/j-7")
        assert captured["method"] == "GET"
        assert payload["status"] == "COMPLETED"
        # format_async_record extracts the inline response content.
        formatted = P.format_async_record(payload)
        assert "[COMPLETED]" in formatted
        assert "hello" in formatted

    def test_get_requires_id(self) -> None:
        with pytest.raises(P.PerplexityError, match="api_request_id"):
            P.perplexity_async_get("")

    def test_list_returns_envelope(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "GET"
            assert str(request.url).endswith("/async/chat/completions")
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "j-1",
                            "model": "m",
                            "status": "IN_PROGRESS",
                            "created_at": 1,
                        },
                        {
                            "id": "j-2",
                            "model": "m",
                            "status": "FAILED",
                            "created_at": 2,
                            "error_message": "oops",
                        },
                    ]
                },
            )

        c = _client(handler)
        try:
            payload = P.perplexity_async_list(client=c)
        finally:
            c.close()
        formatted = P.format_async_list(payload)
        assert "[IN_PROGRESS]" in formatted
        assert "id=j-1" in formatted
        assert "id=j-2" in formatted
        assert "oops" in formatted

    def test_format_list_empty(self) -> None:
        assert P.format_async_list({"data": []}) == "(no async jobs)"
        assert P.format_async_list([]) == "(no async jobs)"
        assert P.format_async_list("garbage") == "(no async jobs)"


# ===================== chat-options forwarding =====================
class TestChatOptionsForwarding:
    def test_full_option_surface_lands_in_body(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content.decode())
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"content": "hi"},
                            "finish_reason": "stop",
                            "index": 0,
                        }
                    ]
                },
            )

        c = _client(handler)
        try:
            P.perplexity_ask(
                [{"role": "user", "content": "Q"}],
                temperature=0.4,
                top_p=0.95,
                top_k=20,
                max_tokens=600,
                frequency_penalty=0.1,
                presence_penalty=0.2,
                country="US",
                search_mode="academic",
                search_recency_filter="week",
                search_domain_filter=["a.com"],
                search_language_filter=["en"],
                disable_search=False,
                return_related_questions=True,
                return_images=False,
                last_updated_after_filter="2024-01-01",
                search_after_date_filter="2024-01-01",
                search_context_size="high",
                search_type="pro",
                user_location={"country": "US", "region": "CA"},
                stop=["END"],
                response_format={"type": "text"},
                client=c,
            )
        finally:
            c.close()
        b = captured["body"]
        assert b["model"] == "sonar-pro"
        assert b["temperature"] == 0.4
        assert b["top_p"] == 0.95
        assert b["top_k"] == 20
        assert b["max_tokens"] == 600
        assert b["frequency_penalty"] == 0.1
        assert b["country"] == "US"
        assert b["search_mode"] == "academic"
        assert b["search_recency_filter"] == "week"
        assert b["search_domain_filter"] == ["a.com"]
        assert b["search_language_filter"] == ["en"]
        assert b["disable_search"] is False
        assert b["return_related_questions"] is True
        assert b["return_images"] is False
        assert b["last_updated_after_filter"] == "2024-01-01"
        assert b["search_after_date_filter"] == "2024-01-01"
        assert b["stop"] == ["END"]
        assert b["response_format"] == {"type": "text"}
        # web_search_options sub-object is built only with set keys.
        wso = b["web_search_options"]
        assert wso["search_context_size"] == "high"
        assert wso["search_type"] == "pro"
        assert wso["user_location"] == {"country": "US", "region": "CA"}

    def test_invalid_enum_raises(self) -> None:
        with pytest.raises(P.PerplexityError, match="search_mode"):
            P.perplexity_ask(
                [{"role": "user", "content": "Q"}], search_mode="bogus"
            )
        with pytest.raises(P.PerplexityError, match="search_type"):
            P.perplexity_ask(
                [{"role": "user", "content": "Q"}], search_type="bogus"
            )
        with pytest.raises(P.PerplexityError, match="search_context_size"):
            P.perplexity_ask(
                [{"role": "user", "content": "Q"}],
                search_context_size="bogus",
            )

    def test_user_location_must_be_object(self) -> None:
        with pytest.raises(P.PerplexityError, match="user_location"):
            P.perplexity_ask(
                [{"role": "user", "content": "Q"}],
                user_location="not-an-object",  # type: ignore[arg-type]
            )

    def test_response_format_must_be_object(self) -> None:
        with pytest.raises(P.PerplexityError, match="response_format"):
            P.perplexity_ask(
                [{"role": "user", "content": "Q"}],
                response_format="not-an-object",  # type: ignore[arg-type]
            )


# ===================== search options =====================
class TestPerplexitySearchOptions:
    def test_full_search_option_surface(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content.decode())
            return httpx.Response(200, json={"results": []})

        c = _client(handler)
        try:
            P.perplexity_search(
                "x",
                max_results=3,
                max_tokens_per_page=512,
                max_tokens=1000,
                country="US",
                search_mode="sec",
                search_recency_filter="day",
                search_domain_filter=["a.com", "b.com"],
                search_language_filter=["en"],
                last_updated_after_filter="2024-01-01",
                last_updated_before_filter="2024-12-31",
                search_after_date_filter="2024-06-01",
                search_before_date_filter="2024-09-01",
                client=c,
            )
        finally:
            c.close()
        b = captured["body"]
        assert b["max_results"] == 3
        assert b["max_tokens_per_page"] == 512
        assert b["max_tokens"] == 1000
        assert b["country"] == "US"
        assert b["search_mode"] == "sec"
        assert b["search_recency_filter"] == "day"
        assert b["search_domain_filter"] == ["a.com", "b.com"]
        assert b["search_language_filter"] == ["en"]
        assert b["last_updated_after_filter"] == "2024-01-01"
        assert b["last_updated_before_filter"] == "2024-12-31"
        assert b["search_after_date_filter"] == "2024-06-01"
        assert b["search_before_date_filter"] == "2024-09-01"

    def test_invalid_search_mode_rejected(self) -> None:
        with pytest.raises(P.PerplexityError, match="search_mode"):
            P.perplexity_search("x", search_mode="bogus")

    def test_score_field_surfaced(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "T",
                            "url": "https://u/",
                            "snippet": "s",
                            "score": 0.987,
                        }
                    ]
                },
            )

        c = _client(handler)
        try:
            results = P.perplexity_search("x", client=c)
        finally:
            c.close()
        assert results[0].score == 0.987
        assert "score=0.987" in P.format_search_results(results)


# ===================== client lifecycle bug fix =====================
class TestClientLifecycle:
    def test_owned_client_closed_on_status_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: previously the stream branch leaked the owned
        client when the status was >=400. The single-try/finally close
        path now guarantees ``c.close()`` runs even when the request
        raises a PerplexityError due to a non-2xx status."""

        def transport_handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        tracked = httpx.Client(
            transport=httpx.MockTransport(transport_handler), timeout=5.0
        )
        original_close = tracked.close
        closed = {"n": 0}

        def counting_close() -> None:
            closed["n"] += 1
            original_close()

        tracked.close = counting_close  # type: ignore[method-assign]
        monkeypatch.setattr(P, "_build_client", lambda timeout=None: tracked)

        with pytest.raises(P.PerplexityError, match="500"):
            P.perplexity_ask([{"role": "user", "content": "Q"}])
        assert closed["n"] == 1, "owned client must be closed on status>=400"

    def test_owned_client_closed_on_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def transport_handler(_req: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow")

        tracked = httpx.Client(
            transport=httpx.MockTransport(transport_handler), timeout=5.0
        )
        original_close = tracked.close
        closed = {"n": 0}

        def counting_close() -> None:
            closed["n"] += 1
            original_close()

        tracked.close = counting_close  # type: ignore[method-assign]
        monkeypatch.setattr(P, "_build_client", lambda timeout=None: tracked)

        with pytest.raises(P.PerplexityError, match="timeout"):
            P.perplexity_ask([{"role": "user", "content": "Q"}])
        assert closed["n"] == 1, "owned client must be closed on timeout"
