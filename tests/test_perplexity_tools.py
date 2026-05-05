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
                        {"title": "No url", "url": "", "snippet": "skip"},
                        {"title": "Solo", "url": "https://s.example/"},
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
        assert len(results) == 2  # bad row dropped
        assert results[0].title == "Hello"
        assert results[0].url == "https://h.example/"
        assert results[0].snippet == "World"
        assert results[0].date == "2026-01-01"
        assert results[1].snippet == ""

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
