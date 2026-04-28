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
        assert "usage:" in text

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
