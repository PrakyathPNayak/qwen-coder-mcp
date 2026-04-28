"""Loop 209 — `/help <pattern> --regex` adds a regex escape hatch
to the help filter. Plain substring search is still the default."""
from __future__ import annotations

from pathlib import Path

import pytest

from qwen_coder_mcp import fs_tools, tui


@pytest.fixture()
def fs_cfg(tmp_path: Path) -> fs_tools.FsConfig:
    return fs_tools.FsConfig(root=tmp_path)


def _help(line: str, fs_cfg: fs_tools.FsConfig) -> str:
    text, _ = tui.dispatch_slash(
        tui.parse_slash(line),
        client=None,  # type: ignore[arg-type]
        fs_cfg=fs_cfg,
    )
    return text


class TestRegexBasic:
    def test_alternation_finds_two_commands(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        out = _help("/help tokens|lat --regex", fs_cfg)
        assert "/tokens" in out
        assert "/lat" in out

    def test_anchor_start(self, fs_cfg: fs_tools.FsConfig) -> None:
        # ^/check matches commands starting with /check at line start.
        # The help text indents commands with two spaces, so anchor at
        # whitespace + slash.
        out = _help("/help ^\\s+/check --regex", fs_cfg)
        # Must include /checkpoints (and possibly other /check* commands).
        assert "/checkpoints" in out

    def test_case_insensitive(self, fs_cfg: fs_tools.FsConfig) -> None:
        out = _help("/help TOKENS --regex", fs_cfg)
        assert "/tokens" in out

    def test_label_says_regex(self, fs_cfg: fs_tools.FsConfig) -> None:
        out = _help("/help tokens --regex", fs_cfg)
        # First line should announce the regex mode.
        first = out.splitlines()[0]
        assert "regex" in first.lower()


class TestRegexErrors:
    def test_invalid_regex_rejected(self, fs_cfg: fs_tools.FsConfig) -> None:
        # Unbalanced ( is a syntax error.
        out = _help("/help foo( --regex", fs_cfg)
        assert "invalid regex" in out

    def test_no_match_message(self, fs_cfg: fs_tools.FsConfig) -> None:
        out = _help("/help xyzzyqwerty --regex", fs_cfg)
        assert "no commands match" in out


class TestRegexFlagPosition:
    def test_flag_before_pattern(self, fs_cfg: fs_tools.FsConfig) -> None:
        out = _help("/help --regex tokens", fs_cfg)
        assert "/tokens" in out

    def test_flag_after_pattern(self, fs_cfg: fs_tools.FsConfig) -> None:
        out = _help("/help tokens --regex", fs_cfg)
        assert "/tokens" in out


class TestBackwardsCompat:
    def test_plain_term_still_substring(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        out = _help("/help tokens", fs_cfg)
        assert "/tokens" in out
        first = out.splitlines()[0]
        # Plain mode must NOT advertise regex.
        assert "regex" not in first.lower()

    def test_no_args_returns_full_help(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        out = _help("/help", fs_cfg)
        assert out == tui.HELP_TEXT

    def test_plain_substring_with_special_char_unchanged(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        # Without --regex, parens are literal substring chars and won't
        # raise. They simply won't match anything.
        out = _help("/help foo(", fs_cfg)
        assert "no commands match" in out
