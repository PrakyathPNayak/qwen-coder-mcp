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
        # The renderer surfaces a usage line when the query is whitespace.
        out = tui._render_perplexity_search("   ")
        assert "usage: /perplexity_search" in out

    def test_search_uses_module(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict = {}

        def fake_search(query: str, **kw):
            captured["query"] = query
            captured.update(kw)
            return [
                perplexity_tools.PerplexitySearchResult(
                    title="T", url="https://u/", snippet="s"
                )
            ]

        monkeypatch.setattr(
            perplexity_tools, "perplexity_search", fake_search
        )
        out = tui._render_perplexity_search("query terms")
        assert captured["query"] == "query terms"
        # No flags supplied, so no extra kwargs forwarded.
        assert "max_results" not in captured
        assert "1. T" in out
        assert "https://u/" in out

    def test_chat_usage_when_empty(self) -> None:
        for kind in ("ask", "research", "reason"):
            assert tui._render_perplexity_chat(kind, "") == (
                f"usage: /perplexity_{kind} [flags] <question>"
            )

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
        # research/reason default to strip_thinking=True via the dispatcher,
        # so the renderer's default mirrors that.
        assert tui._render_perplexity_chat(
            "research", "hi", default_strip_thinking=True
        ) == "RESEARCH(strip=True):hi"
        assert tui._render_perplexity_chat(
            "reason", "hi", default_strip_thinking=True
        ) == "REASON(strip=True):hi"
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
            "/perplexity_embed",
            "/perplexity_async",
            "/bug",
        ],
    )
    def test_command_registered(self, name: str) -> None:
        assert name in tui.SLASH_COMMANDS
        # Tab-completion routing also picks them up.
        assert name in tui.slash_completions(name[:5])


# --------------------------------------------------- flag parser
class TestPerplexityFlagParser:
    def test_no_flags(self) -> None:
        opts, rest = tui._parse_perplexity_flags("hello world")
        assert opts == {}
        assert rest == "hello world"

    def test_single_string_flag(self) -> None:
        opts, rest = tui._parse_perplexity_flags("--recency week tell me")
        assert opts == {"search_recency_filter": "week"}
        assert rest == "tell me"

    def test_repeatable_list_flag(self) -> None:
        opts, rest = tui._parse_perplexity_flags(
            "--domain a.com,b.com --domain c.com find x"
        )
        assert opts["search_domain_filter"] == ["a.com", "b.com", "c.com"]
        assert rest == "find x"

    def test_int_and_float_flags(self) -> None:
        opts, rest = tui._parse_perplexity_flags(
            "--max 5 --tpp 512 --temperature 0.3 --top-p 0.9 query"
        )
        assert opts["max_results"] == 5
        assert opts["max_tokens_per_page"] == 512
        assert opts["temperature"] == 0.3
        assert opts["top_p"] == 0.9
        assert rest == "query"

    def test_bool_flags(self) -> None:
        opts, rest = tui._parse_perplexity_flags(
            "--no-search --related --keep-think question"
        )
        assert opts["disable_search"] is True
        assert opts["return_related_questions"] is True
        assert opts["strip_thinking"] is False
        assert rest == "question"

    def test_strip_think_overrides_keep(self) -> None:
        opts, _ = tui._parse_perplexity_flags("--keep-think --strip-think x")
        assert opts["strip_thinking"] is True

    def test_unknown_flag_terminates(self) -> None:
        # Unknown flags stay in the remainder so the user sees the
        # offending text rather than a silent drop.
        opts, rest = tui._parse_perplexity_flags("--bogus value rest")
        assert opts == {}
        assert rest == "--bogus value rest"

    def test_missing_value_raises(self) -> None:
        with pytest.raises(ValueError):
            tui._parse_perplexity_flags("--recency")

    def test_int_parse_failure_raises(self) -> None:
        with pytest.raises(ValueError):
            tui._parse_perplexity_flags("--max NOT_AN_INT q")


# --------------------------------------------------- search renderer w/ flags
class TestRenderPerplexitySearchFlags:
    def test_forwards_flags(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict = {}

        def fake_search(query: str, **kw):
            captured["query"] = query
            captured.update(kw)
            return []

        monkeypatch.setattr(perplexity_tools, "perplexity_search", fake_search)
        out = tui._render_perplexity_search(
            "--mode academic --recency month --domain a.com,b.com python typing"
        )
        assert captured["query"] == "python typing"
        assert captured["search_mode"] == "academic"
        assert captured["search_recency_filter"] == "month"
        assert captured["search_domain_filter"] == ["a.com", "b.com"]
        assert "(no results)" in out

    def test_drops_chat_only_keys(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict = {}

        def fake_search(query: str, **kw):
            captured.update(kw)
            return []

        monkeypatch.setattr(perplexity_tools, "perplexity_search", fake_search)
        # --temperature is a chat-only flag; it must not be forwarded
        # to perplexity_search.
        tui._render_perplexity_search("--temperature 0.5 --max 3 query")
        assert "temperature" not in captured
        assert captured.get("max_results") == 3


# --------------------------------------------------- chat renderer w/ flags
class TestRenderPerplexityChatFlags:
    def test_forwards_chat_options(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict = {}

        def fake_ask(messages, **kw):
            captured["messages"] = messages
            captured.update(kw)
            return "ok"

        monkeypatch.setattr(perplexity_tools, "perplexity_ask", fake_ask)
        out = tui._render_perplexity_chat(
            "ask",
            "--context high --temperature 0.2 --max-tokens 500 What is X?",
        )
        assert out == "ok"
        assert captured["messages"][0]["content"] == "What is X?"
        assert captured["search_context_size"] == "high"
        assert captured["temperature"] == 0.2
        assert captured["max_tokens"] == 500
        # Search-only / embed-only kwargs are NOT in the chat-opt forward.
        assert "max_results" not in captured

    def test_keep_think_overrides_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict = {}

        def fake_research(messages, **kw):
            captured.update(kw)
            return "r"

        monkeypatch.setattr(
            perplexity_tools, "perplexity_research", fake_research
        )
        # research defaults to strip=True; --keep-think flips to False.
        tui._render_perplexity_chat(
            "research", "--keep-think topic", default_strip_thinking=True
        )
        assert captured["strip_thinking"] is False


# --------------------------------------------------- embed renderer
class TestRenderPerplexityEmbed:
    def test_usage_line_when_missing_args(self) -> None:
        assert "usage:" in tui._render_perplexity_embed("")
        assert "usage:" in tui._render_perplexity_embed("just-model")

    def test_forwards_to_module(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict = {}

        def fake_embed(input, **kw):
            captured["input"] = input
            captured.update(kw)
            return perplexity_tools.PerplexityEmbeddingsResult(
                data=[{"embedding": [0.1, 0.2, 0.3], "index": 0}],
                model="pplx-embed-v1-0.6b",
            )

        monkeypatch.setattr(perplexity_tools, "perplexity_embed", fake_embed)
        out = tui._render_perplexity_embed(
            "--dim 256 --encoding base64_int8 pplx-embed-v1-0.6b hello"
        )
        assert captured["input"] == "hello"
        assert captured["model"] == "pplx-embed-v1-0.6b"
        assert captured["dimensions"] == 256
        assert captured["encoding_format"] == "base64_int8"
        assert "Generated 1 embedding" in out

    def test_surfaces_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*_a, **_kw):
            raise perplexity_tools.PerplexityError("nope")

        monkeypatch.setattr(perplexity_tools, "perplexity_embed", boom)
        out = tui._render_perplexity_embed("pplx-embed-v1-4b some text")
        assert "perplexity_embed error" in out
        assert "nope" in out


# --------------------------------------------------- async renderer
class TestRenderPerplexityAsync:
    def test_usage_when_no_subcmd(self) -> None:
        assert "usage:" in tui._render_perplexity_async("")
        assert "usage:" in tui._render_perplexity_async("bogus")

    def test_list_dispatches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        called = {"n": 0}

        def fake_list():
            called["n"] += 1
            return {"data": []}

        monkeypatch.setattr(
            perplexity_tools, "perplexity_async_list", fake_list
        )
        out = tui._render_perplexity_async("list")
        assert called["n"] == 1
        assert "no async jobs" in out

    def test_get_requires_id(self) -> None:
        out = tui._render_perplexity_async("get")
        assert "usage:" in out

    def test_get_dispatches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict = {}

        def fake_get(rid):
            captured["rid"] = rid
            return {"id": rid, "model": "sonar-pro", "status": "COMPLETED"}

        monkeypatch.setattr(
            perplexity_tools, "perplexity_async_get", fake_get
        )
        out = tui._render_perplexity_async("get xyz123")
        assert captured["rid"] == "xyz123"
        assert "[COMPLETED]" in out
        assert "id=xyz123" in out

    def test_create_requires_model_and_question(self) -> None:
        out = tui._render_perplexity_async("create")
        assert "usage:" in out
        out = tui._render_perplexity_async("create sonar-pro")
        assert "usage:" in out

    def test_create_dispatches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict = {}

        def fake_create(messages, **kw):
            captured["messages"] = messages
            captured.update(kw)
            return {
                "id": "job-1",
                "model": kw["model"],
                "status": "CREATED",
            }

        monkeypatch.setattr(
            perplexity_tools, "perplexity_async_create", fake_create
        )
        out = tui._render_perplexity_async(
            "create sonar-pro --temperature 0.5 explain X"
        )
        assert captured["messages"][0]["content"] == "explain X"
        assert captured["model"] == "sonar-pro"
        assert captured["temperature"] == 0.5
        assert "[CREATED]" in out
        assert "id=job-1" in out
