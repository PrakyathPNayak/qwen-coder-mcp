"""Tests for the loop-244 TaskMemory + auto-injection feature.

Loop 244 fixes "stops abruptly and forgets context often" by giving
the QwenClient a persistent task / todo / facts store whose rendered
[Task memory: ...] block is auto-prepended to every chat request as a
synthetic system message, so the model retains continuity even when
the message history compresses or the session restarts entirely.
"""
from __future__ import annotations

import json

import httpx
import pytest

from qwen_coder_mcp.qwen_client import ChatMessage, QwenClient
from qwen_coder_mcp.config import Settings
from qwen_coder_mcp.task_memory import (
    TaskMemory,
    Todo,
    load_default_task_memory,
)


def _settings(tmp_path):
    return Settings(
        base_url="http://test/v1",
        api_key="EMPTY",
        model="qwen3.6-27b",
        timeout=5,
        max_tokens=100,
        server_max_len=8192,
        loop_interval_seconds=1,
        loop_max_file_bytes=1000,
        loop_push=False,
    )


def _ok(text: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "x", "object": "chat.completion", "created": 0,
            "model": "m", "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }],
        },
    )


class TestTaskMemoryPersistence:
    def test_empty_by_default(self, tmp_path):
        m = TaskMemory(path=tmp_path / "state.json")
        assert m.is_empty()
        assert m.to_system_prompt() == ""

    def test_set_and_persist_current_task(self, tmp_path):
        p = tmp_path / "state.json"
        m = TaskMemory(path=p)
        m.set_current_task("ship loop 244")
        assert p.exists()
        # Reload from disk -> task survives.
        m2 = TaskMemory(path=p)
        assert m2.current_task == "ship loop 244"

    def test_add_and_persist_todos(self, tmp_path):
        p = tmp_path / "state.json"
        m = TaskMemory(path=p)
        m.add_todo("t1", "do thing", status="open")
        m.add_todo("t2", "do other", status="in_progress")
        m2 = TaskMemory(path=p)
        ids = [t.id for t in m2.todos]
        assert ids == ["t1", "t2"]
        assert m2.todos[1].status == "in_progress"

    def test_update_todo_status(self, tmp_path):
        m = TaskMemory(path=tmp_path / "state.json")
        m.add_todo("t1", "x")
        assert m.update_todo_status("t1", "done") is True
        assert m.todos[0].status == "done"
        assert m.update_todo_status("nope", "done") is False

    def test_add_todo_replaces_existing_id(self, tmp_path):
        m = TaskMemory(path=tmp_path / "state.json")
        m.add_todo("t1", "old desc")
        m.add_todo("t1", "new desc", status="in_progress")
        assert len(m.todos) == 1
        assert m.todos[0].description == "new desc"
        assert m.todos[0].status == "in_progress"

    def test_remove_todo(self, tmp_path):
        m = TaskMemory(path=tmp_path / "state.json")
        m.add_todo("t1", "x")
        m.add_todo("t2", "y")
        assert m.remove_todo("t1") is True
        assert [t.id for t in m.todos] == ["t2"]
        assert m.remove_todo("nonexistent") is False

    def test_record_fact(self, tmp_path):
        m = TaskMemory(path=tmp_path / "state.json")
        m.record_fact("server_port", "8000")
        assert m.facts == {"server_port": "8000"}

    def test_record_decision(self, tmp_path):
        m = TaskMemory(path=tmp_path / "state.json")
        m.record_decision("use vLLM 0.11")
        assert m.decisions == ["use vLLM 0.11"]

    def test_clear(self, tmp_path):
        m = TaskMemory(path=tmp_path / "state.json")
        m.set_current_task("x")
        m.add_todo("t1", "y")
        m.record_fact("k", "v")
        m.clear()
        assert m.is_empty()

    def test_corrupt_json_yields_empty_memory(self, tmp_path):
        p = tmp_path / "state.json"
        p.write_text("{not valid json")
        m = TaskMemory(path=p)
        assert m.is_empty()

    def test_atomic_save_no_temp_files_left(self, tmp_path):
        p = tmp_path / "state.json"
        m = TaskMemory(path=p)
        m.set_current_task("x")
        # Only the final state.json should exist; no .tmp files leaked.
        files = list(tmp_path.iterdir())
        assert {f.name for f in files} == {"state.json"}

    def test_eviction_caps_todo_count(self, tmp_path):
        m = TaskMemory(path=tmp_path / "state.json", max_todos=3)
        for i in range(5):
            m.add_todo(f"t{i}", f"desc{i}")
        assert len(m.todos) == 3
        # FIFO drops oldest.
        assert "t0" not in [t.id for t in m.todos]

    def test_eviction_caps_decisions(self, tmp_path):
        m = TaskMemory(path=tmp_path / "state.json", max_decisions=2)
        for i in range(4):
            m.record_decision(f"d{i}")
        assert m.decisions == ["d2", "d3"]

    def test_eviction_with_many_done_todos_no_indexerror(self, tmp_path):
        """Regression: ``_evict_overflow_locked`` used to walk a stale
        list of done-indices, which raised ``IndexError`` (or skipped
        entries) once ``pop()`` shifted later positions. Adding more
        done todos than ``max_todos`` in a tight loop reliably
        triggered it; the fix now evicts selected victims by stable
        todo id instead of mutating through stale list indices.
        """
        m = TaskMemory(path=tmp_path / "state.json", max_todos=2)
        for i in range(6):
            m.add_todo(f"t{i}", f"d{i}", status="done")
        # No crash, exactly max_todos remain, kept ones are the newest.
        assert len(m.todos) == 2
        ids = {t.id for t in m.todos}
        assert "t0" not in ids and "t1" not in ids

    def test_invalid_todo_status_is_normalised(self, tmp_path):
        """``add_todo`` / ``update_todo_status`` previously accepted any
        string, which broke open/done bookkeeping. Bad values are now
        coerced to ``"open"`` so the rest of the module's state-machine
        assumptions hold."""
        m = TaskMemory(path=tmp_path / "state.json")
        m.add_todo("t1", "x", status="garbage")
        assert m.todos[0].status == "open"
        assert m.update_todo_status("t1", "also-garbage") is True
        assert m.todos[0].status == "open"
        # Valid values still flow through unchanged.
        m.update_todo_status("t1", "in_progress")
        assert m.todos[0].status == "in_progress"


class TestRendering:
    def test_renders_current_task(self, tmp_path):
        m = TaskMemory(path=tmp_path / "state.json")
        m.set_current_task("implement loop 244")
        block = m.to_system_prompt()
        assert "[Task memory:" in block
        assert "current task: implement loop 244" in block

    def test_renders_open_todos(self, tmp_path):
        m = TaskMemory(path=tmp_path / "state.json")
        m.add_todo("t1", "write tests", status="in_progress")
        m.add_todo("t2", "ship it", status="open")
        block = m.to_system_prompt()
        assert "open todos:" in block
        assert "t1" in block and "write tests" in block
        assert "t2" in block

    def test_renders_done_todos_compactly(self, tmp_path):
        m = TaskMemory(path=tmp_path / "state.json")
        for i in range(5):
            m.add_todo(f"d{i}", "x", status="done")
        block = m.to_system_prompt()
        assert "done todos" in block
        # Compact: shows count + last 3 ids, not full descriptions.
        assert "5 total" in block

    def test_facts_truncated(self, tmp_path):
        m = TaskMemory(path=tmp_path / "state.json")
        m.record_fact("big", "x" * 500)
        block = m.to_system_prompt()
        assert "…" in block

    def test_recent_decisions_capped_at_5(self, tmp_path):
        m = TaskMemory(path=tmp_path / "state.json", max_decisions=10)
        for i in range(8):
            m.record_decision(f"decision-{i}")
        block = m.to_system_prompt()
        # Only last 5 should show.
        assert "decision-3" in block  # last 5: 3,4,5,6,7
        assert "decision-7" in block
        assert "decision-0" not in block
        assert "decision-2" not in block

    def test_continuity_hint_in_block(self, tmp_path):
        m = TaskMemory(path=tmp_path / "state.json")
        m.set_current_task("x")
        block = m.to_system_prompt()
        # Footer reminds the model to actually use the memory.
        assert "continuity" in block.lower()

    def test_snapshot_returns_json_safe_view(self, tmp_path):
        m = TaskMemory(path=tmp_path / "state.json")
        m.set_current_task("t")
        m.add_todo("t1", "x")
        m.record_fact("k", "v")
        snap = m.snapshot()
        # round-trip through json must work
        json.dumps(snap)
        assert snap["current_task"] == "t"
        assert snap["todos"][0]["id"] == "t1"
        assert snap["facts"]["k"] == "v"


class TestEnvLoading:
    def test_default_disabled(self, monkeypatch):
        monkeypatch.delenv("QWEN_TASK_MEMORY", raising=False)
        assert load_default_task_memory() is None

    def test_enabled_via_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QWEN_TASK_MEMORY", "1")
        monkeypatch.setenv("QWEN_TASK_MEMORY_PATH", str(tmp_path / "s.json"))
        m = load_default_task_memory()
        assert isinstance(m, TaskMemory)
        assert m.path == tmp_path / "s.json"

    def test_truthy_values_enable(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QWEN_TASK_MEMORY_PATH", str(tmp_path / "s.json"))
        for v in ("1", "true", "yes", "on", "TRUE"):
            monkeypatch.setenv("QWEN_TASK_MEMORY", v)
            assert load_default_task_memory() is not None

    def test_falsy_values_disable(self, monkeypatch):
        for v in ("0", "false", "no", "off", ""):
            monkeypatch.setenv("QWEN_TASK_MEMORY", v)
            assert load_default_task_memory() is None


class TestQwenClientInjection:
    @staticmethod
    def _client(handler, settings, *, attach_memory: TaskMemory | None = None):
        c = QwenClient(settings=settings)
        c._client = httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url=settings.base_url,
            timeout=settings.timeout,
        )
        c.task_memory = attach_memory
        return c

    def test_no_memory_no_synthetic_system_msg(self, tmp_path):
        seen: dict = {}

        def handler(req):
            seen.update(json.loads(req.content.decode("utf-8")))
            return _ok("ok")

        c = self._client(handler, _settings(tmp_path), attach_memory=None)
        c.chat([ChatMessage("user", "hi")])
        roles = [m["role"] for m in seen["messages"]]
        assert roles == ["user"]

    def test_empty_memory_no_synthetic_system_msg(self, tmp_path):
        seen: dict = {}

        def handler(req):
            seen.update(json.loads(req.content.decode("utf-8")))
            return _ok("ok")

        m = TaskMemory(path=tmp_path / "state.json")
        c = self._client(handler, _settings(tmp_path), attach_memory=m)
        c.chat([ChatMessage("user", "hi")])
        roles = [m["role"] for m in seen["messages"]]
        # Memory empty -> no injection.
        assert roles == ["user"]

    def test_populated_memory_prepends_synthetic_system(self, tmp_path):
        seen: dict = {}

        def handler(req):
            seen.update(json.loads(req.content.decode("utf-8")))
            return _ok("ok")

        m = TaskMemory(path=tmp_path / "state.json")
        m.set_current_task("ship loop 244")
        m.add_todo("t1", "write tests", status="in_progress")
        c = self._client(handler, _settings(tmp_path), attach_memory=m)
        c.chat([ChatMessage("user", "what's next?")])
        msgs = seen["messages"]
        # First message must be a system msg containing the task memory.
        assert msgs[0]["role"] == "system"
        assert "Task memory" in msgs[0]["content"]
        assert "ship loop 244" in msgs[0]["content"]
        assert "t1" in msgs[0]["content"]
        # User msg follows.
        assert msgs[-1]["role"] == "user"
        assert msgs[-1]["content"] == "what's next?"

    def test_inserts_after_existing_system_role_prompt(self, tmp_path):
        seen: dict = {}

        def handler(req):
            seen.update(json.loads(req.content.decode("utf-8")))
            return _ok("ok")

        m = TaskMemory(path=tmp_path / "state.json")
        m.set_current_task("X")
        c = self._client(handler, _settings(tmp_path), attach_memory=m)
        c.chat([
            ChatMessage("system", "you are a coder"),
            ChatMessage("user", "hi"),
        ])
        msgs = seen["messages"]
        # Original role-prompt first, memory block second, user last.
        assert msgs[0]["content"] == "you are a coder"
        assert "Task memory" in msgs[1]["content"]
        assert msgs[-1]["role"] == "user"

    def test_streaming_path_also_injects(self, tmp_path):
        seen: dict = {}

        def handler(req):
            seen.update(json.loads(req.content.decode("utf-8")))
            return httpx.Response(
                200,
                text='data: {"choices":[{"delta":{"content":"x"},"finish_reason":"stop"}]}\ndata: [DONE]\n',
                headers={"content-type": "text/event-stream"},
            )

        m = TaskMemory(path=tmp_path / "state.json")
        m.set_current_task("X")
        c = self._client(handler, _settings(tmp_path), attach_memory=m)
        list(c.chat_stream([ChatMessage("user", "hi")]))
        msgs = seen["messages"]
        assert any("Task memory" in mm["content"] for mm in msgs)

    def test_caller_messages_not_mutated(self, tmp_path):
        m = TaskMemory(path=tmp_path / "state.json")
        m.set_current_task("X")
        c = self._client(lambda r: _ok("ok"), _settings(tmp_path), attach_memory=m)
        msgs = [ChatMessage("user", "hi")]
        c.chat(msgs)
        # Caller's list still has just the one user msg.
        assert len(msgs) == 1
        assert msgs[0].role == "user"

    def test_memory_failure_does_not_break_chat(self, tmp_path):
        """If TaskMemory.to_system_prompt() raises, chat() must still
        succeed -- memory is decorative, not load-bearing."""

        class BrokenMemory:
            def to_system_prompt(self):
                raise RuntimeError("boom")

        seen: dict = {}

        def handler(req):
            seen.update(json.loads(req.content.decode("utf-8")))
            return _ok("ok")

        c = self._client(
            handler, _settings(tmp_path), attach_memory=BrokenMemory()
        )
        # Must not raise.
        out = c.chat([ChatMessage("user", "hi")])
        assert out == "ok"
        # No injection happened (the exception was swallowed).
        assert all("Task memory" not in m["content"] for m in seen["messages"])
