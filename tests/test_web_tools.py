"""Loop 128: tests for src/qwen_coder_mcp/web_tools.py.

Uses httpx.MockTransport so no real network calls.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from qwen_coder_mcp import web_tools as W


# ----------------------------------------------------- parse_search_results
class TestParseSearchResults:
    def test_extracts_title_url_snippet(self):
        sample = (
            '<div><a class="result__a" href="https://example.com/a">Title A</a>'
            '<a class="result__snippet">Snippet A</a></div>'
            '<div><a class="result__a" href="https://example.com/b">Title B</a>'
            '<div class="result__snippet">Snippet B</div></div>'
        )
        out = W.parse_search_results(sample, max_results=10)
        assert len(out) == 2
        assert out[0].title == "Title A"
        assert out[0].url == "https://example.com/a"
        assert out[0].snippet == "Snippet A"
        assert out[1].snippet == "Snippet B"

    def test_unwraps_ddg_redirect(self):
        sample = (
            '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Freal.example.com%2Fpage&rut=x">'
            'Real</a><a class="result__snippet">snip</a>'
        )
        out = W.parse_search_results(sample, max_results=5)
        assert out[0].url == "https://real.example.com/page"

    def test_respects_max_results_cap(self):
        sample = "".join(
            f'<a class="result__a" href="https://example.com/{i}">T{i}</a>'
            f'<a class="result__snippet">S{i}</a>'
            for i in range(20)
        )
        assert len(W.parse_search_results(sample, max_results=3)) == 3

    def test_html_entities_decoded(self):
        sample = (
            '<a class="result__a" href="https://example.com/x">Foo &amp; Bar</a>'
            '<a class="result__snippet">A &lt;tag&gt; here</a>'
        )
        out = W.parse_search_results(sample, max_results=5)
        assert out[0].title == "Foo & Bar"
        assert out[0].snippet == "A <tag> here"

    def test_skips_results_without_url_or_title(self):
        sample = '<a class="result__a" href="">Empty</a><a class="result__snippet">x</a>'
        assert W.parse_search_results(sample, max_results=5) == []


# ----------------------------------------------------- web_search
class TestWebSearch:
    def test_empty_query_raises(self):
        with pytest.raises(ValueError):
            W.web_search("   ")

    def test_zero_max_results_raises(self):
        with pytest.raises(ValueError):
            W.web_search("python", max_results=0)

    def test_uses_injected_client(self):
        sample = (
            '<a class="result__a" href="https://example.com/x">X</a>'
            '<a class="result__snippet">snip</a>'
        )
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["url"] = str(request.url)
            captured["body"] = request.content.decode("utf-8")
            return httpx.Response(200, text=sample)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        try:
            out = W.web_search("python", max_results=5, client=client)
        finally:
            client.close()
        assert captured["method"] == "POST"
        assert "html.duckduckgo.com" in captured["url"]
        assert "q=python" in captured["body"]
        assert len(out) == 1
        assert out[0].url == "https://example.com/x"

    def test_propagates_http_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="boom")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(httpx.HTTPStatusError):
                W.web_search("anything", client=client)
        finally:
            client.close()


class TestDdgAnomalyFallbackLoop235:
    """Loop 235: DDG returns a 202 anomaly/botnet challenge page when
    it fingerprints us as a scraper. Previously we silently returned []
    because our regex matches nothing in the challenge HTML. Now we
    detect the challenge markers and fall back to DDG's Instant Answer
    JSON API which doesn't bot-block."""

    _ANOMALY_HTML = (
        '<html><body><form action="//duckduckgo.com/anomaly.js?'
        'sv=html&cc=botnet&ti=1234"></form></body></html>'
    )
    _IA_JSON = {
        "Heading": "Python (programming language)",
        "AbstractText": "Python is a high-level programming language.",
        "AbstractURL": "https://en.wikipedia.org/wiki/Python_(programming_language)",
        "RelatedTopics": [
            {
                "Text": "NumPy - NumPy is a library for Python.",
                "FirstURL": "https://duckduckgo.com/NumPy",
            },
            {
                "Text": "Django - A web framework.",
                "FirstURL": "https://duckduckgo.com/Django",
            },
        ],
    }

    def test_anomaly_marker_triggers_ia_fallback(self):
        urls_hit: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            urls_hit.append(str(request.url))
            if "html.duckduckgo.com" in str(request.url):
                return httpx.Response(202, text=self._ANOMALY_HTML)
            if "api.duckduckgo.com" in str(request.url):
                return httpx.Response(200, json=self._IA_JSON)
            return httpx.Response(404, text="nope")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        try:
            out = W.web_search("python", max_results=5, client=client)
        finally:
            client.close()
        assert any("html.duckduckgo.com" in u for u in urls_hit)
        assert any("api.duckduckgo.com" in u for u in urls_hit)
        assert len(out) == 3
        assert out[0].url.startswith("https://en.wikipedia.org")
        assert out[1].title == "NumPy"
        assert "library for Python" in out[1].snippet

    def test_empty_parse_also_falls_back(self):
        """Even on a 200 with no anomaly markers, an empty result list
        should fall back to IA so the caller isn't blind to a future
        DDG markup change."""
        def handler(request: httpx.Request) -> httpx.Response:
            if "html.duckduckgo.com" in str(request.url):
                return httpx.Response(200, text="<html>no results here</html>")
            if "api.duckduckgo.com" in str(request.url):
                return httpx.Response(200, json=self._IA_JSON)
            return httpx.Response(404)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        try:
            out = W.web_search("python", max_results=5, client=client)
        finally:
            client.close()
        assert len(out) == 3
        assert out[0].title == "Python (programming language)"

    def test_is_ddg_anomaly_detects_botnet(self):
        assert W._is_ddg_anomaly(self._ANOMALY_HTML, 202) is True
        assert W._is_ddg_anomaly(self._ANOMALY_HTML, 200) is True

    def test_is_ddg_anomaly_lowercases_check(self):
        upper = (
            '<form action="//duckduckgo.com/ANOMALY.JS?'
            'sv=html&CC=BOTNET"></form>'
        )
        assert W._is_ddg_anomaly(upper, 202) is True

    def test_is_ddg_anomaly_negative_for_normal_page(self):
        sample = (
            '<a class="result__a" href="https://example.com/x">X</a>'
            '<a class="result__snippet">snip</a>'
        )
        assert W._is_ddg_anomaly(sample, 200) is False

    def test_is_ddg_anomaly_handles_empty(self):
        assert W._is_ddg_anomaly("", 200) is False
        assert W._is_ddg_anomaly(None, 200) is False  # type: ignore[arg-type]

    def test_ia_fallback_respects_max_results(self):
        big = {
            "Heading": "Q",
            "AbstractText": "abs",
            "AbstractURL": "https://example.com/abs",
            "RelatedTopics": [
                {
                    "Text": f"T{i} - desc{i}",
                    "FirstURL": f"https://example.com/t{i}",
                }
                for i in range(20)
            ],
        }

        def handler(request: httpx.Request) -> httpx.Response:
            if "html.duckduckgo.com" in str(request.url):
                return httpx.Response(202, text=self._ANOMALY_HTML)
            return httpx.Response(200, json=big)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        try:
            out = W.web_search("q", max_results=3, client=client)
        finally:
            client.close()
        assert len(out) == 3

    def test_ia_fallback_walks_nested_topic_groups(self):
        """DDG IA sometimes nests topics under a 'Topics' key (groups
        like "See also"). The walker must recurse into them."""
        nested = {
            "Heading": "Q",
            "AbstractText": "",
            "AbstractURL": "",
            "RelatedTopics": [
                {
                    "Name": "See also",
                    "Topics": [
                        {
                            "Text": "Nested - inner topic",
                            "FirstURL": "https://example.com/nested",
                        },
                    ],
                },
                {
                    "Text": "Top - top topic",
                    "FirstURL": "https://example.com/top",
                },
            ],
        }

        def handler(request: httpx.Request) -> httpx.Response:
            if "html.duckduckgo.com" in str(request.url):
                return httpx.Response(202, text=self._ANOMALY_HTML)
            return httpx.Response(200, json=nested)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        try:
            out = W.web_search("q", max_results=5, client=client)
        finally:
            client.close()
        urls = [r.url for r in out]
        assert "https://example.com/nested" in urls
        assert "https://example.com/top" in urls

    def test_ia_fallback_skips_topics_missing_url_or_text(self):
        partial = {
            "Heading": "Q",
            "AbstractText": "",
            "AbstractURL": "",
            "RelatedTopics": [
                {"Text": "no url here", "FirstURL": ""},
                {"Text": "", "FirstURL": "https://example.com/no-text"},
                {"Text": "Good - real one", "FirstURL": "https://example.com/good"},
            ],
        }

        def handler(request: httpx.Request) -> httpx.Response:
            if "html.duckduckgo.com" in str(request.url):
                return httpx.Response(202, text=self._ANOMALY_HTML)
            return httpx.Response(200, json=partial)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        try:
            out = W.web_search("q", max_results=5, client=client)
        finally:
            client.close()
        assert len(out) == 1
        assert out[0].url == "https://example.com/good"


# ----------------------------------------------------- fetch_url
class TestFetchUrl:
    def test_empty_url_raises(self):
        with pytest.raises(ValueError):
            W.fetch_url("")

    def test_non_http_scheme_raises(self):
        with pytest.raises(ValueError):
            W.fetch_url("file:///etc/passwd")
        with pytest.raises(ValueError):
            W.fetch_url("ftp://example.com/")

    def test_zero_max_bytes_raises(self):
        with pytest.raises(ValueError):
            W.fetch_url("https://example.com/", max_bytes=0)

    def test_text_content_returned(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text="hello world",
                headers={"content-type": "text/plain; charset=utf-8"},
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        try:
            res = W.fetch_url("https://example.com/", client=client)
        finally:
            client.close()
        assert res["status"] == 200
        assert res["text"] == "hello world"
        assert res["truncated"] is False
        assert res.get("error") is None

    def test_truncates_to_max_bytes(self):
        big = "x" * 5000

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, text=big, headers={"content-type": "text/plain"}
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        try:
            res = W.fetch_url(
                "https://example.com/", max_bytes=1000, client=client
            )
        finally:
            client.close()
        assert len(res["text"]) == 1000
        assert res["truncated"] is True

    def test_refuses_non_text_content(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=b"\x89PNG\r\n", headers={"content-type": "image/png"}
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        try:
            res = W.fetch_url("https://example.com/", client=client)
        finally:
            client.close()
        assert res["error"] == "non_text_content"
        assert res["text"] == ""

    def test_accepts_json_content_type(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, text='{"ok": true}', headers={"content-type": "application/json"}
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        try:
            res = W.fetch_url("https://example.com/api", client=client)
        finally:
            client.close()
        assert res["text"] == '{"ok": true}'
        assert res.get("error") is None


# ----------------------------------------------------- format_search_results
class TestFormatSearchResults:
    def test_renders_numbered_block(self):
        rs = [
            W.SearchResult("First", "https://a.example.com/", "snippet A"),
            W.SearchResult("Second", "https://b.example.com/", ""),
        ]
        text = W.format_search_results(rs)
        assert "1. First" in text
        assert "https://a.example.com/" in text
        assert "snippet A" in text
        assert "2. Second" in text

    def test_empty_list_message(self):
        assert W.format_search_results([]) == "(no results)"


# ----------------------------------------------------- server wiring
class TestServerWiring:
    def test_web_search_tool_listed(self):
        from qwen_coder_mcp.server import _build_server
        import asyncio

        server, _ = _build_server()
        # The mcp Server stores list_tools handler internally; we exercise
        # the registered handler the same way the runtime would.
        handlers = server.request_handlers
        # Find the list_tools handler regardless of internal type identity.
        names: list[str] = []
        for tool in asyncio.run(_collect_tools(server)):
            names.append(tool.name)
        assert "web_search" in names
        assert "fetch_url" in names

    def test_dispatch_web_search_uses_web_tools(self, monkeypatch):
        from qwen_coder_mcp import server as S

        called = {}

        def fake(query, *, max_results=5):
            called["q"] = query
            called["n"] = max_results
            return [W.SearchResult("X", "https://x.example/", "s")]

        monkeypatch.setattr(W, "web_search", fake)
        monkeypatch.setattr(S.web_tools, "web_search", fake)
        out = S._dispatch(None, "web_search", {"query": "py", "max_results": 3})
        assert called == {"q": "py", "n": 3}
        assert "X" in out and "https://x.example/" in out

    def test_dispatch_fetch_url_uses_web_tools(self, monkeypatch):
        from qwen_coder_mcp import server as S

        def fake(url, *, max_bytes=200_000):
            return {
                "status": 200,
                "url": url,
                "content_type": "text/plain",
                "text": "hi",
                "truncated": False,
            }

        monkeypatch.setattr(S.web_tools, "fetch_url", fake)
        out = S._dispatch(None, "fetch_url", {"url": "https://example.com/"})
        assert "https://example.com/" in out
        assert out.endswith("hi")


async def _collect_tools(server):
    # Tap into the mcp Server's registered list_tools handler.
    for handler in server.request_handlers.values():
        try:
            res = await handler(None)  # type: ignore[arg-type]
        except Exception:
            continue
        tools = getattr(res, "root", None)
        if tools is None:
            continue
        tools = getattr(tools, "tools", None)
        if tools:
            return tools
    return []
