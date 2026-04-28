"""Tests for the loop-249 autonomous-loop ↔ task_memory bridge.

The autonomous self-improvement loop runs forever, processing one file
per iteration. Loop 249 wires it into the persistent TaskMemory so the
model sees the iteration's goal as ``current_task`` even after vLLM
restarts or context compression. The bridge is a single helper —
``agent.loop._seed_iteration_memory`` — invoked once per iteration with
the iteration number and the file under review.

Properties under test:
  * Helper sets current_task with both iteration number and rel path.
  * Helper records role + iteration as facts (queryable via recall_state).
  * Helper is a no-op when the client has no ``task_memory`` attached.
  * Helper swallows every kind of memory failure — must never raise.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent import loop as agent_loop_mod
from qwen_coder_mcp.task_memory import TaskMemory


class _ClientWith:
    def __init__(self, tm):
        self.task_memory = tm


class _ClientWithout:
    task_memory = None


@pytest.fixture
def memory(tmp_path: Path) -> TaskMemory:
    return TaskMemory(path=tmp_path / "state.json")


class TestSeedIterationMemoryHappyPath:
    def test_sets_current_task_with_iteration_and_rel(self, memory):
        client = _ClientWith(memory)
        agent_loop_mod._seed_iteration_memory(
            client, iteration=42, rel=Path("src/foo.py")
        )
        ct = memory.snapshot()["current_task"]
        assert "iteration 42" in ct
        assert "src/foo.py" in ct
        assert "bugs" in ct.lower() or "improvement" in ct.lower()

    def test_records_loop_iteration_fact(self, memory):
        client = _ClientWith(memory)
        agent_loop_mod._seed_iteration_memory(
            client, iteration=7, rel=Path("a.py")
        )
        assert memory.snapshot()["facts"]["loop_iteration"] == "7"

    def test_records_agent_role_fact(self, memory):
        client = _ClientWith(memory)
        agent_loop_mod._seed_iteration_memory(
            client, iteration=1, rel=Path("a.py")
        )
        role = memory.snapshot()["facts"]["agent_role"]
        assert "autonomous" in role.lower()

    def test_persists_across_reload(self, tmp_path):
        path = tmp_path / "state.json"
        tm = TaskMemory(path=path)
        client = _ClientWith(tm)
        agent_loop_mod._seed_iteration_memory(
            client, iteration=99, rel=Path("x.py")
        )
        # New process / fresh client.
        tm2 = TaskMemory(path=path)
        snap = tm2.snapshot()
        assert "iteration 99" in snap["current_task"]
        assert snap["facts"]["loop_iteration"] == "99"

    def test_overwrites_previous_iteration_task(self, memory):
        client = _ClientWith(memory)
        agent_loop_mod._seed_iteration_memory(
            client, iteration=1, rel=Path("a.py")
        )
        agent_loop_mod._seed_iteration_memory(
            client, iteration=2, rel=Path("b.py")
        )
        ct = memory.snapshot()["current_task"]
        assert "iteration 2" in ct
        assert "b.py" in ct
        assert "iteration 1" not in ct


class TestSeedIterationMemoryNoOpPaths:
    def test_no_memory_attached_is_silent_noop(self):
        # Must not raise.
        agent_loop_mod._seed_iteration_memory(
            _ClientWithout(), iteration=1, rel=Path("a.py")
        )

    def test_client_without_attribute_is_silent_noop(self):
        class _Empty:
            pass

        agent_loop_mod._seed_iteration_memory(
            _Empty(), iteration=1, rel=Path("a.py")
        )


class TestSeedIterationMemoryFailureSafe:
    """Memory glitches must never break the iteration."""

    def test_set_current_task_failure_swallowed(self, tmp_path):
        class _BoomMem:
            def set_current_task(self, _desc):
                raise RuntimeError("boom")

            def record_fact(self, _k, _v):
                pass

        client = _ClientWith(_BoomMem())
        # Must not raise.
        agent_loop_mod._seed_iteration_memory(
            client, iteration=1, rel=Path("a.py")
        )

    def test_record_fact_failure_swallowed(self):
        class _BoomMem:
            def set_current_task(self, _desc):
                pass

            def record_fact(self, _k, _v):
                raise RuntimeError("kaboom")

        client = _ClientWith(_BoomMem())
        agent_loop_mod._seed_iteration_memory(
            client, iteration=1, rel=Path("a.py")
        )

    def test_attribute_access_failure_swallowed(self):
        class _BoomClient:
            @property
            def task_memory(self):
                raise RuntimeError("property boom")

        # Must not raise even when getattr itself blows up via property.
        agent_loop_mod._seed_iteration_memory(
            _BoomClient(), iteration=1, rel=Path("a.py")
        )
