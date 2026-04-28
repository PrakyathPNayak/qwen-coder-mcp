"""Pin the ``timing.log`` exit-record extension (loop 226).

Loop 105 added a synthetic ``crashed`` record so analytics counting
outcomes per category never undercount iterations when the inner
try/except fired. Loop 226 extends the same pattern to the
shutdown path: without this, timing.log analytics undercount the
final iteration and cannot disambiguate SIGTERM from
KeyboardInterrupt from an unhandled crash.

Symmetric with the loop-225 runtime.log exit line; the
``iteration_count`` extra is the join key between the two streams.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent import loop as agent_loop


@pytest.fixture
def isolated_timing_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    timing = tmp_path / "timing.log"
    monkeypatch.setattr(agent_loop, "TIMING_FILE", timing)
    # Disable rotation so the test doesn't accidentally truncate.
    monkeypatch.setattr(
        agent_loop, "_rotate_timing_if_oversized", lambda: None
    )
    return timing


def _last_record(path: Path) -> dict:
    lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
    assert lines, f"no records in {path}"
    return json.loads(lines[-1])


class TestWriteTimingExtras:
    def test_extras_dict_is_merged_into_record(
        self, isolated_timing_log: Path
    ) -> None:
        # Direct exercise of the extras kwarg added in loop 226.
        agent_loop._write_timing(
            Path("."),
            "exit:sigterm",
            {},
            extras={"iteration_count": 42, "custom_marker": "x"},
        )
        rec = _last_record(isolated_timing_log)
        assert rec["iteration_count"] == 42
        assert rec["custom_marker"] == "x"

    def test_extras_cannot_overwrite_load_bearing_keys(
        self, isolated_timing_log: Path
    ) -> None:
        # If extras silently overrode 'outcome' or 'category', a
        # caller bug would corrupt every analytics consumer that
        # filters on category. Pin the guard.
        agent_loop._write_timing(
            Path("."),
            "exit:sigterm",
            {},
            extras={
                "outcome": "FAKE_OVERRIDE",
                "category": "FAKE_OVERRIDE",
                "ts": "FAKE_OVERRIDE",
                "file": "FAKE_OVERRIDE",
                "phases": "FAKE_OVERRIDE",
                "iteration_count": 1,
            },
        )
        rec = _last_record(isolated_timing_log)
        assert rec["outcome"] == "exit:sigterm"
        assert rec["category"] == "exit"
        assert rec["phases"] == {}
        assert rec["file"] == "."
        # ts is 'now' -- just verify it's not the override.
        assert rec["ts"] != "FAKE_OVERRIDE"
        # The non-reserved key still wins.
        assert rec["iteration_count"] == 1

    def test_extras_none_keeps_pre_loop_226_shape(
        self, isolated_timing_log: Path
    ) -> None:
        # Backwards compat: existing callers pass nothing and must
        # get exactly the same record shape they always have.
        agent_loop._write_timing(Path("."), "ok:diff_applied", {"qwen": 1.5})
        rec = _last_record(isolated_timing_log)
        assert set(rec.keys()) == {"ts", "file", "outcome", "category", "phases"}
        assert "iteration_count" not in rec


class TestWriteTimingExit:
    def test_writes_exit_record_with_iteration_count(
        self, isolated_timing_log: Path
    ) -> None:
        agent_loop._write_timing_exit("sigterm", 42)
        rec = _last_record(isolated_timing_log)
        assert rec["outcome"] == "exit:sigterm"
        assert rec["category"] == "exit"
        assert rec["iteration_count"] == 42
        # No phases on a synthetic exit record.
        assert rec["phases"] == {}

    def test_keyboard_interrupt_reason(self, isolated_timing_log: Path) -> None:
        agent_loop._write_timing_exit("keyboard-interrupt", 7)
        rec = _last_record(isolated_timing_log)
        assert rec["outcome"] == "exit:keyboard-interrupt"
        assert rec["iteration_count"] == 7

    def test_unhandled_exception_reason(
        self, isolated_timing_log: Path
    ) -> None:
        agent_loop._write_timing_exit("unhandled-exception", 99)
        rec = _last_record(isolated_timing_log)
        assert rec["outcome"] == "exit:unhandled-exception"
        assert rec["category"] == "exit"
        assert rec["iteration_count"] == 99

    def test_zero_iteration_count(self, isolated_timing_log: Path) -> None:
        # Edge case: process killed before the first iteration ran.
        agent_loop._write_timing_exit("sigterm", 0)
        rec = _last_record(isolated_timing_log)
        assert rec["iteration_count"] == 0

    def test_swallows_write_failures(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Like every other timing.log helper, must never raise even
        # if the underlying write blew up.
        def _boom(*_a, **_k) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(agent_loop, "_write_timing", _boom)
        # Should not raise.
        agent_loop._write_timing_exit("sigterm", 1)
