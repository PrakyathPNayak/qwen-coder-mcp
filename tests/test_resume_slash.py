"""Tests for the ``/resume`` slash command — reloads
``.agent/agent_state.json`` into the live chat history list."""
from __future__ import annotations

from pathlib import Path

from qwen_coder_mcp import agent_loop, fs_tools
from qwen_coder_mcp.qwen_client import ChatMessage
from qwen_coder_mcp.tui import (
    SLASH_COMMANDS,
    SlashCommand,
    dispatch_slash,
    parse_slash,
    slash_completions,
)


def _cfg(tmp_path: Path) -> fs_tools.FsConfig:
    return fs_tools.FsConfig(root=tmp_path)


class _NullClient:
    def chat(self, _h: list[ChatMessage]) -> str:
        return ""


class TestResumeRegistration:
    def test_registered_in_slash_commands(self) -> None:
        assert "/resume" in SLASH_COMMANDS

    def test_tab_completion_finds_resume(self) -> None:
        assert "/resume" in slash_completions("/res")

    def test_parse_slash_recognises_resume(self) -> None:
        cmd = parse_slash("/resume")
        assert cmd is not None and cmd.name == "resume"


class TestResumeBehaviour:
    def test_resume_with_no_checkpoint_reports_missing(
        self, tmp_path: Path
    ) -> None:
        history: list[ChatMessage] = []
        out, quit_ = dispatch_slash(
            SlashCommand(name="resume"),
            client=_NullClient(),
            fs_cfg=_cfg(tmp_path),
            history=history,
        )
        assert quit_ is False
        assert "no checkpoint" in out
        assert history == []

    def test_resume_loads_checkpoint_into_history(
        self, tmp_path: Path
    ) -> None:
        # Seed an on-disk checkpoint exactly the way run_agent would.
        target = tmp_path / ".agent" / "agent_state.json"
        seed = [
            ChatMessage(role="user", content="hi"),
            ChatMessage(role="assistant", content="hello back"),
        ]
        agent_loop.save_agent_checkpoint(target, seed)

        history: list[ChatMessage] = [ChatMessage(role="user", content="stale")]
        out, quit_ = dispatch_slash(
            SlashCommand(name="resume"),
            client=_NullClient(),
            fs_cfg=_cfg(tmp_path),
            history=history,
        )
        assert quit_ is False
        assert [(m.role, m.content) for m in history] == [
            ("user", "hi"),
            ("assistant", "hello back"),
        ]
        # Status string mentions the count and last assistant snippet.
        assert "resumed 2 messages" in out
        assert "hello back" in out

    def test_resume_in_place_mutation(self, tmp_path: Path) -> None:
        # Verify the function clears + extends rather than rebinding —
        # the TUI App holds a reference to the same list.
        target = tmp_path / ".agent" / "agent_state.json"
        agent_loop.save_agent_checkpoint(
            target,
            [ChatMessage(role="user", content="loaded")],
        )
        history: list[ChatMessage] = [ChatMessage(role="user", content="old")]
        ref = history
        dispatch_slash(
            SlashCommand(name="resume"),
            client=_NullClient(),
            fs_cfg=_cfg(tmp_path),
            history=history,
        )
        assert ref is history
        assert len(history) == 1 and history[0].content == "loaded"

    def test_resume_corrupt_file_reports_missing(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / ".agent" / "agent_state.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("not json {{{", encoding="utf-8")
        history: list[ChatMessage] = []
        out, _ = dispatch_slash(
            SlashCommand(name="resume"),
            client=_NullClient(),
            fs_cfg=_cfg(tmp_path),
            history=history,
        )
        assert "no checkpoint" in out
        assert history == []

    def test_resume_no_assistant_message_omits_snippet(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / ".agent" / "agent_state.json"
        agent_loop.save_agent_checkpoint(
            target,
            [ChatMessage(role="user", content="just user msgs")],
        )
        out, _ = dispatch_slash(
            SlashCommand(name="resume"),
            client=_NullClient(),
            fs_cfg=_cfg(tmp_path),
            history=[],
        )
        assert "resumed 1 messages" in out
        assert "last assistant" not in out
