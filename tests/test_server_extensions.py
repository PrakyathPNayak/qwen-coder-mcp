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
    ):
        assert n in names


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
