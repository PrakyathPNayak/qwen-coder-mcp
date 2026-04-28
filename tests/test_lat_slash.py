"""Tests for the ``/lat`` slash command and the ``TurnProfile``
renderer that backs it (loop 182). The renderer is pure so most
coverage lives in ``TestFormatTurnProfile``; the dispatcher branch
is exercised in ``TestLatDispatch``."""
from __future__ import annotations

from pathlib import Path

import pytest

from qwen_coder_mcp import fs_tools
from qwen_coder_mcp.tui import (
    HELP_TEXT,
    SLASH_COMMANDS,
    TurnProfile,
    dispatch_slash,
    format_turn_profile,
    parse_slash,
)


@pytest.fixture()
def fs_cfg(tmp_path: Path) -> fs_tools.FsConfig:
    return fs_tools.FsConfig(root=tmp_path)


class TestFormatTurnProfile:
    def test_none_returns_placeholder(self) -> None:
        assert "no agent turn" in format_turn_profile(None)

    def test_renders_total_and_ttft(self) -> None:
        prof = TurnProfile(
            started_at=100.0,
            ended_at=102.5,
            ttft_s=0.4,
        )
        out = format_turn_profile(prof)
        assert "total:" in out
        assert "first token:" in out
        assert "2.5s" in out
        assert "400ms" in out

    def test_no_tools_renders_placeholder_row(self) -> None:
        prof = TurnProfile(started_at=0.0, ended_at=1.0)
        out = format_turn_profile(prof)
        assert "tools (0)" in out
        assert "no tool calls" in out

    def test_tool_calls_numbered_and_aligned(self) -> None:
        prof = TurnProfile(
            started_at=0.0,
            ended_at=2.0,
            tool_calls=[
                ("fs_read", 0.012),
                ("fs_grep", 0.045),
                ("shell_run", 1.8),
            ],
        )
        out = format_turn_profile(prof)
        assert "tools (3)" in out
        # Index column is 1-based.
        assert "1. fs_read" in out
        assert "2. fs_grep" in out
        assert "3. shell_run" in out
        assert "12ms" in out
        assert "45ms" in out
        assert "1.8s" in out

    def test_tool_call_with_unknown_latency(self) -> None:
        prof = TurnProfile(
            started_at=0.0,
            ended_at=1.0,
            tool_calls=[("fs_read", None)],
        )
        out = format_turn_profile(prof)
        assert "(?)" in out

    def test_summary_line_included(self) -> None:
        prof = TurnProfile(
            started_at=0.0,
            ended_at=2.0,
            summary_text="2 tool calls, 0.057s total",
            summary_total_s=0.057,
        )
        out = format_turn_profile(prof)
        assert "summary: 2 tool calls" in out

    def test_total_omitted_when_unfinished(self) -> None:
        # ended_at=None => total_s() returns None => no total: line
        prof = TurnProfile(started_at=10.0, ended_at=None)
        out = format_turn_profile(prof)
        assert "total:" not in out


class TestLatDispatch:
    def test_lat_without_app_says_no_turn(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        out, quit_now = dispatch_slash(
            parse_slash("/lat"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[],
        )
        assert quit_now is False
        assert "no agent turn" in out

    def test_lat_reads_app_last_turn_profile(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        class _StubApp:
            last_turn_profile = TurnProfile(
                started_at=0.0,
                ended_at=1.5,
                ttft_s=0.3,
                tool_calls=[("fs_read", 0.02)],
                summary_text="1 tool calls, 0.020s total",
            )

        out, _ = dispatch_slash(
            parse_slash("/lat"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[],
            app=_StubApp(),
        )
        assert "1.5s" in out
        assert "300ms" in out
        assert "fs_read" in out
        assert "summary:" in out

    def test_lat_app_with_no_attribute(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        # An object without last_turn_profile -> getattr default -> None.
        class _Bare:
            pass

        out, _ = dispatch_slash(
            parse_slash("/lat"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[],
            app=_Bare(),
        )
        assert "no agent turn" in out


class TestRegistryWiring:
    def test_command_registered(self) -> None:
        assert "/lat" in SLASH_COMMANDS

    def test_command_in_help(self) -> None:
        assert "/lat" in HELP_TEXT
