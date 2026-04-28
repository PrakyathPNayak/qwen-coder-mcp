"""Loop 130 tests: TUI slash-command parser and dispatcher."""
from __future__ import annotations

from pathlib import Path

import pytest

from qwen_coder_mcp import fs_tools, tui
from qwen_coder_mcp.qwen_client import ChatMessage


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.reply = "fake reply"

    def system_user(self, system: str, user: str, **kw) -> str:
        self.calls.append(("system_user", system[:20], user[:20]))
        return self.reply

    def chat(self, history, **kw) -> str:
        self.calls.append(("chat", len(history)))
        return self.reply


class TestParseSlash:
    def test_none_for_plain_text(self) -> None:
        assert tui.parse_slash("hello world") is None

    def test_help(self) -> None:
        c = tui.parse_slash("/help")
        assert c is not None
        assert c.name == "help"
        assert c.args == []

    def test_with_args(self) -> None:
        c = tui.parse_slash("/search python asyncio")
        assert c is not None
        assert c.name == "search"
        assert c.args == ["python", "asyncio"]
        assert c.rest == "python asyncio"

    def test_lowercases(self) -> None:
        c = tui.parse_slash("/SEARCH foo")
        assert c is not None
        assert c.name == "search"

    def test_empty_slash(self) -> None:
        c = tui.parse_slash("/")
        assert c is not None
        assert c.name == ""

    def test_strips(self) -> None:
        c = tui.parse_slash("/ help  ")
        assert c is not None
        assert c.name == "help"


class TestDispatchSlash:
    def test_help(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        text, q = tui.dispatch_slash(
            tui.SlashCommand(name="help"),
            client=_FakeClient(),
            fs_cfg=cfg,
        )
        assert "Slash commands" in text
        assert q is False

    def test_quit(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        _, q = tui.dispatch_slash(
            tui.SlashCommand(name="quit"),
            client=_FakeClient(),
            fs_cfg=cfg,
        )
        assert q is True

    def test_unknown(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        text, _ = tui.dispatch_slash(
            tui.SlashCommand(name="bogus"),
            client=_FakeClient(),
            fs_cfg=cfg,
        )
        assert "unknown" in text

    def test_read_usage(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        text, _ = tui.dispatch_slash(
            tui.SlashCommand(name="read"),
            client=_FakeClient(),
            fs_cfg=cfg,
        )
        assert "usage:" in text

    def test_read_real(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
        cfg = fs_tools.FsConfig(root=tmp_path)
        text, _ = tui.dispatch_slash(
            tui.SlashCommand(name="read", args=["a.txt"], rest="a.txt"),
            client=_FakeClient(),
            fs_cfg=cfg,
        )
        assert "hello" in text

    def test_ls_default(self, tmp_path: Path) -> None:
        (tmp_path / "x").mkdir()
        cfg = fs_tools.FsConfig(root=tmp_path)
        text, _ = tui.dispatch_slash(
            tui.SlashCommand(name="ls"), client=_FakeClient(), fs_cfg=cfg
        )
        assert "x/" in text

    def test_find_bugs(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
        cfg = fs_tools.FsConfig(root=tmp_path)
        client = _FakeClient()
        text, _ = tui.dispatch_slash(
            tui.SlashCommand(name="find_bugs", args=["a.py"], rest="a.py"),
            client=client,
            fs_cfg=cfg,
        )
        assert text == "fake reply"
        assert client.calls and client.calls[0][0] == "system_user"

    def test_explain(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
        cfg = fs_tools.FsConfig(root=tmp_path)
        client = _FakeClient()
        text, _ = tui.dispatch_slash(
            tui.SlashCommand(name="explain", args=["a.py"], rest="a.py"),
            client=client,
            fs_cfg=cfg,
        )
        assert text == "fake reply"
        assert client.calls

    def test_read_escape_returns_error(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        text, _ = tui.dispatch_slash(
            tui.SlashCommand(name="read", args=["../etc/passwd"], rest="../etc/passwd"),
            client=_FakeClient(),
            fs_cfg=cfg,
        )
        assert "error" in text and "escapes" in text

    def test_search_usage(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        text, _ = tui.dispatch_slash(
            tui.SlashCommand(name="search"),
            client=_FakeClient(),
            fs_cfg=cfg,
        )
        assert "usage:" in text

    def test_fetch_usage(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        text, _ = tui.dispatch_slash(
            tui.SlashCommand(name="fetch"),
            client=_FakeClient(),
            fs_cfg=cfg,
        )
        assert "usage:" in text


class TestChatTurn:
    def test_appends_history(self) -> None:
        client = _FakeClient()
        history: list[ChatMessage] = []
        reply = tui.chat_turn(history, "hello", client=client)
        assert reply == "fake reply"
        assert history[0].role == "system"
        assert history[1].role == "user"
        assert history[1].content == "hello"
        assert history[2].role == "assistant"
        assert history[2].content == "fake reply"

    def test_preserves_existing_system(self) -> None:
        client = _FakeClient()
        history = [ChatMessage(role="system", content="custom")]
        tui.chat_turn(history, "hi", client=client)
        assert history[0].content == "custom"

    def test_multi_turn(self) -> None:
        client = _FakeClient()
        history: list[ChatMessage] = []
        tui.chat_turn(history, "first", client=client)
        tui.chat_turn(history, "second", client=client)
        roles = [m.role for m in history]
        assert roles == ["system", "user", "assistant", "user", "assistant"]


class TestBuildApp:
    def test_textual_available(self) -> None:
        pytest.importorskip("textual")
        AppCls = tui._build_app(
            client_factory=lambda: _FakeClient(),
            fs_cfg=fs_tools.FsConfig(root=Path.cwd()),
        )
        app = AppCls()
        assert app is not None


class TestExtractDiff:
    def test_diff_fence(self) -> None:
        text = "before\n```diff\ndiff --git a/x b/x\n@@\n-a\n+b\n```\nafter"
        out = tui.extract_diff(text)
        assert out is not None
        assert "diff --git a/x b/x" in out
        assert "after" not in out

    def test_patch_fence(self) -> None:
        text = "```patch\ndiff --git a/x b/x\n```"
        out = tui.extract_diff(text)
        assert out is not None
        assert "diff --git" in out

    def test_bare_diff(self) -> None:
        text = "blah\ndiff --git a/x b/x\n@@\n-a\n+b\n"
        out = tui.extract_diff(text)
        assert out is not None
        assert out.startswith("diff --git")
        assert "blah" not in out

    def test_no_diff(self) -> None:
        assert tui.extract_diff("just text") is None
        assert tui.extract_diff("") is None


class TestApplySlash:
    def _git_init(self, root: Path) -> None:
        import subprocess
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(
            [
                "git", "-c", "user.email=a@b.c", "-c", "user.name=a",
                "commit", "--allow-empty", "-m", "init",
            ],
            cwd=root,
            check=True,
            capture_output=True,
        )

    def test_no_history(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        text, _ = tui.dispatch_slash(
            tui.SlashCommand(name="apply"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=[],
        )
        assert "no assistant reply" in text

    def test_no_diff_in_reply(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        history = [
            ChatMessage(role="user", content="hi"),
            ChatMessage(role="assistant", content="hello back, no diff"),
        ]
        text, _ = tui.dispatch_slash(
            tui.SlashCommand(name="apply"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=history,
        )
        assert "no unified diff" in text

    def test_applies_diff(self, tmp_path: Path) -> None:
        import subprocess
        self._git_init(tmp_path)
        (tmp_path / "a.txt").write_text("hello\n")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
        subprocess.run(
            [
                "git", "-c", "user.email=a@b.c", "-c", "user.name=a",
                "commit", "-m", "add",
            ],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )
        cfg = fs_tools.FsConfig(root=tmp_path)
        diff_block = (
            "```diff\n"
            "diff --git a/a.txt b/a.txt\n"
            "--- a/a.txt\n"
            "+++ b/a.txt\n"
            "@@ -1 +1 @@\n"
            "-hello\n"
            "+world\n"
            "```\n"
        )
        history = [
            ChatMessage(role="user", content="change hello to world"),
            ChatMessage(role="assistant", content=diff_block),
        ]
        text, _ = tui.dispatch_slash(
            tui.SlashCommand(name="apply"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=history,
        )
        assert "ok" in text
        assert (tmp_path / "a.txt").read_text() == "world\n"


class TestHistorySlash:
    def test_empty(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        text, _ = tui.dispatch_slash(
            tui.SlashCommand(name="history"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=[],
        )
        assert "no history" in text

    def test_renders(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        history = [
            ChatMessage(role="system", content="sys"),
            ChatMessage(role="user", content="hi"),
            ChatMessage(role="assistant", content="hello"),
        ]
        text, _ = tui.dispatch_slash(
            tui.SlashCommand(name="history"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=history,
        )
        assert "you> hi" in text
        assert "qwen> hello" in text
        assert "sys" not in text

    def test_n_limit(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        history = [
            ChatMessage(role="user", content=f"u{i}") for i in range(20)
        ]
        text, _ = tui.dispatch_slash(
            tui.SlashCommand(name="history", args=["3"], rest="3"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=history,
        )
        assert "u17" in text
        assert "u19" in text
        assert "u0" not in text

    def test_bad_n(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        text, _ = tui.dispatch_slash(
            tui.SlashCommand(name="history", args=["abc"], rest="abc"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=[ChatMessage(role="user", content="x")],
        )
        assert "usage:" in text


class TestPilotSmoke:
    @pytest.mark.anyio("asyncio")
    async def test_help_via_pilot(self, tmp_path: Path) -> None:
        pytest.importorskip("textual")
        AppCls = tui._build_app(
            client_factory=lambda: _FakeClient(),
            fs_cfg=fs_tools.FsConfig(root=tmp_path),
        )
        app = AppCls()
        async with app.run_test() as pilot:
            from textual.widgets import Input, RichLog
            entry = app.query_one("#entry", Input)
            entry.value = "/help"
            await pilot.press("enter")
            await pilot.pause()
            log = app.query_one("#log", RichLog)
            rendered = "\n".join(str(line) for line in log.lines)
            assert "Slash commands" in rendered


class _FakeStreamingClient:
    def __init__(self, chunks: list[str], error: Exception | None = None) -> None:
        self.chunks = chunks
        self.error = error

    def chat_stream(self, history, **kw):
        if self.error is not None:
            raise self.error
        for c in self.chunks:
            yield c

    def chat(self, history, **kw):
        return "".join(self.chunks)

    def system_user(self, *a, **kw):
        return "n/a"


class TestChatTurnStream:
    def test_yields_and_commits(self) -> None:
        client = _FakeStreamingClient(["hel", "lo"])
        history: list[ChatMessage] = []
        chunks: list[tuple[str, str]] = []
        for c, accum in tui.chat_turn_stream(history, "hi", client=client):
            chunks.append((c, accum))
        assert chunks == [("hel", "hel"), ("lo", "hello")]
        assert history[-1].role == "assistant"
        assert history[-1].content == "hello"

    def test_error_yields_message_no_commit(self) -> None:
        client = _FakeStreamingClient([], error=RuntimeError("boom"))
        history: list[ChatMessage] = []
        out = list(tui.chat_turn_stream(history, "hi", client=client))
        assert len(out) == 1
        assert "stream error" in out[0][0]
        assert "boom" in out[0][0]
        assert all(m.role != "assistant" for m in history)

    def test_preserves_existing_system(self) -> None:
        client = _FakeStreamingClient(["x"])
        history = [ChatMessage(role="system", content="custom")]
        list(tui.chat_turn_stream(history, "hi", client=client))
        assert history[0].content == "custom"


class TestDiffSlash:
    def test_usage(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        text, _ = tui.dispatch_slash(
            tui.SlashCommand(name="diff"),
            client=_FakeClient(),
            fs_cfg=cfg,
        )
        assert "usage:" in text

    def test_one_arg(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        text, _ = tui.dispatch_slash(
            tui.SlashCommand(name="diff", args=["a.txt"], rest="a.txt"),
            client=_FakeClient(),
            fs_cfg=cfg,
        )
        # /diff <path> now means "diff against HEAD"; on a non-git tmp_path
        # the underlying git call surfaces an error. Either way it is no
        # longer a usage hint.
        assert "usage:" not in text

    def test_diff(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("hello\nworld\n")
        (tmp_path / "b.txt").write_text("hello\nthere\n")
        cfg = fs_tools.FsConfig(root=tmp_path)
        text, _ = tui.dispatch_slash(
            tui.SlashCommand(name="diff", args=["a.txt", "b.txt"], rest="a.txt b.txt"),
            client=_FakeClient(),
            fs_cfg=cfg,
        )
        assert "-world" in text
        assert "+there" in text
        assert "--- a.txt" in text
        assert "+++ b.txt" in text

    def test_identical(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("same\n")
        (tmp_path / "b.txt").write_text("same\n")
        cfg = fs_tools.FsConfig(root=tmp_path)
        text, _ = tui.dispatch_slash(
            tui.SlashCommand(name="diff", args=["a.txt", "b.txt"], rest=""),
            client=_FakeClient(),
            fs_cfg=cfg,
        )
        assert "identical" in text

    def test_missing(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        text, _ = tui.dispatch_slash(
            tui.SlashCommand(name="diff", args=["nope.txt", "also.txt"], rest=""),
            client=_FakeClient(),
            fs_cfg=cfg,
        )
        assert "diff error" in text

    def test_escape(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("x")
        cfg = fs_tools.FsConfig(root=tmp_path)
        text, _ = tui.dispatch_slash(
            tui.SlashCommand(name="diff", args=["a.txt", "../etc/passwd"], rest=""),
            client=_FakeClient(),
            fs_cfg=cfg,
        )
        assert "error" in text


# ----------------------------------------------------------- Loop 134
class _ConnRefusedClient:
    """Client stub whose chat() raises a ConnectError-shaped exception."""
    def chat(self, history, **kw):
        import httpx
        raise httpx.ConnectError("Connection refused [Errno 111]")

    def chat_stream(self, history, **kw):  # not used but keep parity
        raise NotImplementedError


class TestFriendlyChatError:
    def test_connect_error_includes_hint(self) -> None:
        client = _ConnRefusedClient()
        history: list[ChatMessage] = []
        out = tui.chat_turn(history, "hello", client=client)
        assert "ConnectError" in out
        assert "serve_qwen" in out
        # User message stays so the user can retry; no assistant reply added.
        assert any(m.role == "user" for m in history)
        assert not any(m.role == "assistant" for m in history)

    def test_non_connect_error_no_hint(self) -> None:
        class Boom:
            def chat(self, history, **kw):
                raise ValueError("bad payload")
        out = tui.chat_turn([], "hi", client=Boom())
        assert "ValueError" in out
        assert "serve_qwen" not in out


class _HealthClient:
    """Stub exposing only health_check for banner tests."""
    def __init__(self, payload):
        self._payload = payload

    def health_check(self):
        return self._payload


class TestHealthBanner:
    """Drive the App's _render_health_banner via Pilot."""

    @pytest.mark.anyio("asyncio")
    async def test_banner_ok(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        AppCls = tui._build_app(
            client_factory=lambda: _HealthClient(
                {"ok": True, "models": ["qwen3.6-27b"]}
            ),
            fs_cfg=cfg,
        )
        app = AppCls()
        async with app.run_test() as pilot:
            await pilot.pause()
            from textual.widgets import RichLog
            log = app.query_one("#log", RichLog)
            text = "\n".join(str(line) for line in log.lines)
            assert "backend ok" in text
            assert "qwen3" in text and "27b" in text

    @pytest.mark.anyio("asyncio")
    async def test_banner_unavailable_with_hint(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        AppCls = tui._build_app(
            client_factory=lambda: _HealthClient(
                {
                    "ok": False,
                    "error": "connection refused",
                    "hint": "start scripts/serve_qwen.sh",
                }
            ),
            fs_cfg=cfg,
        )
        app = AppCls()
        async with app.run_test() as pilot:
            await pilot.pause()
            from textual.widgets import RichLog
            log = app.query_one("#log", RichLog)
            text = "\n".join(str(line) for line in log.lines)
            assert "unavailable" in text
            assert "hint" in text


# ----------------------------------------------------------- Loop 135 slash commands
class TestRunSlash:
    def test_run_command(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        text, q = tui.dispatch_slash(
            tui.parse_slash("/run echo hello"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=[],
        )
        assert q is False
        assert "hello" in text
        assert "exit=0" in text

    def test_run_usage(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/run"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=[],
        )
        assert "usage" in text

    def test_run_denied(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/run sudo rm anything"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=[],
        )
        assert "run error" in text


class TestGrepSlash:
    def test_grep(self, tmp_path: Path) -> None:
        (tmp_path / "x.py").write_text("hello world\n")
        cfg = fs_tools.FsConfig(root=tmp_path)
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/grep hello"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=[],
        )
        assert "x.py" in text and "hello" in text

    def test_grep_no_match(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/grep nonexistent"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=[],
        )
        assert "no matches" in text

    def test_grep_usage(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/grep"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=[],
        )
        assert "usage" in text


class TestFindSlash:
    def test_find(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("")
        cfg = fs_tools.FsConfig(root=tmp_path)
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/find *.py"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=[],
        )
        assert "a.py" in text

    def test_find_usage(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/find"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=[],
        )
        assert "usage" in text


class TestClearAndSave:
    def test_clear(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        history = [
            ChatMessage("system", "sys"),
            ChatMessage("user", "hi"),
            ChatMessage("assistant", "hello"),
        ]
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/clear"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=history,
        )
        assert "cleared" in text
        assert history == []

    def test_save(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        history = [
            ChatMessage("system", "sys"),
            ChatMessage("user", "hi"),
            ChatMessage("assistant", "hello"),
        ]
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/save out.md"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=history,
        )
        assert "saved 2 turns" in text
        body = (tmp_path / "out.md").read_text()
        assert "you>" in body and "qwen>" in body

    def test_save_usage(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/save"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=[ChatMessage("user", "hi")],
        )
        assert "usage" in text

    def test_save_empty(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/save out.md"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=[],
        )
        assert "no chat" in text


# ----------------------------------------------------------- Loop 136
class TestGitSlash:
    def test_git_status(self, tmp_path: Path) -> None:
        # init a repo so git status doesn't error out.
        import subprocess
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
        cfg = fs_tools.FsConfig(root=tmp_path)
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/git status"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=[],
        )
        assert "exit=0" in text

    def test_git_rejects_unknown(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/git push"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=[],
        )
        assert "not allowed" in text

    def test_git_usage(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/git"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=[],
        )
        assert "usage" in text


class TestTestsSlash:
    def test_tests_runs_pytest(self, tmp_path: Path) -> None:
        # tiny passing test so pytest exits 0 with -q
        (tmp_path / "test_x.py").write_text(
            "def test_ok():\n    assert 1 == 1\n"
        )
        cfg = fs_tools.FsConfig(root=tmp_path)
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/tests"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=[],
        )
        assert "exit=0" in text


class TestAtMentionExpansion:
    def test_expands_single_file(self, tmp_path: Path) -> None:
        (tmp_path / "src.py").write_text("def f(): pass\n")
        cfg = fs_tools.FsConfig(root=tmp_path)
        out = tui.expand_at_mentions(cfg, "look at @src.py please")
        assert "attached files" in out
        assert "def f()" in out

    def test_no_at_returns_unchanged(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        assert tui.expand_at_mentions(cfg, "no mentions") == "no mentions"

    def test_missing_file_silent(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        out = tui.expand_at_mentions(cfg, "look at @does_not_exist.py")
        # Original token preserved, no attachment block.
        assert "@does_not_exist.py" in out
        assert "attached files" not in out

    def test_dedups(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("alpha")
        cfg = fs_tools.FsConfig(root=tmp_path)
        out = tui.expand_at_mentions(cfg, "see @a.py and again @a.py")
        assert out.count("# a.py") == 1

    def test_truncation(self, tmp_path: Path) -> None:
        (tmp_path / "big.txt").write_text("x" * 50000)
        cfg = fs_tools.FsConfig(root=tmp_path)
        out = tui.expand_at_mentions(cfg, "@big.txt", max_bytes_each=100)
        assert "[truncated]" in out

    def test_path_escape_silent(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        out = tui.expand_at_mentions(cfg, "see @../etc/passwd")
        # No attachment, original mention preserved.
        assert "attached files" not in out

    def test_chat_turn_uses_expansion(self, tmp_path: Path) -> None:
        (tmp_path / "x.py").write_text("hello content\n")
        cfg = fs_tools.FsConfig(root=tmp_path)

        captured: list[list] = []
        class C:
            def chat(self, history, **kw):
                captured.append([m.content for m in history if m.role == "user"])
                return "ack"

        history: list[ChatMessage] = []
        tui.chat_turn(history, "review @x.py", client=C(), fs_cfg=cfg)
        # The user message stored in history should contain expanded body.
        user_msg = [m.content for m in history if m.role == "user"][-1]
        assert "hello content" in user_msg


# ----------------------------------------------------------- Loop 137
class TestEstimateTokens:
    def test_empty(self) -> None:
        assert tui.estimate_tokens("") == 0

    def test_short(self) -> None:
        assert tui.estimate_tokens("a") == 1

    def test_scales_with_length(self) -> None:
        a = tui.estimate_tokens("x" * 40)
        b = tui.estimate_tokens("x" * 400)
        assert b > a
        assert a == 10
        assert b == 100


class TestTokensSlash:
    def test_tokens_summary(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        history: list[ChatMessage] = [
            ChatMessage(role="system", content="x" * 40),
            ChatMessage(role="user", content="y" * 80),
        ]
        text, quit_now = tui.dispatch_slash(
            tui.parse_slash("/tokens"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=history,
        )
        assert quit_now is False
        assert "tokens across" in text
        assert "2 messages" in text

    def test_tokens_no_history(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/tokens"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=None,
        )
        assert "no history" in text


# ----------------------------------------------------------- Loop 138
class TestSysPromptSlash:
    def test_show_when_present(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        history = [ChatMessage(role="system", content="be brief")]
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/sysprompt"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=history,
        )
        assert "be brief" in text

    def test_show_when_missing(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/sysprompt"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=[],
        )
        assert "(none)" in text

    def test_replace_existing(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        history = [
            ChatMessage(role="system", content="old"),
            ChatMessage(role="user", content="hi"),
        ]
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/sysprompt act as a python tutor"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=history,
        )
        assert "set" in text
        assert history[0].role == "system"
        assert history[0].content == "act as a python tutor"
        # User message preserved.
        assert history[1].role == "user"

    def test_insert_when_absent(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        history: list[ChatMessage] = []
        tui.dispatch_slash(
            tui.parse_slash("/sysprompt brand new"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=history,
        )
        assert len(history) == 1
        assert history[0].role == "system"
        assert history[0].content == "brand new"


class TestModelSlash:
    def test_shows_current(self, tmp_path: Path) -> None:
        from qwen_coder_mcp.config import Settings
        cfg = fs_tools.FsConfig(root=tmp_path)

        class C:
            settings = Settings(
                base_url="http://x",
                api_key="k",
                model="qwen-foo",
                timeout=5.0,
                max_tokens=10,
                loop_interval_seconds=1,
                loop_max_file_bytes=1000,
                loop_push=False,
            )

        text, _ = tui.dispatch_slash(
            tui.parse_slash("/model"),
            client=C(),
            fs_cfg=cfg,
            history=[],
        )
        assert "qwen-foo" in text

    def test_sets_new(self, tmp_path: Path) -> None:
        from qwen_coder_mcp.config import Settings
        cfg = fs_tools.FsConfig(root=tmp_path)

        class C:
            settings = Settings(
                base_url="http://x",
                api_key="k",
                model="qwen-foo",
                timeout=5.0,
                max_tokens=10,
                loop_interval_seconds=1,
                loop_max_file_bytes=1000,
                loop_push=False,
            )

        client_obj = C()
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/model qwen-bar"),
            client=client_obj,
            fs_cfg=cfg,
            history=[],
        )
        assert "qwen-bar" in text
        assert client_obj.settings.model == "qwen-bar"

    def test_no_settings(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)

        class C:
            pass

        text, _ = tui.dispatch_slash(
            tui.parse_slash("/model qwen-baz"),
            client=C(),
            fs_cfg=cfg,
            history=[],
        )
        assert "no settings" in text


# ----------------------------------------------------------- Loop 139
class TestUndoSlash:
    def test_pops_last_pair(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        history = [
            ChatMessage(role="system", content="s"),
            ChatMessage(role="user", content="u1"),
            ChatMessage(role="assistant", content="a1"),
        ]
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/undo"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=history,
        )
        assert "popped 2" in text
        assert len(history) == 1
        assert history[0].role == "system"

    def test_pops_dangling_user_only(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        history = [
            ChatMessage(role="system", content="s"),
            ChatMessage(role="user", content="u1"),
        ]
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/undo"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=history,
        )
        assert "popped 1" in text
        assert len(history) == 1

    def test_nothing_to_undo(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        history = [ChatMessage(role="system", content="s")]
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/undo"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=history,
        )
        assert "nothing to undo" in text


class TestRetrySlash:
    def test_retry_emits_sentinel(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        history = [
            ChatMessage(role="system", content="s"),
            ChatMessage(role="user", content="hello qwen"),
            ChatMessage(role="assistant", content="hi"),
        ]
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/retry"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=history,
        )
        assert text.startswith("__RETRY__")
        assert text.endswith("hello qwen")
        # History rolled back to before the user message.
        assert len(history) == 1
        assert history[0].role == "system"

    def test_retry_no_user(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        history = [ChatMessage(role="system", content="s")]
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/retry"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=history,
        )
        assert "no prior user message" in text

    def test_retry_drops_trailing_assistant_only(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        history = [
            ChatMessage(role="user", content="u1"),
            ChatMessage(role="assistant", content="a1"),
            ChatMessage(role="user", content="u2"),
            ChatMessage(role="assistant", content="a2"),
        ]
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/retry"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=history,
        )
        assert text == "__RETRY__u2"
        # Only the u2/a2 pair was stripped; u1/a1 still there.
        assert len(history) == 2
        assert history[-1].content == "a1"


# ----------------------------------------------------------- Loop 140
class TestPersistHistory:
    def test_round_trip(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        path = tui.history_file_path(cfg)
        original = [
            ChatMessage(role="system", content="be helpful"),
            ChatMessage(role="user", content="hello"),
            ChatMessage(role="assistant", content="hi there"),
        ]
        n = tui.save_history_jsonl(original, path)
        assert n == 3
        loaded = tui.load_history_jsonl(path)
        assert len(loaded) == 3
        assert loaded[0].role == "system"
        assert loaded[2].content == "hi there"

    def test_load_missing(self, tmp_path: Path) -> None:
        path = tmp_path / "nope.jsonl"
        assert tui.load_history_jsonl(path) == []

    def test_load_skips_malformed(self, tmp_path: Path) -> None:
        path = tmp_path / "h.jsonl"
        path.write_text(
            '{"role":"user","content":"hi"}\n'
            'not json at all\n'
            '{"role":"bogus","content":"x"}\n'
            '{"role":"assistant","content":"yes"}\n',
            encoding="utf-8",
        )
        loaded = tui.load_history_jsonl(path)
        assert len(loaded) == 2
        assert loaded[0].content == "hi"
        assert loaded[1].content == "yes"

    def test_save_caps_length(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        path = tui.history_file_path(cfg)
        big = [ChatMessage(role="user", content=str(i)) for i in range(50)]
        n = tui.save_history_jsonl(big, path, max_messages=10)
        assert n == 10
        loaded = tui.load_history_jsonl(path)
        # Last 10 messages, indices 40..49.
        assert loaded[0].content == "40"
        assert loaded[-1].content == "49"

    def test_history_file_path_under_root(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        p = tui.history_file_path(cfg)
        assert p == tmp_path / ".agent" / "tui_history.jsonl"


# ----------------------------------------------------------- Loop 141
class TestSlashCompletions:
    def test_empty_returns_nothing(self) -> None:
        assert tui.slash_completions("") == []

    def test_no_slash_returns_nothing(self) -> None:
        assert tui.slash_completions("hello") == []

    def test_partial_matches(self) -> None:
        out = tui.slash_completions("/he")
        assert "/help" in out
        # Only commands starting with /he survive.
        assert all(c.startswith("/he") for c in out)

    def test_exact_command(self) -> None:
        out = tui.slash_completions("/help")
        assert "/help" in out

    def test_only_slash_returns_all(self) -> None:
        out = tui.slash_completions("/")
        assert len(out) == len(tui.SLASH_COMMANDS)

    def test_args_after_command_still_uses_head(self) -> None:
        # If the user typed "/he some text" we still suggest /help etc.
        out = tui.slash_completions("/he some text")
        assert "/help" in out

    def test_unknown_prefix(self) -> None:
        assert tui.slash_completions("/zzznosuchthing") == []

    def test_all_dispatched_commands_in_list(self) -> None:
        # Every slash command branch in dispatch_slash should be listed.
        for cmd in [
            "/help", "/search", "/fetch", "/read", "/ls",
            "/find_bugs", "/explain", "/apply", "/history", "/diff",
            "/run", "/grep", "/find", "/clear", "/save",
            "/git", "/tests", "/tokens", "/sysprompt", "/model",
            "/undo", "/retry", "/quit",
        ]:
            assert cmd in tui.SLASH_COMMANDS, f"{cmd} missing"


# ----------------------------------------------------------- Loop 142
class TestDiffHead:
    def _init_repo(self, tmp_path: Path) -> None:
        import subprocess
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)

    def test_diff_against_head_after_modify(self, tmp_path: Path) -> None:
        import subprocess
        self._init_repo(tmp_path)
        f = tmp_path / "a.py"
        f.write_text("first\n")
        subprocess.run(["git", "add", "a.py"], cwd=tmp_path, check=True)
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init"],
            cwd=tmp_path, check=True,
        )
        f.write_text("second\n")

        cfg = fs_tools.FsConfig(root=tmp_path)
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/diff a.py"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=[],
        )
        assert "first" in text
        assert "second" in text

    def test_diff_against_head_no_changes(self, tmp_path: Path) -> None:
        import subprocess
        self._init_repo(tmp_path)
        f = tmp_path / "a.py"
        f.write_text("only\n")
        subprocess.run(["git", "add", "a.py"], cwd=tmp_path, check=True)
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init"],
            cwd=tmp_path, check=True,
        )

        cfg = fs_tools.FsConfig(root=tmp_path)
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/diff a.py"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=[],
        )
        assert "no changes" in text

    def test_diff_two_arg_still_works(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("hello\n")
        (tmp_path / "b.txt").write_text("world\n")
        cfg = fs_tools.FsConfig(root=tmp_path)
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/diff a.txt b.txt"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=[],
        )
        assert "hello" in text
        assert "world" in text

    def test_diff_no_args_usage(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/diff"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=[],
        )
        assert "usage" in text

    def test_diff_path_escape(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/diff ../../etc/passwd"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=[],
        )
        assert "error" in text.lower()


# ----------------------------------------------------------- Loop 143
class TestSysInfoSlash:
    def test_healthy_backend(self, tmp_path: Path) -> None:
        from qwen_coder_mcp.config import Settings
        cfg = fs_tools.FsConfig(root=tmp_path)

        class C:
            settings = Settings(
                base_url="http://localhost:8000/v1",
                api_key="k",
                model="qwen-foo",
                timeout=5.0,
                max_tokens=10,
                loop_interval_seconds=1,
                loop_max_file_bytes=1000,
                loop_push=False,
            )
            def health_check(self):
                return {"ok": True, "models": ["qwen-foo"]}

        history = [ChatMessage(role="user", content="hello world")]
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/sysinfo"),
            client=C(),
            fs_cfg=cfg,
            history=history,
        )
        assert "qwen-foo" in text
        assert "backend ok" in text
        assert "1 messages" in text
        assert str(tmp_path) in text

    def test_unhealthy_backend(self, tmp_path: Path) -> None:
        from qwen_coder_mcp.config import Settings
        cfg = fs_tools.FsConfig(root=tmp_path)

        class C:
            settings = Settings(
                base_url="http://localhost:8000/v1",
                api_key="k",
                model="qwen-foo",
                timeout=5.0,
                max_tokens=10,
                loop_interval_seconds=1,
                loop_max_file_bytes=1000,
                loop_push=False,
            )
            def health_check(self):
                return {"ok": False, "error": "connection refused"}

        text, _ = tui.dispatch_slash(
            tui.parse_slash("/sysinfo"),
            client=C(),
            fs_cfg=cfg,
            history=[],
        )
        assert "unavailable" in text
        assert "connection refused" in text

    def test_health_check_raises(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)

        class C:
            def health_check(self):
                raise RuntimeError("kaboom")

        text, _ = tui.dispatch_slash(
            tui.parse_slash("/sysinfo"),
            client=C(),
            fs_cfg=cfg,
            history=[],
        )
        assert "kaboom" in text

    def test_health_check_hint_rendered(self, tmp_path: Path) -> None:
        from qwen_coder_mcp.config import Settings
        cfg = fs_tools.FsConfig(root=tmp_path)

        class C:
            settings = Settings(
                base_url="http://localhost:8000/v1",
                api_key="k",
                model="qwen-foo",
                timeout=5.0,
                max_tokens=10,
                loop_interval_seconds=1,
                loop_max_file_bytes=1000,
                loop_push=False,
            )
            def health_check(self):
                return {
                    "ok": False,
                    "error": "connection refused: [Errno 111]",
                    "hint": "is the qwen server running? start it with scripts/serve_qwen.sh",
                }

        text, _ = tui.dispatch_slash(
            tui.parse_slash("/sysinfo"),
            client=C(),
            fs_cfg=cfg,
            history=[],
        )
        assert "unavailable" in text
        assert "connection refused" in text
        assert "scripts/serve_qwen.sh" in text
        assert "hint:" in text


# ----------------------------------------------------------- Loop 144
class TestExportSlash:
    def test_export_basic(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        history = [
            ChatMessage(role="system", content="be brief"),
            ChatMessage(role="user", content="hello"),
            ChatMessage(role="assistant", content="hi"),
        ]
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/export out.md"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=history,
        )
        assert "exported 2 turns" in text
        body = (tmp_path / "out.md").read_text()
        assert "# qwen-coder-tui chat transcript" in body
        assert "## you" in body
        assert "## qwen" in body
        assert "> system: be brief" in body

    def test_export_no_args(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/export"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=[ChatMessage(role="user", content="x")],
        )
        assert "usage" in text

    def test_export_no_history(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/export out.md"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=None,
        )
        assert "no history" in text

    def test_export_empty_history(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/export out.md"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=[],
        )
        assert "no chat to export" in text

    def test_export_path_escape(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/export ../../etc/whatever"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=[ChatMessage(role="user", content="x")],
        )
        assert "error" in text.lower()


# ----------------------------------------------------------- Loop 145
class TestPinSlash:
    def test_pin_attaches_to_system_prompt(self, tmp_path: Path) -> None:
        (tmp_path / "spec.md").write_text("read this every turn\n")
        cfg = fs_tools.FsConfig(root=tmp_path)
        history: list[ChatMessage] = [ChatMessage(role="system", content="base")]
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/pin spec.md"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=history,
        )
        assert "pinned spec.md" in text
        assert "read this every turn" in history[0].content
        assert "pinned files" in history[0].content

    def test_pin_inserts_system_when_missing(self, tmp_path: Path) -> None:
        (tmp_path / "spec.md").write_text("hi\n")
        cfg = fs_tools.FsConfig(root=tmp_path)
        history: list[ChatMessage] = []
        tui.dispatch_slash(
            tui.parse_slash("/pin spec.md"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=history,
        )
        assert history[0].role == "system"

    def test_pin_appends_second_file(self, tmp_path: Path) -> None:
        (tmp_path / "a.md").write_text("alpha\n")
        (tmp_path / "b.md").write_text("beta\n")
        cfg = fs_tools.FsConfig(root=tmp_path)
        history: list[ChatMessage] = [ChatMessage(role="system", content="base")]
        tui.dispatch_slash(
            tui.parse_slash("/pin a.md"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=history,
        )
        tui.dispatch_slash(
            tui.parse_slash("/pin b.md"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=history,
        )
        assert "alpha" in history[0].content
        assert "beta" in history[0].content
        # Marker only appears once even with two pinned files.
        assert history[0].content.count("--- pinned files ---") == 1

    def test_pin_path_escape(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        history: list[ChatMessage] = [ChatMessage(role="system", content="base")]
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/pin ../../etc/passwd"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=history,
        )
        assert "error" in text.lower()
        # System prompt must be unchanged on failure.
        assert history[0].content == "base"

    def test_unpin_clears_block(self, tmp_path: Path) -> None:
        (tmp_path / "a.md").write_text("alpha\n")
        cfg = fs_tools.FsConfig(root=tmp_path)
        history: list[ChatMessage] = [ChatMessage(role="system", content="base")]
        tui.dispatch_slash(
            tui.parse_slash("/pin a.md"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=history,
        )
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/unpin"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=history,
        )
        assert "cleared" in text
        assert history[0].content == "base"

    def test_unpin_when_nothing_pinned(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        history: list[ChatMessage] = [ChatMessage(role="system", content="base")]
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/unpin"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=history,
        )
        assert "nothing pinned" in text

    def test_pin_truncates_large_file(self, tmp_path: Path) -> None:
        (tmp_path / "big.txt").write_text("z" * 20000)
        cfg = fs_tools.FsConfig(root=tmp_path)
        history: list[ChatMessage] = [ChatMessage(role="system", content="base")]
        tui.dispatch_slash(
            tui.parse_slash("/pin big.txt"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=history,
        )
        assert "[truncated]" in history[0].content


# ----------------------------------------------------------- Loop 146
class TestLooksLikeMarkdown:
    def test_fenced_code(self) -> None:
        assert tui.looks_like_markdown("here:\n```python\nprint(1)\n```")

    def test_heading(self) -> None:
        assert tui.looks_like_markdown("# heading\n\nbody")

    def test_bullet_list(self) -> None:
        assert tui.looks_like_markdown("intro\n- one\n- two")

    def test_numbered_list(self) -> None:
        assert tui.looks_like_markdown("steps:\n1. first\n2. second")

    def test_blockquote(self) -> None:
        assert tui.looks_like_markdown("note:\n> careful")

    def test_bold(self) -> None:
        assert tui.looks_like_markdown("this is **important** text")

    def test_plain_short_text(self) -> None:
        assert not tui.looks_like_markdown("yes")

    def test_plain_paragraph(self) -> None:
        assert not tui.looks_like_markdown(
            "the answer is forty two because it is the convention"
        )

    def test_empty(self) -> None:
        assert not tui.looks_like_markdown("")


# ----------------------------------------------------------- Loop 147
class TestPinnedSlash:
    def test_lists_pinned_paths(self, tmp_path: Path) -> None:
        (tmp_path / "a.md").write_text("alpha\n")
        (tmp_path / "b.md").write_text("beta\n")
        cfg = fs_tools.FsConfig(root=tmp_path)
        history: list[ChatMessage] = [ChatMessage(role="system", content="base")]
        tui.dispatch_slash(
            tui.parse_slash("/pin a.md"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=history,
        )
        tui.dispatch_slash(
            tui.parse_slash("/pin b.md"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=history,
        )
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/pinned"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=history,
        )
        assert "a.md" in text
        assert "b.md" in text
        assert text.startswith("pinned files:")

    def test_nothing_pinned(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        history: list[ChatMessage] = [ChatMessage(role="system", content="base")]
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/pinned"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=history,
        )
        assert "nothing pinned" in text

    def test_after_unpin_reports_nothing(self, tmp_path: Path) -> None:
        (tmp_path / "a.md").write_text("alpha\n")
        cfg = fs_tools.FsConfig(root=tmp_path)
        history: list[ChatMessage] = [ChatMessage(role="system", content="base")]
        tui.dispatch_slash(
            tui.parse_slash("/pin a.md"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=history,
        )
        tui.dispatch_slash(
            tui.parse_slash("/unpin"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=history,
        )
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/pinned"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=history,
        )
        assert "nothing pinned" in text


# ----------------------------------------------------------- Loop 148
class TestHistoryClear:
    def test_clear_keeps_system_drops_others(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        history: list[ChatMessage] = [
            ChatMessage(role="system", content="base"),
            ChatMessage(role="user", content="hi"),
            ChatMessage(role="assistant", content="hello"),
        ]
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/history clear"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=history,
        )
        assert "history cleared" in text
        assert len(history) == 1
        assert history[0].role == "system"

    def test_clear_deletes_persistence_file(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        path = tui.history_file_path(cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"role":"user","content":"old"}\n')
        history: list[ChatMessage] = [ChatMessage(role="user", content="hi")]
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/history clear"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=history,
        )
        assert not path.exists()
        assert "deleted persistence file" in text

    def test_clear_when_no_persistence_file(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        history: list[ChatMessage] = [ChatMessage(role="user", content="hi")]
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/history clear"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=history,
        )
        assert "history cleared" in text
        assert "deleted persistence" not in text

    def test_history_n_still_works(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        history: list[ChatMessage] = [
            ChatMessage(role="user", content="hi"),
            ChatMessage(role="assistant", content="hello"),
        ]
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/history 5"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=history,
        )
        assert "hi" in text


# ----------------------------------------------------------- Loop 149
class TestOpenSlash:
    def test_path_escape_blocks_editor_launch(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/open ../../etc/passwd"),
            client=_FakeClient(),
            fs_cfg=cfg,
        )
        assert "open error" in text

    def test_usage_when_no_path(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/open"),
            client=_FakeClient(),
            fs_cfg=cfg,
        )
        assert text.startswith("usage:")

    def test_invokes_editor_with_resolved_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "spec.md"
        target.write_text("hi\n")
        cfg = fs_tools.FsConfig(root=tmp_path)
        captured: dict[str, list[str]] = {}

        class _Proc:
            returncode = 0

        def _fake_run(args, check):  # type: ignore[no-untyped-def]
            captured["args"] = list(args)
            return _Proc()

        monkeypatch.setenv("EDITOR", "myedit -w")
        monkeypatch.setattr("subprocess.run", _fake_run)
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/open spec.md"),
            client=_FakeClient(),
            fs_cfg=cfg,
        )
        assert "opened spec.md" in text
        assert captured["args"][0] == "myedit"
        assert captured["args"][1] == "-w"
        assert captured["args"][-1].endswith("spec.md")

    def test_editor_not_found_returns_friendly_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "a.md"
        target.write_text("hi\n")
        cfg = fs_tools.FsConfig(root=tmp_path)

        def _raise(args, check):  # type: ignore[no-untyped-def]
            raise FileNotFoundError(args[0])

        monkeypatch.setenv("EDITOR", "nope-not-real")
        monkeypatch.setattr("subprocess.run", _raise)
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/open a.md"),
            client=_FakeClient(),
            fs_cfg=cfg,
        )
        assert "editor not found" in text


# ----------------------------------------------------------- Loop 151
class TestCdSlash:
    def test_no_arg_shows_cwd(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/cd"), client=_FakeClient(), fs_cfg=cfg,
        )
        assert "(cwd)" in text
        assert str(tmp_path) in text

    def test_relative_subdir_returns_sentinel(self, tmp_path: Path) -> None:
        sub = tmp_path / "sub"
        sub.mkdir()
        cfg = fs_tools.FsConfig(root=tmp_path)
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/cd sub"), client=_FakeClient(), fs_cfg=cfg,
        )
        assert text.startswith(tui._CD_SENTINEL)
        assert str(sub.resolve()) in text

    def test_absolute_path_returns_sentinel(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        text, _ = tui.dispatch_slash(
            tui.parse_slash(f"/cd {tmp_path}"),
            client=_FakeClient(),
            fs_cfg=cfg,
        )
        assert text.startswith(tui._CD_SENTINEL)

    def test_missing_path_errors(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/cd nope"), client=_FakeClient(), fs_cfg=cfg,
        )
        assert "no such directory" in text

    def test_path_to_file_errors(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("hi\n")
        cfg = fs_tools.FsConfig(root=tmp_path)
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/cd a.txt"), client=_FakeClient(), fs_cfg=cfg,
        )
        assert "not a directory" in text


# ----------------------------------------------------------- Loop 152
class TestGrepTypeFilter:
    def test_split_extracts_suffix(self) -> None:
        positionals, suffix = tui._split_grep_flags(["TODO", "src", "--py"])
        assert positionals == ["TODO", "src"]
        assert suffix == "py"

    def test_split_no_suffix(self) -> None:
        positionals, suffix = tui._split_grep_flags(["TODO", "src"])
        assert positionals == ["TODO", "src"]
        assert suffix is None

    def test_split_only_pattern(self) -> None:
        positionals, suffix = tui._split_grep_flags(["TODO", "--md"])
        assert positionals == ["TODO"]
        assert suffix == "md"

    def test_grep_filters_to_suffix(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("# TODO python\n")
        (tmp_path / "b.md").write_text("TODO markdown\n")
        cfg = fs_tools.FsConfig(root=tmp_path)
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/grep TODO --py"),
            client=_FakeClient(),
            fs_cfg=cfg,
        )
        assert "a.py" in text
        assert "b.md" not in text

    def test_grep_md_filter(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("# TODO python\n")
        (tmp_path / "b.md").write_text("TODO markdown\n")
        cfg = fs_tools.FsConfig(root=tmp_path)
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/grep TODO --md"),
            client=_FakeClient(),
            fs_cfg=cfg,
        )
        assert "b.md" in text
        assert "a.py" not in text

    def test_grep_no_filter_keeps_both(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("# TODO python\n")
        (tmp_path / "b.md").write_text("TODO markdown\n")
        cfg = fs_tools.FsConfig(root=tmp_path)
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/grep TODO"),
            client=_FakeClient(),
            fs_cfg=cfg,
        )
        assert "a.py" in text
        assert "b.md" in text

    def test_only_flag_without_pattern_is_usage_error(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/grep --py"),
            client=_FakeClient(),
            fs_cfg=cfg,
        )
        assert text.startswith("usage:")


# ----------------------------------------------------------- Loop 153
class TestPinMultiFile:
    def test_pin_three_files_one_call(self, tmp_path: Path) -> None:
        for n, body in [("a.py", "alpha"), ("b.md", "beta"), ("c.txt", "gamma")]:
            (tmp_path / n).write_text(body + "\n")
        cfg = fs_tools.FsConfig(root=tmp_path)
        history: list[ChatMessage] = [ChatMessage(role="system", content="base")]
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/pin a.py b.md c.txt"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=history,
        )
        assert "pinned a.py" in text
        assert "pinned b.md" in text
        assert "pinned c.txt" in text
        assert "alpha" in history[0].content
        assert "beta" in history[0].content
        assert "gamma" in history[0].content
        assert history[0].content.count("--- pinned files ---") == 1

    def test_pin_partial_failure(self, tmp_path: Path) -> None:
        (tmp_path / "good.py").write_text("ok\n")
        cfg = fs_tools.FsConfig(root=tmp_path)
        history: list[ChatMessage] = [ChatMessage(role="system", content="base")]
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/pin good.py ../../etc/passwd"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=history,
        )
        assert "pinned good.py" in text
        assert "pin error" in text
        assert "ok" in history[0].content

    def test_pin_no_args_usage(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        history: list[ChatMessage] = [ChatMessage(role="system", content="base")]
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/pin"),
            client=_FakeClient(),
            fs_cfg=cfg,
            history=history,
        )
        assert text.startswith("usage:")
