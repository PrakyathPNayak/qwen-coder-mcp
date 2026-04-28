"""Loop 191 — ``/help <term>`` substring filter over the help table."""
from __future__ import annotations

from pathlib import Path

import pytest

from qwen_coder_mcp import fs_tools
from qwen_coder_mcp.tui import HELP_TEXT, dispatch_slash, parse_slash


@pytest.fixture()
def fs_cfg(tmp_path: Path) -> fs_tools.FsConfig:
    return fs_tools.FsConfig(root=tmp_path)


def _help(term: str, fs_cfg: fs_tools.FsConfig) -> str:
    out, done = dispatch_slash(
        parse_slash(f"/help {term}" if term else "/help"),
        client=None,  # type: ignore[arg-type]
        fs_cfg=fs_cfg,
        history=[],
    )
    assert done is False
    return out


def test_bare_help_unchanged(fs_cfg: fs_tools.FsConfig) -> None:
    assert _help("", fs_cfg) == HELP_TEXT


def test_filter_matches_command_name(fs_cfg: fs_tools.FsConfig) -> None:
    out = _help("agent", fs_cfg)
    assert "/agent" in out
    # /search shouldn't survive an "agent" filter.
    assert "/search" not in out
    # Nor should /quit or /help itself.
    assert "/quit" not in out


def test_filter_is_case_insensitive(fs_cfg: fs_tools.FsConfig) -> None:
    a = _help("AGENT", fs_cfg)
    b = _help("agent", fs_cfg)
    assert a == b


def test_filter_matches_summary_text(fs_cfg: fs_tools.FsConfig) -> None:
    # "DuckDuckGo" appears only in the /search row's summary.
    out = _help("duckduckgo", fs_cfg)
    assert "/search" in out
    assert "/agent" not in out


def test_filter_keeps_continuation_lines(
    fs_cfg: fs_tools.FsConfig,
) -> None:
    # /grep has a wrapped two-line entry; matching the first line
    # should pull the continuation line through too.
    out = _help("grep", fs_cfg)
    assert "/grep" in out
    assert "filters by suffix" in out


def test_filter_no_match(fs_cfg: fs_tools.FsConfig) -> None:
    out = _help("zzzzznotacmd", fs_cfg)
    assert "no commands match" in out
    assert "zzzzznotacmd" in out


def test_filter_is_substring_not_regex(
    fs_cfg: fs_tools.FsConfig,
) -> None:
    # Regex metacharacters should be treated literally.
    out = _help(".*", fs_cfg)
    assert "no commands match" in out


def test_filter_with_multi_word_term(
    fs_cfg: fs_tools.FsConfig,
) -> None:
    # Multi-word terms are joined with single spaces.
    out = _help("system prompt", fs_cfg)
    assert "/sysprompt" in out


def test_header_preserved_on_match(
    fs_cfg: fs_tools.FsConfig,
) -> None:
    out = _help("agent", fs_cfg)
    assert "Slash commands matching 'agent':" in out


def test_filter_matches_unique_command(
    fs_cfg: fs_tools.FsConfig,
) -> None:
    out = _help("/quit", fs_cfg)
    # /quit is a single one-line entry; every other entry should have
    # been filtered out.
    assert out.count("\n  /") == 1
