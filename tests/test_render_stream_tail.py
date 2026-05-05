"""Unit tests for ``render_stream_tail`` — the helper that snaps the
streaming widget's visible tail to a whitespace boundary so the head
of the tail doesn't flicker mid-word as more chunks arrive.
"""
from __future__ import annotations

from qwen_coder_mcp.tui import render_stream_tail, sanitize_live_stream_accum


class TestRenderStreamTail:
    def test_short_string_returned_unchanged(self) -> None:
        assert render_stream_tail("hello world", budget=2000) == "hello world"

    def test_zero_budget_returns_empty(self) -> None:
        assert render_stream_tail("anything at all", budget=0) == ""

    def test_negative_budget_returns_empty(self) -> None:
        assert render_stream_tail("anything at all", budget=-5) == ""

    def test_exact_budget_boundary_returns_full_string(self) -> None:
        s = "x" * 2000
        assert render_stream_tail(s, budget=2000) == s

    def test_snaps_forward_to_next_whitespace(self) -> None:
        head = "leading garbage that we want clipped off the front"
        tail_text = " then a clean sentence continues here."
        accum = head + tail_text
        # Choose a budget that lands the cut inside "garbage"
        budget = len(tail_text) + 5
        out = render_stream_tail(accum, budget=budget)
        # Output must start cleanly after a whitespace character — the
        # first character should be a non-space token.
        assert not out.startswith(" ")
        # And it must be a suffix of accum.
        assert accum.endswith(out)
        # And it must be no smaller than budget - 64 (the snap window).
        assert len(out) >= budget - 64

    def test_no_whitespace_in_window_falls_back_to_raw_cut(self) -> None:
        # 4000 chars of solid hex with no whitespace: snap window finds
        # nothing, so we keep the raw cut rather than collapse.
        accum = "a" * 4000
        out = render_stream_tail(accum, budget=2000)
        assert out == "a" * 2000

    def test_snap_window_is_bounded(self) -> None:
        # If the next whitespace is well past the 64-char snap window,
        # we should NOT walk past it — the tail stays at roughly the
        # raw cut.
        head = "x" * 1000
        no_space_run = "y" * 200  # no whitespace in this run
        rest = " z" * 100
        accum = head + no_space_run + rest
        out = render_stream_tail(accum, budget=len(rest) + 100)
        # Snap window is 64; the next space sits 100 chars in, so we
        # fall back to the raw cut. Output length stays close to budget.
        assert len(out) >= len(rest) + 100 - 64


class TestSanitizeLiveStreamAccum:
    def test_passes_plain_answer(self) -> None:
        assert sanitize_live_stream_accum("plain answer") == "plain answer"

    def test_hides_unwrapped_reasoning_until_close(self) -> None:
        raw = "The user wants code. I should think through edge cases."
        assert sanitize_live_stream_accum(raw) == ""

    def test_shows_content_after_unwrapped_think_close(self) -> None:
        raw = "The user wants code.</think>\n\n```python\nprint('ok')\n```"
        assert sanitize_live_stream_accum(raw).startswith("```python")

    def test_shows_code_fence_if_reasoning_never_closes(self) -> None:
        raw = (
            "Here's a thinking process:\n1. analyze\n\n"
            "```python\nprint('ok')\n```"
        )
        assert sanitize_live_stream_accum(raw).startswith("```python")

    def test_shows_tool_call_if_reasoning_never_closes(self) -> None:
        raw = (
            "The user wants me to write a file.\n"
            '<tool_call>{"name":"fs_list","args":{}}</tool_call>'
        )
        assert sanitize_live_stream_accum(raw).startswith("<tool_call>")
