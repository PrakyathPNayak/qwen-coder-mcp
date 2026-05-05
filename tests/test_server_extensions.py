"""Tests for the new server dispatch arms (perplexity_* + patch_anchor).

The perplexity tools are exercised by monkeypatching the underlying
``perplexity_tools`` module so no network is touched. The patch_anchor
tool is exercised against a real tmp_path-rooted FsConfig.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from qwen_coder_mcp import fs_tools, perplexity_tools, server as srv


class _StubClient:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    def system_user(self, *_a: Any, **_kw: Any) -> str:
        return "[stub]"

    def close(self) -> None:
        pass


def test_new_tools_are_registered() -> None:
    names = {t.name for t in srv._list_tools()}
    for n in (
        "patch_anchor",
        "perplexity_search",
        "perplexity_ask",
        "perplexity_research",
        "perplexity_reason",
        "perplexity_embed",
        "perplexity_async_create",
        "perplexity_async_get",
        "perplexity_async_list",
    ):
        assert n in names


def test_perplexity_chat_input_schema_exposes_full_surface() -> None:
    """The shared chat input schema must surface every option the
    perplexity-py SDK documents -- the original revision exposed only
    three of them, which is what motivated this re-port."""
    schema = srv._perplexity_chat_input_schema(
        {"type": "string"}, with_strip_thinking=True
    )
    props = schema["properties"]
    for key in (
        "messages",
        "search_recency_filter",
        "search_domain_filter",
        "search_language_filter",
        "search_mode",
        "search_after_date_filter",
        "search_before_date_filter",
        "last_updated_after_filter",
        "last_updated_before_filter",
        "disable_search",
        "return_related_questions",
        "return_images",
        "search_context_size",
        "search_type",
        "user_location",
        "image_results_enhanced_relevance",
        "reasoning_effort",
        "temperature",
        "top_p",
        "top_k",
        "max_tokens",
        "frequency_penalty",
        "presence_penalty",
        "stop",
        "country",
        "response_format",
        "strip_thinking",
    ):
        assert key in props, f"missing chat option in schema: {key}"


def test_perplexity_chat_input_schema_async_extras() -> None:
    schema = srv._perplexity_chat_input_schema(
        {"type": "string"}, with_async_extras=True
    )
    assert "model" in schema["properties"]
    assert "idempotency_key" in schema["properties"]
    assert "model" in schema["required"]


def test_dispatch_perplexity_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_embed(input, **kw):
        captured["input"] = input
        captured.update(kw)
        return perplexity_tools.PerplexityEmbeddingsResult(
            data=[{"embedding": [0.1, 0.2], "index": 0}],
            model="pplx-embed-v1-0.6b",
        )

    monkeypatch.setattr(perplexity_tools, "perplexity_embed", fake_embed)
    out = srv._dispatch(
        _StubClient(),
        "perplexity_embed",
        {
            "input": "hello",
            "model": "pplx-embed-v1-0.6b",
            "dimensions": 256,
            "encoding_format": "base64_int8",
        },
        None,
    )
    assert captured["input"] == "hello"
    assert captured["model"] == "pplx-embed-v1-0.6b"
    assert captured["dimensions"] == 256
    assert captured["encoding_format"] == "base64_int8"
    assert "Generated 1 embedding" in out


def test_dispatch_perplexity_embed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_a, **_kw):
        raise perplexity_tools.PerplexityError("nope")

    monkeypatch.setattr(perplexity_tools, "perplexity_embed", boom)
    out = srv._dispatch(
        _StubClient(),
        "perplexity_embed",
        {"input": "x", "model": "pplx-embed-v1-0.6b"},
        None,
    )
    assert "perplexity_embed error" in out
    assert "nope" in out


def test_dispatch_perplexity_async_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def fake_create(messages, **kw):
        captured["messages"] = messages
        captured.update(kw)
        return {
            "id": "j-1",
            "model": kw["model"],
            "status": "CREATED",
            "created_at": 1,
        }

    monkeypatch.setattr(
        perplexity_tools, "perplexity_async_create", fake_create
    )
    out = srv._dispatch(
        _StubClient(),
        "perplexity_async_create",
        {
            "messages": [{"role": "user", "content": "Q"}],
            "model": "sonar-pro",
            "idempotency_key": "abc",
            "temperature": 0.4,
            "search_recency_filter": "week",
        },
        None,
    )
    assert captured["model"] == "sonar-pro"
    assert captured["idempotency_key"] == "abc"
    assert captured["temperature"] == 0.4
    assert captured["search_recency_filter"] == "week"
    assert "[CREATED]" in out
    assert "id=j-1" in out


def test_dispatch_perplexity_async_get(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def fake_get(rid):
        captured["rid"] = rid
        return {
            "id": rid,
            "model": "sonar-pro",
            "status": "COMPLETED",
            "created_at": 1,
        }

    monkeypatch.setattr(perplexity_tools, "perplexity_async_get", fake_get)
    out = srv._dispatch(
        _StubClient(),
        "perplexity_async_get",
        {"api_request_id": "j-77"},
        None,
    )
    assert captured["rid"] == "j-77"
    assert "[COMPLETED]" in out


def test_dispatch_perplexity_async_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        perplexity_tools,
        "perplexity_async_list",
        lambda: {"data": []},
    )
    out = srv._dispatch(
        _StubClient(), "perplexity_async_list", {}, None
    )
    assert "no async jobs" in out


def test_dispatch_chat_forwards_all_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dispatcher must forward every option the schema accepts
    onto the underlying perplexity_chat call. Previously several fields
    (temperature, top_p, search_mode, ...) were silently dropped."""
    captured: dict = {}

    def fake_ask(messages, **kw):
        captured["messages"] = messages
        captured.update(kw)
        return "ok"

    monkeypatch.setattr(perplexity_tools, "perplexity_ask", fake_ask)
    out = srv._dispatch(
        _StubClient(),
        "perplexity_ask",
        {
            "messages": [{"role": "user", "content": "Q"}],
            "search_recency_filter": "month",
            "search_mode": "academic",
            "search_domain_filter": ["a.com"],
            "search_context_size": "high",
            "search_type": "pro",
            "user_location": {"country": "US"},
            "temperature": 0.7,
            "top_p": 0.95,
            "max_tokens": 800,
            "country": "US",
            "disable_search": False,
            "return_related_questions": True,
            "response_format": {"type": "text"},
        },
        None,
    )
    assert out == "ok"
    assert captured["search_recency_filter"] == "month"
    assert captured["search_mode"] == "academic"
    assert captured["search_context_size"] == "high"
    assert captured["search_type"] == "pro"
    assert captured["user_location"] == {"country": "US"}
    assert captured["temperature"] == 0.7
    assert captured["max_tokens"] == 800
    assert captured["disable_search"] is False
    assert captured["return_related_questions"] is True
    assert captured["response_format"] == {"type": "text"}


def test_dispatch_search_forwards_all_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def fake_search(query, **kw):
        captured["query"] = query
        captured.update(kw)
        return []

    monkeypatch.setattr(perplexity_tools, "perplexity_search", fake_search)
    srv._dispatch(
        _StubClient(),
        "perplexity_search",
        {
            "query": "x",
            "max_results": 5,
            "max_tokens_per_page": 768,
            "max_tokens": 1500,
            "country": "GB",
            "search_mode": "sec",
            "search_recency_filter": "year",
            "search_domain_filter": ["a.com"],
            "search_language_filter": ["en"],
            "last_updated_after_filter": "2024-01-01",
            "search_after_date_filter": "2024-06-01",
        },
        None,
    )
    assert captured["query"] == "x"
    assert captured["max_results"] == 5
    assert captured["max_tokens_per_page"] == 768
    assert captured["max_tokens"] == 1500
    assert captured["country"] == "GB"
    assert captured["search_mode"] == "sec"
    assert captured["search_recency_filter"] == "year"
    assert captured["search_domain_filter"] == ["a.com"]
    assert captured["search_language_filter"] == ["en"]
    assert captured["last_updated_after_filter"] == "2024-01-01"
    assert captured["search_after_date_filter"] == "2024-06-01"


def test_dispatch_patch_anchor_happy_path(tmp_path: Path) -> None:
    cfg = fs_tools.FsConfig(root=tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    out = srv._dispatch(
        _StubClient(),
        "patch_anchor",
        {"path": "a.py", "old_str": "x = 1", "new_str": "x = 2"},
        cfg,
    )
    assert "patched a.py" in out
    assert (tmp_path / "a.py").read_text() == "x = 2\n"


def test_dispatch_patch_anchor_error_surface(tmp_path: Path) -> None:
    cfg = fs_tools.FsConfig(root=tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    out = srv._dispatch(
        _StubClient(),
        "patch_anchor",
        {"path": "a.py", "old_str": "missing", "new_str": "x"},
        cfg,
    )
    assert out.startswith("patch_anchor error:")


def test_dispatch_perplexity_search(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_search(query: str, **kw):
        captured["query"] = query
        captured.update(kw)
        return [
            perplexity_tools.PerplexitySearchResult(
                title="T", url="https://u/", snippet="S"
            )
        ]

    monkeypatch.setattr(perplexity_tools, "perplexity_search", fake_search)
    out = srv._dispatch(
        _StubClient(),
        "perplexity_search",
        {"query": "hello", "max_results": 3, "country": "US"},
        None,
    )
    assert captured["query"] == "hello"
    assert captured["max_results"] == 3
    assert captured["country"] == "US"
    assert "1. T" in out


def test_dispatch_perplexity_ask(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_ask(messages, **kw):
        captured["messages"] = messages
        captured.update(kw)
        return "answered"

    monkeypatch.setattr(perplexity_tools, "perplexity_ask", fake_ask)
    out = srv._dispatch(
        _StubClient(),
        "perplexity_ask",
        {
            "messages": [{"role": "user", "content": "Q"}],
            "search_recency_filter": "day",
        },
        None,
    )
    assert out == "answered"
    assert captured["messages"] == [{"role": "user", "content": "Q"}]
    assert captured["search_recency_filter"] == "day"


def test_dispatch_perplexity_research(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_research(messages, **kw):
        captured.update(kw)
        return "deep"

    monkeypatch.setattr(perplexity_tools, "perplexity_research", fake_research)
    out = srv._dispatch(
        _StubClient(),
        "perplexity_research",
        {
            "messages": [{"role": "user", "content": "Q"}],
            "strip_thinking": True,
            "reasoning_effort": "high",
        },
        None,
    )
    assert out == "deep"
    assert captured["strip_thinking"] is True
    assert captured["reasoning_effort"] == "high"


def test_dispatch_perplexity_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_reason(messages, **kw):
        captured.update(kw)
        return "step by step"

    monkeypatch.setattr(perplexity_tools, "perplexity_reason", fake_reason)
    out = srv._dispatch(
        _StubClient(),
        "perplexity_reason",
        {
            "messages": [{"role": "user", "content": "Q"}],
            "search_context_size": "low",
        },
        None,
    )
    assert out == "step by step"
    assert captured["search_context_size"] == "low"


def test_dispatch_perplexity_error_surface(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a, **_kw):
        raise perplexity_tools.PerplexityError("API down")

    monkeypatch.setattr(perplexity_tools, "perplexity_ask", boom)
    out = srv._dispatch(
        _StubClient(),
        "perplexity_ask",
        {"messages": [{"role": "user", "content": "Q"}]},
        None,
    )
    assert out.startswith("perplexity_ask error:")
    assert "API down" in out
