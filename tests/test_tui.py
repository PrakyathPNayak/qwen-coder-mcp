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
