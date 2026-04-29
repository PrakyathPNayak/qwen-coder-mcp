"""Loop 280: /mouse and /select slash commands.

Toggling Textual's mouse capture is what lets the host terminal do
native click-drag selection on the response RichLog. The slash command
itself is pure: it emits a sentinel-prefixed string that the App routes
to escape-code emission. We test the dispatcher-level contract here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from qwen_coder_mcp import fs_tools, tui
from qwen_coder_mcp.tui import (
    _AGENT_TOGGLE_SENTINEL,
    SLASH_COMMANDS,
    SlashCommand,
    dispatch_slash,
    HELP_TEXT,
)


@pytest.fixture
def fs_cfg(tmp_path: Path) -> fs_tools.FsConfig:
    return fs_tools.FsConfig(root=tmp_path)


def _dispatch(rest: str, fs_cfg: fs_tools.FsConfig) -> str:
    text, quit_ = dispatch_slash(
        SlashCommand(name="mouse", args=tuple(rest.split()) if rest else (), rest=rest),
        client=None,
        fs_cfg=fs_cfg,
    )
    assert quit_ is False
    return text


def test_mouse_in_slash_commands_tuple() -> None:
    assert "/mouse" in SLASH_COMMANDS
    assert "/select" in SLASH_COMMANDS


def test_mouse_help_text_documents_command() -> None:
    assert "/mouse" in HELP_TEXT
    assert "/select" in HELP_TEXT


def test_mouse_no_arg_returns_toggle_sentinel(fs_cfg: fs_tools.FsConfig) -> None:
    text = _dispatch("", fs_cfg)
    assert text == _AGENT_TOGGLE_SENTINEL + "mouse_toggle"


def test_mouse_toggle_explicit(fs_cfg: fs_tools.FsConfig) -> None:
    text = _dispatch("toggle", fs_cfg)
    assert text == _AGENT_TOGGLE_SENTINEL + "mouse_toggle"


def test_mouse_off(fs_cfg: fs_tools.FsConfig) -> None:
    text = _dispatch("off", fs_cfg)
    assert text == _AGENT_TOGGLE_SENTINEL + "mouse_off"


def test_mouse_on(fs_cfg: fs_tools.FsConfig) -> None:
    text = _dispatch("on", fs_cfg)
    assert text == _AGENT_TOGGLE_SENTINEL + "mouse_on"


def test_mouse_off_synonyms(fs_cfg: fs_tools.FsConfig) -> None:
    for syn in ("0", "false", "release", "OFF", "Off"):
        text = _dispatch(syn, fs_cfg)
        assert text == _AGENT_TOGGLE_SENTINEL + "mouse_off", syn


def test_mouse_on_synonyms(fs_cfg: fs_tools.FsConfig) -> None:
    for syn in ("1", "true", "capture", "ON", "On"):
        text = _dispatch(syn, fs_cfg)
        assert text == _AGENT_TOGGLE_SENTINEL + "mouse_on", syn


def test_mouse_unknown_arg_returns_usage(fs_cfg: fs_tools.FsConfig) -> None:
    text = _dispatch("wat", fs_cfg)
    assert "usage" in text.lower()
    assert "/mouse" in text


def test_select_alias_returns_mouse_off(fs_cfg: fs_tools.FsConfig) -> None:
    text, quit_ = dispatch_slash(
        SlashCommand(name="select", args=(), rest=""),
        client=None,
        fs_cfg=fs_cfg,
    )
    assert quit_ is False
    assert text == _AGENT_TOGGLE_SENTINEL + "mouse_off"
