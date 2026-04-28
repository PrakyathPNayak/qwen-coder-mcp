"""Tests for ``/agent --resume`` (loop 186) — the flag that pre-loads
the latest agent checkpoint into chat history before starting the
agent turn. Coverage focuses on the parser (`/agent --resume`),
the wire-format encoder, and the decoder's three-tuple return."""
from __future__ import annotations

from pathlib import Path

import pytest

from qwen_coder_mcp import fs_tools, tui


class _FakeClient:
    pass


@pytest.fixture()
def fs_cfg(tmp_path: Path) -> fs_tools.FsConfig:
    return fs_tools.FsConfig(root=tmp_path)


class TestDecodeAgentBody:
    def test_no_flags_resume_false(self) -> None:
        task, n, resume = tui._decode_agent_body("plain task")
        assert task == "plain task" and n is None and resume is False

    def test_max_only(self) -> None:
        task, n, resume = tui._decode_agent_body("--max=7\nrun")
        assert task == "run" and n == 7 and resume is False

    def test_resume_only(self) -> None:
        task, n, resume = tui._decode_agent_body("--resume\nrun")
        assert task == "run" and n is None and resume is True

    def test_both_flags_max_first(self) -> None:
        task, n, resume = tui._decode_agent_body("--max=5\n--resume\nrun")
        assert task == "run" and n == 5 and resume is True

    def test_both_flags_resume_first(self) -> None:
        task, n, resume = tui._decode_agent_body("--resume\n--max=5\nrun")
        assert task == "run" and n == 5 and resume is True

    def test_unparseable_max_returns_body_unchanged(self) -> None:
        # Unparseable --max bails — task is the original body so the
        # user's text isn't silently lost.
        task, n, resume = tui._decode_agent_body("--max=oops\nrun")
        assert "--max=oops" in task and n is None


class TestAgentResumeFlag:
    def test_resume_alone(self, fs_cfg: fs_tools.FsConfig) -> None:
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/agent --resume continue work"),
            client=_FakeClient(),
            fs_cfg=fs_cfg,
        )
        assert text.startswith(tui._AGENT_SENTINEL)
        body = text[len(tui._AGENT_SENTINEL):]
        task, n, resume = tui._decode_agent_body(body)
        assert resume is True and n is None and task == "continue work"

    def test_resume_with_write(self, fs_cfg: fs_tools.FsConfig) -> None:
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/agent --write --resume edit it"),
            client=_FakeClient(),
            fs_cfg=fs_cfg,
        )
        assert text.startswith(tui._AGENT_WRITE_SENTINEL)
        body = text[len(tui._AGENT_WRITE_SENTINEL):]
        task, n, resume = tui._decode_agent_body(body)
        assert resume is True and task == "edit it"

    def test_resume_with_max(self, fs_cfg: fs_tools.FsConfig) -> None:
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/agent --resume --max 10 step"),
            client=_FakeClient(),
            fs_cfg=fs_cfg,
        )
        body = text[len(tui._AGENT_SENTINEL):]
        task, n, resume = tui._decode_agent_body(body)
        assert resume is True and n == 10 and task == "step"

    def test_resume_with_write_and_max(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/agent --write --max 8 --resume task"),
            client=_FakeClient(),
            fs_cfg=fs_cfg,
        )
        assert text.startswith(tui._AGENT_WRITE_SENTINEL)
        body = text[len(tui._AGENT_WRITE_SENTINEL):]
        task, n, resume = tui._decode_agent_body(body)
        assert resume is True and n == 8 and task == "task"

    def test_resume_without_task_rejected(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/agent --resume"),
            client=_FakeClient(),
            fs_cfg=fs_cfg,
        )
        assert "usage:" in text

    def test_help_advertises_resume(self) -> None:
        assert "--resume" in tui.HELP_TEXT
