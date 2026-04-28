"""Pin the TUI startup banner's engine-probe line.

Loop 220: the existing TUI startup banner uses
``QwenClient.health_check()`` which hits ``/v1/models`` and only
catches API-server-down or auth-bad. It misses the loops 211 / 216
bug class where the API server is up but the engine itself failed
to initialise. Loop 220 surfaces ``vllm_health_probe()`` (loop 215)
as an optional second line under the API check.

The rendering logic was extracted to ``format_engine_probe_lines``
specifically so it can be pinned without instantiating the full
Textual App.
"""
from __future__ import annotations

import pytest

from qwen_coder_mcp.tui import format_engine_probe_lines


class TestFormatEngineProbeLines:
    def test_none_probe_returns_no_lines(self) -> None:
        assert format_engine_probe_lines(None) == []

    def test_empty_dict_returns_no_lines(self) -> None:
        # Treated as falsy -- no engine info to surface.
        assert format_engine_probe_lines({}) == []

    def test_ok_probe_is_silent(self) -> None:
        # Happy path: API banner already said everything's fine; we
        # don't echo it from the engine probe.
        assert format_engine_probe_lines({"ok": True, "status": 200}) == []

    def test_engine_503_warming_up(self) -> None:
        out = format_engine_probe_lines(
            {"ok": False, "status": 503, "hint": "still initialising"}
        )
        assert len(out) == 1
        line = out[0]
        assert "engine not ready" in line
        assert "503" in line
        assert "still initialising" in line

    def test_engine_connection_refused(self) -> None:
        out = format_engine_probe_lines(
            {"ok": False, "error": "ConnectError: refused", "hint": None}
        )
        assert len(out) == 1
        assert "engine not ready" in out[0]
        assert "ConnectError" in out[0]
        # No trailing dim hint suffix when hint is None.
        assert "[dim]" not in out[0]

    def test_engine_error_with_hint_includes_dim_suffix(self) -> None:
        out = format_engine_probe_lines(
            {"ok": False, "error": "boom", "hint": "fix it"}
        )
        assert "[dim](fix it)[/dim]" in out[0]

    def test_no_error_no_hint_falls_back_to_status(self) -> None:
        out = format_engine_probe_lines({"ok": False, "status": 500})
        assert len(out) == 1
        assert "500" in out[0]

    def test_uses_yellow_warning_marker(self) -> None:
        out = format_engine_probe_lines(
            {"ok": False, "error": "x"}
        )
        # Distinguishable from the API-side red ✗ so users can tell
        # which probe disagreed.
        assert out[0].startswith("[yellow]⚠")
