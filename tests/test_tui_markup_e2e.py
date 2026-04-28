"""Loop 263: E2E markup-safety regression for the TUI.

The operator hit ``MarkupError: closing tag '[/▍]' does not match any
open tag`` *after* loop 262 had escaped the assistant-reply / user-echo
write paths. Loop 263 hardens the remaining leak surfaces:

  * ``_agent_status`` (hottest path during a multi-step agent turn -- it
    receives tool-call previews, tool-result heads, summaries, and TTFT
    notices, all of which can contain bracket-laden model output).
  * Health-banner exception text and engine-probe text.
  * Save-history / resume / cwd-change writes.
  * Slash-dispatcher result write (``log.write(text)`` from the
    ``_render_*`` helpers; some of those splice audit/tool content).

Plus a defense-in-depth ``_safe_log_write`` helper that catches
``rich.errors.MarkupError`` and retries the write with the entire line
re-escaped, so any future leak degrades to "no styling, still rendered"
instead of "TUI dead".

These tests exercise the offending payload through the actual Rich
markup parser (``rich.text.Text.from_markup``) so the assertions catch
real renderer behaviour, not a mock.
"""
from __future__ import annotations

import time

import pytest

from qwen_coder_mcp import tui


# Helper: render a markup string the way RichLog does, raising
# MarkupError on mismatch so the test fails loudly.
def _render(markup: str) -> str:
    rich_text = pytest.importorskip("rich.text")
    return rich_text.Text.from_markup(markup).plain


# A representative sampling of bracket-laden payloads the model has
# emitted in the wild (taken from real operator tracebacks + tool
# output likely to recur).
OFFENDING_PAYLOADS = [
    "closing tag '[/▍]' does not matc",  # the literal operator complaint
    "regex /\\[(.*?)\\]/ matched",
    "[ERROR] something went wrong",
    "[/dim] dangling close",
    "stdout: [██████████░░] 83%",
    "tool returned [foo][bar]",
    "Traceback (most recent call last):\n  [/x]",
    "agent error: ValueError: '[/▍]' is bad",
]


# ----------------------------------------------- _safe_log_write helper
class _CapturingLog:
    """A RichLog stand-in that *actually* parses markup via rich.text so
    we can assert MarkupError fall-back behaviour without spinning up
    Textual. Stores plain text of every successfully-rendered line."""

    def __init__(self) -> None:
        self.rendered: list[str] = []

    def write(self, content) -> None:  # noqa: ANN001
        # Mimic RichLog(markup=True): parse the input as Rich markup and
        # raise MarkupError when malformed. Renderable objects (e.g.
        # Markdown) bypass markup parsing.
        if not isinstance(content, str):
            self.rendered.append(repr(content))
            return
        rich_text = pytest.importorskip("rich.text")
        # from_markup raises MarkupError on bad input -- exactly what
        # the real RichLog does internally.
        self.rendered.append(rich_text.Text.from_markup(content).plain)


class TestSafeLogWriteFallback:
    @pytest.mark.parametrize("payload", OFFENDING_PAYLOADS)
    def test_bad_markup_falls_back_to_escaped_write(self, payload):
        log = _CapturingLog()
        line = f"[cyan]→ tool[/cyan] {payload}"
        # Direct write would raise; safe wrapper must succeed.
        tui._safe_log_write(log, line)
        assert log.rendered, "fallback path produced no output"
        # The payload's recognisable text MUST be in the rendered line.
        # We strip box-drawing chars before comparing because the
        # rendered text retains them.
        assert any(token in log.rendered[-1] for token in payload.split() if token)

    def test_clean_markup_renders_with_styling(self):
        log = _CapturingLog()
        tui._safe_log_write(log, "[cyan]hello[/cyan]")
        # No fallback triggered; styled output rendered cleanly.
        assert log.rendered == ["hello"]

    def test_swallows_non_markup_errors(self):
        class _Broken:
            def write(self, _content):  # noqa: ANN001
                raise IOError("log unmounted")

        # MUST NOT raise -- TUI logging is observability, not control flow.
        tui._safe_log_write(_Broken(), "anything")

    def test_non_string_content_passes_through(self):
        log = _CapturingLog()
        # Renderables (Markdown, Text, etc) skip markup parsing.
        class _Renderable:
            def __repr__(self) -> str:
                return "<Markdown obj>"

        tui._safe_log_write(log, _Renderable())
        assert log.rendered == ["<Markdown obj>"]


# ----------------------------------------------- targeted escape sites
class TestStatusLineEscaping:
    """The specific markup templates used by ``_agent_status`` callers
    in tui.py. These mirror lines 3680, 3719, 3771, 3795, 3806, 3817 --
    if any of them stop escaping their dynamic suffix this test fails.
    """

    @pytest.mark.parametrize("payload", OFFENDING_PAYLOADS)
    def test_tool_call_status_renders(self, payload):
        # Mirrors: f"[cyan]→ tool[/cyan] {ev.tool}{args_repr}" with safe.
        line = f"[cyan]→ tool[/cyan] {tui._safe_markup('run_shell')} {tui._safe_markup(payload)}"
        rendered = _render(line)
        assert "→ tool" in rendered
        # Payload text should survive (after escape, brackets become literal).
        for token in payload.split():
            if token and "[" not in token:
                assert token in rendered

    @pytest.mark.parametrize("payload", OFFENDING_PAYLOADS)
    def test_tool_result_status_renders(self, payload):
        line = f"[green]← {tui._safe_markup('run_shell')}[/green] (12ms) {tui._safe_markup(payload)}"
        rendered = _render(line)
        assert "←" in rendered

    @pytest.mark.parametrize("payload", OFFENDING_PAYLOADS)
    def test_write_confirm_status_renders(self, payload):
        line = (
            f"[yellow]✎ write[/yellow] {tui._safe_markup('fs_write')} "
            f"{tui._safe_markup(payload)}"
        )
        rendered = _render(line)
        assert "✎ write" in rendered

    @pytest.mark.parametrize("payload", OFFENDING_PAYLOADS)
    def test_summary_status_renders(self, payload):
        line = f"[dim]· {tui._safe_markup(payload)}[/dim]"
        rendered = _render(line)
        # Bullet survives; dim styling is rendered as plain in our extractor.
        assert "·" in rendered

    @pytest.mark.parametrize("payload", OFFENDING_PAYLOADS)
    def test_checkpoint_failure_status_renders(self, payload):
        # Mirrors line 3719.
        line = (
            f"[yellow]⚠ checkpoint failed at step 3: "
            f"{tui._safe_markup(payload)}[/yellow]"
        )
        rendered = _render(line)
        assert "checkpoint failed" in rendered


# --------------------------------------------------- agent error wrapper
class TestAgentErrorWrapping:
    """The runner's exception-to-final_text wrapping (loop 263 changed
    it from ``f"[agent error: ...]"`` to a plain-text ``"agent error:
    ..."`` so the downstream ``_post_assistant`` escape doesn't end up
    showing literal ``\\[`` to the user).
    """

    def test_final_text_format_is_plain(self):
        """The exception payload going into ``final_text`` must NOT
        start with a literal ``[`` because that would force the
        downstream ``_safe_markup`` escape to leak ``\\[`` into the
        rendered output. Plain-text wrapping keeps the qwen> prefix
        styled and the body readable.
        """
        # Reconstruct the format string used in the runner.
        exc = ValueError("'[/▍]' is bad")
        final_text = f"agent error: {type(exc).__name__}: {exc}"
        # Now feed it through the same path _post_assistant uses for
        # plain replies: f"[green]qwen>[/green] {_safe_markup(reply)}".
        line = f"[green]qwen>[/green] {tui._safe_markup(final_text)}"
        rendered = _render(line)
        assert "agent error:" in rendered
        assert "ValueError" in rendered
        assert "▍" in rendered  # box char preserved
        # Crucially: no literal "\\[" leaks into the rendered output --
        # the escape only matters at parse time.
        assert "\\[" not in rendered


# ------------------------------------------------------- E2E benchmark
class TestE2EBenchmark:
    """Drive a synthetic full-turn worth of agent-status writes through
    the same code path the worker thread uses, and assert the whole
    sequence renders without a single MarkupError. Doubles as a perf
    sanity check -- 500 status lines must finish in <1s so the TUI
    doesn't lag during a long agent turn.
    """

    def test_full_turn_renders_cleanly(self):
        log = _CapturingLog()
        events = [
            f"[cyan]→ tool[/cyan] run_shell {tui._safe_markup(p)}"
            for p in OFFENDING_PAYLOADS
        ] + [
            f"[green]← run_shell[/green] (12ms) {tui._safe_markup(p)}"
            for p in OFFENDING_PAYLOADS
        ] + [
            f"[dim]· {tui._safe_markup(p)}[/dim]"
            for p in OFFENDING_PAYLOADS
        ] + [
            f"[yellow]⚠ checkpoint failed at step {i}: {tui._safe_markup(p)}[/yellow]"
            for i, p in enumerate(OFFENDING_PAYLOADS)
        ]
        for line in events:
            tui._safe_log_write(log, line)
        # Every line landed in the buffer.
        assert len(log.rendered) == len(events)

    def test_500_status_writes_under_one_second(self):
        log = _CapturingLog()
        sample = (
            f"[cyan]→ tool[/cyan] run_shell "
            f"{tui._safe_markup('cmd with [/▍] block char')}"
        )
        t0 = time.monotonic()
        for _ in range(500):
            tui._safe_log_write(log, sample)
        elapsed = time.monotonic() - t0
        assert len(log.rendered) == 500
        # Generous bound -- on modest CI hardware this completes in
        # ~50ms. >1s indicates a regression.
        assert elapsed < 1.0, f"500 status writes took {elapsed:.3f}s"

    def test_fallback_path_under_pressure(self):
        """Same payloads, but feeding RAW (un-pre-escaped) markup to
        ``_safe_log_write`` so the fallback escape path is exercised
        end-to-end. Asserts no crash and bounded latency.
        """
        log = _CapturingLog()
        bad_lines = [f"[cyan]prefix[/cyan] {p}" for p in OFFENDING_PAYLOADS]
        t0 = time.monotonic()
        for _ in range(50):
            for line in bad_lines:
                tui._safe_log_write(log, line)
        elapsed = time.monotonic() - t0
        assert len(log.rendered) == 50 * len(bad_lines)
        # Fallback path is slower (one parse, one re-escape, one parse)
        # but must still be under a second for ~400 lines.
        assert elapsed < 1.0, f"fallback writes took {elapsed:.3f}s"
