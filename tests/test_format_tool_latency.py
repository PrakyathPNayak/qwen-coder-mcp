"""Tests for ``format_tool_latency`` — the per-tool elapsed-time
formatter shown next to ``← <tool>`` lines in the agent transcript."""
from __future__ import annotations

from qwen_coder_mcp.tui import format_tool_latency


class TestFormatToolLatency:
    def test_sub_millisecond_renders_as_zero_ms(self) -> None:
        assert format_tool_latency(0.0) == "(0ms)"

    def test_sub_second_renders_in_milliseconds(self) -> None:
        assert format_tool_latency(0.123) == "(123ms)"

    def test_just_under_one_second_still_ms(self) -> None:
        assert format_tool_latency(0.999) == "(999ms)"

    def test_one_second_flips_to_seconds(self) -> None:
        assert format_tool_latency(1.0) == "(1.0s)"

    def test_seconds_carry_one_decimal(self) -> None:
        assert format_tool_latency(2.45) == "(2.5s)"

    def test_just_under_a_minute_still_seconds(self) -> None:
        assert format_tool_latency(59.4) == "(59.4s)"

    def test_one_minute_flips_to_mmss(self) -> None:
        assert format_tool_latency(60.0) == "(1m00s)"

    def test_minute_format_pads_seconds(self) -> None:
        assert format_tool_latency(64.7) == "(1m04s)"

    def test_multi_minute_format(self) -> None:
        assert format_tool_latency(125.0) == "(2m05s)"

    def test_negative_returns_question_mark(self) -> None:
        assert format_tool_latency(-0.1) == "(?)"
