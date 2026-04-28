"""Pin the loop-startup vLLM /health pre-flight probe.

Loop 219: instead of letting operators discover an unhealthy backend
by reading the first chat-call timeout traceback, the autonomous
loop probes /health up front and writes a structured readiness line
to runtime.log. This module pins the probe's:

- success path (one attempt, immediate ok)
- transient unavailability path (eventual success)
- timeout path (proceeds anyway, never blocks forever)
- env-disabled path
- missing-method path (graceful skip)
- exception path (probe call itself raises)
"""
from __future__ import annotations

import pytest

from agent.loop import _preflight_health_probe


class _FakeClient:
    """Minimal QwenClient-shaped stub. ``probe_results`` is consumed
    in order, last result repeats."""

    def __init__(self, probe_results: list[dict]) -> None:
        self.probe_results = list(probe_results)
        self.calls = 0

    def vllm_health_probe(self) -> dict:
        self.calls += 1
        if self.probe_results:
            return self.probe_results[
                min(self.calls - 1, len(self.probe_results) - 1)
            ]
        return {"ok": False, "error": "no result"}


class _RaisingClient:
    """Client whose probe raises -- the wrapper must never propagate."""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls = 0

    def vllm_health_probe(self) -> dict:
        self.calls += 1
        raise self.exc


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s

    def monotonic(self) -> float:
        return self.now


class TestPreflightHealthProbe:
    def test_immediate_ok_on_first_attempt(self) -> None:
        client = _FakeClient([{"ok": True, "status": 200}])
        clock = _FakeClock()
        out = _preflight_health_probe(
            client,
            deadline_seconds=30.0,
            poll_interval_seconds=3.0,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
        assert out["ok"] is True
        assert client.calls == 1
        assert clock.sleeps == []  # no waiting needed

    def test_eventual_ok_after_transient_failures(self) -> None:
        client = _FakeClient(
            [
                {"ok": False, "status": 503, "hint": "warming up"},
                {"ok": False, "error": "ConnectError"},
                {"ok": True, "status": 200},
            ]
        )
        clock = _FakeClock()
        out = _preflight_health_probe(
            client,
            deadline_seconds=30.0,
            poll_interval_seconds=3.0,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
        assert out["ok"] is True
        assert client.calls == 3
        # Two backoff waits, before the third probe.
        assert clock.sleeps == [3.0, 3.0]

    def test_deadline_elapses_returns_last_result(self) -> None:
        client = _FakeClient(
            [{"ok": False, "status": 503, "hint": "still booting"}]
        )
        clock = _FakeClock()
        out = _preflight_health_probe(
            client,
            deadline_seconds=10.0,
            poll_interval_seconds=3.0,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
        assert out["ok"] is False
        assert out.get("status") == 503
        # The probe ran to deadline and returned the last result; the
        # caller (main()) is expected to proceed regardless.
        assert client.calls >= 2
        # Total elapsed time is at most one poll-interval past deadline
        # (the final sleep is clamped to remaining-budget).
        assert clock.now <= 10.0 + 3.0

    def test_disabled_via_env(self, monkeypatch) -> None:
        monkeypatch.setenv("QWEN_LOOP_DISABLE_HEALTH_PROBE", "1")
        client = _FakeClient([{"ok": True}])
        clock = _FakeClock()
        out = _preflight_health_probe(
            client,
            deadline_seconds=30.0,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
        assert out == {"ok": False, "skipped": True}
        # Env-disable means we never call the probe -- it can't even
        # be inferred from a stub without this branch.
        assert client.calls == 0

    def test_missing_probe_method_is_skipped(self) -> None:
        class _StubClient:
            pass

        out = _preflight_health_probe(_StubClient())
        assert out == {"ok": False, "skipped": True}

    def test_probe_exception_is_swallowed(self) -> None:
        client = _RaisingClient(RuntimeError("boom"))
        clock = _FakeClock()
        out = _preflight_health_probe(
            client,
            deadline_seconds=5.0,
            poll_interval_seconds=1.0,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
        # Probe must NEVER propagate -- the loop must always reach
        # its iteration body.
        assert out["ok"] is False
        assert "boom" in str(out.get("error", ""))
        assert client.calls >= 1

    def test_zero_deadline_runs_one_probe(self) -> None:
        # Operators may want a single non-blocking check.
        client = _FakeClient([{"ok": False, "status": 503}])
        clock = _FakeClock()
        out = _preflight_health_probe(
            client,
            deadline_seconds=0.0,
            poll_interval_seconds=1.0,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
        assert out["ok"] is False
        assert client.calls == 1
        assert clock.sleeps == []
