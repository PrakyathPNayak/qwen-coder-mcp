"""Pin the autonomous-loop structured exit-reason logging.

Loop 225 added ``_format_exit_line``, ``_log_exit``,
``_install_sigterm_handler``, and the matching SystemExit/Keyboard
Interrupt/exception handling around ``main()``'s while-True. Without
these, a SIGTERM from ``stop_qwen.sh``-style management or an
unhandled crash inside the inner try/except would terminate
``main()`` silently -- runtime.log would just stop, no breadcrumb.

The pure formatting helper ``_format_exit_line`` is the load-bearing
testable piece; the rest is glue that defers to it.
"""
from __future__ import annotations

import signal

import pytest

from agent import loop as agent_loop


class TestFormatExitLine:
    def test_no_exception_minimal_form(self) -> None:
        line = agent_loop._format_exit_line("sigterm", 42)
        # Loop 233: pid added to disambiguate concurrent loops.
        assert line.startswith("loop exit reason=sigterm | iter=42 | pid=")
        # No exc segment when exc=None.
        assert "exc=" not in line

    def test_with_exception_includes_type_and_message(self) -> None:
        line = agent_loop._format_exit_line(
            "unhandled-exception", 7, exc=ValueError("bad input")
        )
        assert "exc=ValueError: bad input" in line
        assert "iter=7" in line
        assert "reason=unhandled-exception" in line

    def test_with_exception_no_message_emits_type_only(self) -> None:
        # An exception raised with no args has empty str(); we
        # should still log the type without a stray colon.
        line = agent_loop._format_exit_line(
            "unhandled-exception", 1, exc=RuntimeError()
        )
        assert "exc=RuntimeError" in line
        assert "exc=RuntimeError:" not in line, (
            "no message -> no trailing colon"
        )

    def test_multiline_message_collapsed_to_first_line(self) -> None:
        # A traceback-style multi-line message would break the
        # one-record-per-line grep-friendliness of runtime.log.
        msg = "first line\nsecond line\nthird line"
        line = agent_loop._format_exit_line(
            "unhandled-exception", 3, exc=RuntimeError(msg)
        )
        assert "first line" in line
        assert "second line" not in line
        assert "third line" not in line

    def test_long_message_truncated(self) -> None:
        # 240 chars max, suffix '...' marker added when truncated.
        long_msg = "x" * 500
        line = agent_loop._format_exit_line(
            "unhandled-exception", 0, exc=RuntimeError(long_msg)
        )
        # Find the exc segment.
        seg = line.split("exc=RuntimeError: ", 1)[1]
        assert len(seg) <= 240
        assert seg.endswith("...")

    def test_zero_iteration(self) -> None:
        # Edge case: process killed before the first iteration ran.
        # Surface that as iter=0, not iter='' or iter=None.
        line = agent_loop._format_exit_line("sigterm", 0)
        assert "iter=0" in line

    def test_keyboard_interrupt_reason(self) -> None:
        line = agent_loop._format_exit_line("keyboard-interrupt", 99)
        assert "reason=keyboard-interrupt" in line


class TestShutdownRequestedException:
    def test_is_subclass_of_system_exit(self) -> None:
        # The whole point: the SIGTERM handler raises this and the
        # main loop's existing SystemExit-handling paths (interpreter
        # shutdown, finally blocks) run normally.
        assert issubclass(agent_loop._ShutdownRequested, SystemExit)

    def test_default_exit_code_is_zero(self) -> None:
        # SIGTERM is a graceful shutdown request, so the default
        # exit code must be 0 (nothing went wrong).
        with pytest.raises(agent_loop._ShutdownRequested) as excinfo:
            raise agent_loop._ShutdownRequested(0)
        assert excinfo.value.code == 0


class TestInstallSigtermHandler:
    @pytest.fixture(autouse=True)
    def _restore_sigterm(self) -> None:  # type: ignore[misc]
        # Each test installs a handler; restore the default after
        # so we don't pollute the test runner's signal table.
        original = signal.getsignal(signal.SIGTERM)
        yield
        signal.signal(signal.SIGTERM, original)

    def test_returns_true_on_linux(self) -> None:
        # Linux always has SIGTERM; install should succeed.
        assert agent_loop._install_sigterm_handler() is True

    def test_handler_raises_shutdown_requested(self) -> None:
        # After installation, the handler must raise our
        # SystemExit subclass (not just print or silently swallow).
        # Drive the handler directly via signal.raise_signal so
        # we don't depend on inter-process kill semantics.
        installed = agent_loop._install_sigterm_handler()
        assert installed is True
        with pytest.raises(agent_loop._ShutdownRequested):
            signal.raise_signal(signal.SIGTERM)


class TestLogExitNeverRaises:
    def test_log_exit_swallows_log_failures(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # If _log itself blew up, _log_exit must not propagate --
        # observability must never break the loop. Pin the
        # try/except around the _log call.
        def _boom(*_a, **_k) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(agent_loop, "_log", _boom)
        # Should not raise.
        agent_loop._log_exit("sigterm", 42)

    def test_log_exit_calls_log_with_formatted_line(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[str] = []
        monkeypatch.setattr(agent_loop, "_log", captured.append)
        agent_loop._log_exit("sigterm", 17)
        assert len(captured) == 1
        assert captured[0].startswith("loop exit reason=sigterm | iter=17 | pid=")


class TestExitLinePidLoop233:
    """Loop 233: pid added to the exit line and timing-log extras so
    two simultaneous loops in different repos do not collide on
    iteration_count alone in joined analytics."""

    def test_format_exit_line_includes_real_pid(self) -> None:
        import os as _os
        line = agent_loop._format_exit_line("sigterm", 1)
        assert f"pid={_os.getpid()}" in line

    def test_format_exit_line_pid_segment_after_iter(self) -> None:
        # Order matters for grep: reason | iter | pid | (exc).
        line = agent_loop._format_exit_line("sigterm", 1)
        iter_idx = line.index("iter=")
        pid_idx = line.index("pid=")
        assert iter_idx < pid_idx

    def test_format_exit_line_pid_before_exc(self) -> None:
        line = agent_loop._format_exit_line(
            "unhandled-exception", 5, exc=ValueError("oops")
        )
        pid_idx = line.index("pid=")
        exc_idx = line.index("exc=")
        assert pid_idx < exc_idx

    def test_write_timing_exit_emits_pid_in_record(self, tmp_path, monkeypatch) -> None:
        # Loop 233: pid extra threaded through _write_timing_exit so
        # timing.log JSON records also disambiguate concurrent loops.
        import os as _os
        captured: list[dict] = []
        def fake_write_timing(loop_dir, outcome, phases, *, extras=None, **kw):
            captured.append({"outcome": outcome, "extras": dict(extras or {})})
        monkeypatch.setattr(agent_loop, "_write_timing", fake_write_timing)
        agent_loop._write_timing_exit("sigterm", 42)
        assert len(captured) == 1
        rec = captured[0]
        assert rec["outcome"] == "exit:sigterm"
        assert rec["extras"]["iteration_count"] == 42
        assert rec["extras"]["pid"] == _os.getpid()
