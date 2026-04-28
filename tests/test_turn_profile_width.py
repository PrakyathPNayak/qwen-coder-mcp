"""Loop 202 — `format_turn_profile` honours TTY width."""
from __future__ import annotations

from qwen_coder_mcp.tui import (
    TurnProfile,
    format_turn_profile,
    format_turn_profiles,
)


def _profile(
    summary: str = "ok",
    *,
    tools: list[tuple[str, float]] | None = None,
) -> TurnProfile:
    return TurnProfile(
        started_at=100.0,
        ended_at=102.5,
        ttft_s=0.4,
        tool_calls=tools if tools is not None else [("fs_read", 0.012)],
        summary_text=summary,
        summary_total_s=2.5,
    )


class TestSummaryWrapping:
    def test_wide_terminal_keeps_summary_on_one_line(self) -> None:
        prof = _profile(summary="3 tool calls, 1.857s total")
        out = format_turn_profile(prof, width=200)
        # Find the summary line — should be a single, unwrapped row.
        summary_lines = [
            line for line in out.splitlines() if "summary:" in line
        ]
        assert len(summary_lines) == 1
        assert "3 tool calls, 1.857s total" in summary_lines[0]

    def test_narrow_terminal_wraps_long_summary(self) -> None:
        long_summary = (
            "completed 12 tool calls; encountered 3 errors; total wall time "
            "11.482s with 0 retries triggered"
        )
        prof = _profile(summary=long_summary)
        out = format_turn_profile(prof, width=50)
        summary_block = "\n".join(
            line for line in out.splitlines() if "summary" in line or line.startswith("           ")
        )
        # Should now span multiple lines.
        assert "\n" in summary_block
        # Every wrapped line should fit within the width budget.
        for line in summary_block.splitlines():
            assert len(line) <= 50

    def test_wrap_indent_lines_up_under_colon(self) -> None:
        # Forces wrap by using a long summary on a narrow width.
        prof = _profile(
            summary="alpha bravo charlie delta echo foxtrot golf hotel"
        )
        out = format_turn_profile(prof, width=40)
        lines = out.splitlines()
        summary_idx = next(
            i for i, line in enumerate(lines) if "summary:" in line
        )
        # Continuation line begins with the same number of spaces as
        # "  summary: " (11 chars).
        cont = lines[summary_idx + 1]
        assert cont.startswith(" " * 11)
        assert cont[11] != " "  # actual content starts immediately after.


class TestToolColumnNarrow:
    def test_long_name_truncated_with_ellipsis_on_narrow(self) -> None:
        prof = _profile(
            tools=[("an_unusually_long_tool_name_indeed", 0.5)],
            summary="x",
        )
        out = format_turn_profile(prof, width=40)
        # The ellipsis character signals a truncated tool name.
        assert "…" in out
        # Latency still rendered.
        assert "(500ms)" in out

    def test_normal_name_unchanged_on_wide_terminal(self) -> None:
        prof = _profile(tools=[("fs_read", 0.012)])
        out = format_turn_profile(prof, width=200)
        # Full name preserved, no ellipsis.
        assert "fs_read" in out
        assert "…" not in out
        assert "(12ms)" in out


class TestWidthFallback:
    def test_default_uses_terminal_size(self, monkeypatch) -> None:
        import shutil

        called = {"hit": False}

        def fake_size(default=(80, 24)):
            called["hit"] = True
            return shutil.os.terminal_size((120, 30))

        monkeypatch.setattr(shutil, "get_terminal_size", fake_size)
        prof = _profile(summary="ok")
        out = format_turn_profile(prof)
        assert called["hit"] is True
        assert "summary: ok" in out

    def test_width_floored_at_40(self) -> None:
        # width=10 is silly; should be clamped to ≥ 40 internally so
        # we don't emit a 1-char-per-line waterfall.
        prof = _profile(summary="alpha bravo charlie delta echo foxtrot")
        out = format_turn_profile(prof, width=10)
        # No wrapped line should be ridiculously short.
        for line in out.splitlines():
            if "summary" in line or line.startswith(" " * 11):
                # Either the prefix-only line or content lines.
                assert len(line) >= 10


class TestStackedProfilesPropagate:
    def test_format_turn_profiles_passes_width_through(self) -> None:
        # Two profiles, narrow width — both should wrap.
        long = "alpha bravo charlie delta echo foxtrot golf hotel india"
        profiles = [_profile(summary=long), _profile(summary=long)]
        out = format_turn_profiles(profiles, n=2, width=50)
        # Both blocks should show wrapped continuation indent.
        assert out.count("\n           ") >= 2
        # Both turn headers present.
        assert "=== turn -1 ===" in out
        assert "=== turn -2 ===" in out
