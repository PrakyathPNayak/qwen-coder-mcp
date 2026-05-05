"""Tests for the new TUI helpers: /perplexity_*, /patch_anchor, /bug.

We cover the pure helpers (parse, redact, render) without booting the
Textual App. Network calls are stubbed via monkeypatch on the
``perplexity_tools`` module the TUI imported at module load.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from qwen_coder_mcp import fs_tools, perplexity_tools, tui
from qwen_coder_mcp.qwen_client import ChatMessage


# --------------------------------------------------------- patch_anchor parse
class TestPatchAnchorParse:
    def test_empty_returns_usage(self) -> None:
        assert "usage:" in tui._parse_patch_anchor_args("")

    def test_only_path_returns_usage(self) -> None:
        out = tui._parse_patch_anchor_args("foo.py")
        assert isinstance(out, str) and "usage:" in out

    def test_missing_old_block(self) -> None:
        out = tui._parse_patch_anchor_args("foo.py just text")
        assert isinstance(out, str) and "missing <<<old>>>" in out

    def test_unterminated_old_block(self) -> None:
        out = tui._parse_patch_anchor_args("foo.py <<<abc")
        assert isinstance(out, str) and "unterminated" in out

    def test_missing_new_block(self) -> None:
        out = tui._parse_patch_anchor_args("foo.py <<<a>>> tail")
        assert isinstance(out, str) and "missing <<<new>>>" in out

    def test_unterminated_new_block(self) -> None:
        out = tui._parse_patch_anchor_args("foo.py <<<a>>> <<<b")
        assert isinstance(out, str) and "unterminated" in out

    def test_happy_path(self) -> None:
        out = tui._parse_patch_anchor_args("foo.py <<<x = 1>>> <<<x = 2>>>")
        assert out == ("foo.py", "x = 1", "x = 2")

    def test_preserves_whitespace_inside_blocks(self) -> None:
        out = tui._parse_patch_anchor_args(
            "f.py <<< spaced  out >>> <<<\nnewline\n>>>"
        )
        assert out == ("f.py", " spaced  out ", "\nnewline\n")


class TestPatchAnchorRender:
    def test_renders_success(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
        out = tui._render_patch_anchor(cfg, "a.py <<<x = 1>>> <<<x = 2>>>")
        assert "patched a.py" in out
        assert (tmp_path / "a.py").read_text() == "x = 2\n"

    def test_renders_error_on_no_match(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        (tmp_path / "a.py").write_text("hello\n", encoding="utf-8")
        out = tui._render_patch_anchor(cfg, "a.py <<<missing>>> <<<x>>>")
        assert "patch_anchor error" in out
        assert "not found" in out


# --------------------------------------------------- perplexity wrappers
class TestRenderPerplexity:
    def test_search_empty_query_via_dispatcher_branch(self) -> None:
        # The dispatcher rejects empty rest before calling the renderer,
        # but the renderer itself should also surface the underlying
        # PerplexityError if called with whitespace.
        out = tui._render_perplexity_search("   ")
        assert "perplexity_search error" in out

    def test_search_uses_module(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict = {}

        def fake_search(query: str, *, max_results: int = 10):
            captured["query"] = query
            captured["max_results"] = max_results
            return [
                perplexity_tools.PerplexitySearchResult(
                    title="T", url="https://u/", snippet="s"
                )
            ]

        monkeypatch.setattr(
            perplexity_tools, "perplexity_search", fake_search
        )
        out = tui._render_perplexity_search("query terms")
        assert captured == {"query": "query terms", "max_results": 10}
        assert "1. T" in out
        assert "https://u/" in out

    def test_chat_usage_when_empty(self) -> None:
        for kind in ("ask", "research", "reason"):
            assert tui._render_perplexity_chat(kind, "") == f"usage: /perplexity_{kind} <question>"

    def test_chat_dispatches_by_kind(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[str] = []

        def fake_ask(messages, **_kw):
            seen.append("ask")
            return "ASK:" + messages[0]["content"]

        def fake_research(messages, **kw):
            seen.append("research")
            return f"RESEARCH(strip={kw.get('strip_thinking')}):{messages[0]['content']}"

        def fake_reason(messages, **kw):
            seen.append("reason")
            return f"REASON(strip={kw.get('strip_thinking')}):{messages[0]['content']}"

        monkeypatch.setattr(perplexity_tools, "perplexity_ask", fake_ask)
        monkeypatch.setattr(perplexity_tools, "perplexity_research", fake_research)
        monkeypatch.setattr(perplexity_tools, "perplexity_reason", fake_reason)

        assert tui._render_perplexity_chat("ask", "hi") == "ASK:hi"
        assert tui._render_perplexity_chat("research", "hi", strip_thinking=True) == \
            "RESEARCH(strip=True):hi"
        assert tui._render_perplexity_chat("reason", "hi", strip_thinking=True) == \
            "REASON(strip=True):hi"
        assert seen == ["ask", "research", "reason"]

    def test_chat_surfaces_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*_a, **_kw):
            raise perplexity_tools.PerplexityError("nope")

        monkeypatch.setattr(perplexity_tools, "perplexity_ask", boom)
        out = tui._render_perplexity_chat("ask", "hi")
        assert "perplexity_ask error" in out
        assert "nope" in out


# ----------------------------------------------------------------- /bug
class TestBug:
    def test_writes_report(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        history = [
            ChatMessage(role="user", content="hello"),
            ChatMessage(role="assistant", content="hi back"),
        ]
        out = tui._render_bug(cfg, history, "buggy thing happened")
        assert out.startswith("wrote bug report:")
        bugs = list((tmp_path / ".agent" / "bugs").glob("*.md"))
        assert len(bugs) == 1
        body = bugs[0].read_text(encoding="utf-8")
        assert "# Bug report" in body
        assert "buggy thing happened" in body
        assert "## Environment" in body
        assert "## Recent chat history" in body
        assert "hello" in body
        assert "hi back" in body

    def test_handles_no_history(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        out = tui._render_bug(cfg, None, "")
        assert out.startswith("wrote bug report:")
        body = next((tmp_path / ".agent" / "bugs").glob("*.md")).read_text()
        assert "(no chat history)" in body

    def test_redacts_secrets(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        history = [
            ChatMessage(
                role="user",
                content=(
                    "set PERPLEXITY_API_KEY=pplx-abcdef0123456789 "
                    "and Authorization: Bearer sk-ABCDEFGHIJ1234567890 "
                    "and ghp_abcdefghijklmnopqrstuvwxyz1234"
                ),
            )
        ]
        tui._render_bug(cfg, history, "Bearer sk-leakytokenABCDEFGHIJ")
        body = next((tmp_path / ".agent" / "bugs").glob("*.md")).read_text()
        assert "pplx-abcdef" not in body
        assert "sk-ABCDEFGHIJ" not in body
        assert "ghp_abcdefghij" not in body
        assert "sk-leakytokenABCDEFGHIJ" not in body
        assert "[REDACTED]" in body

    def test_redact_helper_pure(self) -> None:
        assert "[REDACTED]" in tui._redact_for_bug_report(
            "api_key = 'pplx-secretvalue123'"
        )
        # Innocuous text passes through unchanged
        assert tui._redact_for_bug_report("hello world") == "hello world"


# --------------------------------------------------- slash registration
class TestSlashRegistration:
    @pytest.mark.parametrize(
        "name",
        [
            "/patch_anchor",
            "/perplexity_search",
            "/perplexity_ask",
            "/perplexity_research",
            "/perplexity_reason",
            "/bug",
        ],
    )
    def test_command_registered(self, name: str) -> None:
        assert name in tui.SLASH_COMMANDS
        # Tab-completion routing also picks them up.
        assert name in tui.slash_completions(name[:5])
