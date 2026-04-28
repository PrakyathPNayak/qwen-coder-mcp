"""Loop 203 — `format_history_diff` derives preview width from terminal."""
from __future__ import annotations

from qwen_coder_mcp.tui import ChatMessage, format_history_diff


def _msg(role: str, content: str) -> ChatMessage:
    return ChatMessage(role=role, content=content)


LONG = "lorem ipsum dolor sit amet " * 30  # well over 200 chars


class TestPreviewAuto:
    def test_explicit_none_uses_terminal(self, monkeypatch) -> None:
        import shutil

        monkeypatch.setattr(
            shutil,
            "get_terminal_size",
            lambda default=(80, 24): shutil.os.terminal_size((200, 30)),
        )
        a = [_msg("user", LONG)]
        b = [_msg("user", LONG[:-5])]  # forces ~ row
        out = format_history_diff(a, b, preview_chars=None)
        # 200-col terminal → preview ~172 chars; far above 60.
        # Find the row content between "(" and "…"/")".
        row = next(line for line in out.splitlines() if line.startswith("~"))
        # Should contain a substantially longer slice than the 60-default.
        assert len(row) > 100

    def test_none_clamped_low_on_narrow_terminal(self, monkeypatch) -> None:
        import shutil

        monkeypatch.setattr(
            shutil,
            "get_terminal_size",
            lambda default=(80, 24): shutil.os.terminal_size((30, 24)),
        )
        a = [_msg("user", LONG)]
        b = [_msg("user", LONG[:-5])]
        out = format_history_diff(a, b, preview_chars=None)
        row = next(line for line in out.splitlines() if line.startswith("~"))
        # Floored at 20 — preview can't shrink below that even on
        # absurd narrow terminals.
        # The full row has the row prefix + parens + preview, so length
        # is bounded by ~50.
        assert "…" in row  # truncated

    def test_explicit_int_still_works(self) -> None:
        a = [_msg("user", LONG)]
        b = [_msg("user", LONG[:-5])]
        out = format_history_diff(a, b, preview_chars=40)
        row = next(line for line in out.splitlines() if line.startswith("~"))
        # Preview is "(" + ≤40 chars + "…)".
        assert "…" in row

    def test_default_60_unchanged(self) -> None:
        # Backwards compat: not specifying the kwarg should still give
        # the historical 60-char default, not the auto-mode.
        a = [_msg("user", LONG)]
        b = [_msg("user", LONG[:-5])]
        out = format_history_diff(a, b)
        row = next(line for line in out.splitlines() if line.startswith("~"))
        # Match the inside of the parens.
        import re

        m = re.search(r"\((.*?)\)", row)
        assert m is not None
        # Roughly 60 chars (with ellipsis on overflow).
        assert 50 <= len(m.group(1)) <= 65

    def test_none_does_not_break_short_messages(self, monkeypatch) -> None:
        import shutil

        monkeypatch.setattr(
            shutil,
            "get_terminal_size",
            lambda default=(80, 24): shutil.os.terminal_size((120, 24)),
        )
        a = [_msg("user", "hi")]
        b = [_msg("user", "hi")]
        out = format_history_diff(a, b, preview_chars=None)
        # Short message renders identically — auto-width has no effect.
        assert "(hi)" in out
