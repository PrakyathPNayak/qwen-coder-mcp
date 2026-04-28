"""Tests for the /memory slash command (loop 245).

The command is the operator-facing surface for the loop-244 TaskMemory.
It exposes show / set-task / todo add+done+block+del / fact / decision /
clear / --json subcommands. These tests pin the contract against
``tui._render_memory`` directly and verify the dispatcher routes
``/memory`` to it via ``execute_slash``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from qwen_coder_mcp import fs_tools, tui
from qwen_coder_mcp.task_memory import TaskMemory


class _StubClient:
    """Minimal stand-in: only ``task_memory`` is read by /memory."""

    def __init__(self, tm: TaskMemory | None) -> None:
        self.task_memory = tm


@pytest.fixture
def memory(tmp_path: Path) -> TaskMemory:
    return TaskMemory(path=tmp_path / "state.json")


@pytest.fixture
def client(memory: TaskMemory) -> _StubClient:
    return _StubClient(memory)


class TestRenderMemoryShow:
    def test_disabled_when_no_task_memory_attached(self):
        out = tui._render_memory(_StubClient(None), [])
        assert "QWEN_TASK_MEMORY" in out
        assert "disabled" in out

    def test_show_empty_memory(self, client):
        out = tui._render_memory(client, [])
        assert out == "task memory: empty"

    def test_show_word_alias(self, client):
        client.task_memory.set_current_task("ship loop 245")
        out = tui._render_memory(client, ["show"])
        assert "current task: ship loop 245" in out

    def test_show_lists_current_task_and_todos(self, client):
        client.task_memory.set_current_task("ship loop 245")
        client.task_memory.add_todo("t1", "wire dispatcher", status="open")
        client.task_memory.add_todo("t2", "tests", status="in_progress")
        out = tui._render_memory(client, [])
        assert "current task: ship loop 245" in out
        assert "todos (2):" in out
        assert "[open] t1: wire dispatcher" in out
        assert "[in_progress] t2: tests" in out

    def test_show_lists_facts_and_decisions(self, client):
        client.task_memory.record_fact("repo_root", "/workspace/qwen-coder-mcp")
        client.task_memory.record_decision("avoid co-author trailer")
        out = tui._render_memory(client, [])
        assert "facts (1):" in out
        assert "repo_root: /workspace/qwen-coder-mcp" in out
        assert "decisions (1):" in out
        assert "avoid co-author trailer" in out


class TestRenderMemoryJson:
    def test_json_returns_parseable_snapshot(self, client):
        client.task_memory.set_current_task("X")
        client.task_memory.add_todo("t1", "do thing")
        out = tui._render_memory(client, ["--json"])
        snap = json.loads(out)
        assert snap["current_task"] == "X"
        assert any(t["id"] == "t1" for t in snap["todos"])

    def test_format_json_alias(self, client):
        client.task_memory.set_current_task("Y")
        out = tui._render_memory(client, ["--format=json"])
        snap = json.loads(out)
        assert snap["current_task"] == "Y"


class TestSetCurrentTask:
    def test_task_sets_current(self, client):
        out = tui._render_memory(client, ["task", "implement", "loop", "245"])
        assert "implement loop 245" in out
        assert client.task_memory.snapshot()["current_task"] == "implement loop 245"

    def test_task_without_args_returns_usage(self, client):
        out = tui._render_memory(client, ["task"])
        assert out.startswith("usage:")

    def test_task_with_blank_returns_usage(self, client):
        out = tui._render_memory(client, ["task", "   "])
        assert out.startswith("usage:")


class TestTodoSubcommands:
    def test_todo_add(self, client):
        out = tui._render_memory(client, ["todo", "add", "t1", "do", "thing"])
        assert "todo added: t1" in out
        snap = client.task_memory.snapshot()
        assert snap["todos"][0]["id"] == "t1"
        assert snap["todos"][0]["description"] == "do thing"
        assert snap["todos"][0]["status"] == "open"

    def test_todo_add_usage(self, client):
        assert tui._render_memory(client, ["todo", "add"]).startswith("usage:")
        assert tui._render_memory(client, ["todo", "add", "t1"]).startswith("usage:")

    def test_todo_done(self, client):
        client.task_memory.add_todo("t1", "x")
        out = tui._render_memory(client, ["todo", "done", "t1"])
        assert "t1 → done" in out
        assert client.task_memory.snapshot()["todos"][0]["status"] == "done"

    def test_todo_block(self, client):
        client.task_memory.add_todo("t1", "x")
        out = tui._render_memory(client, ["todo", "block", "t1"])
        assert "t1 → blocked" in out

    def test_todo_done_unknown_id(self, client):
        out = tui._render_memory(client, ["todo", "done", "nope"])
        assert "no such todo" in out

    def test_todo_del(self, client):
        client.task_memory.add_todo("t1", "x")
        out = tui._render_memory(client, ["todo", "del", "t1"])
        assert "todo deleted: t1" in out
        assert client.task_memory.snapshot()["todos"] == []

    def test_todo_unknown_action(self, client):
        out = tui._render_memory(client, ["todo", "frobnicate"])
        assert "unknown" in out

    def test_todo_no_action(self, client):
        out = tui._render_memory(client, ["todo"])
        assert out.startswith("usage:")


class TestFactsAndDecisions:
    def test_fact(self, client):
        out = tui._render_memory(client, ["fact", "key", "value", "extra"])
        assert "fact recorded: key=value extra" in out
        assert client.task_memory.snapshot()["facts"]["key"] == "value extra"

    def test_fact_usage(self, client):
        assert tui._render_memory(client, ["fact"]).startswith("usage:")
        assert tui._render_memory(client, ["fact", "k"]).startswith("usage:")

    def test_decision(self, client):
        out = tui._render_memory(client, ["decision", "use", "atomic", "writes"])
        assert "decision recorded: use atomic writes" in out
        assert client.task_memory.snapshot()["decisions"][0] == "use atomic writes"

    def test_decision_usage(self, client):
        assert tui._render_memory(client, ["decision"]).startswith("usage:")


class TestClearAndUnknown:
    def test_clear_wipes_state(self, client):
        client.task_memory.set_current_task("x")
        client.task_memory.add_todo("t1", "y")
        out = tui._render_memory(client, ["clear"])
        assert "cleared" in out
        snap = client.task_memory.snapshot()
        assert snap["current_task"] == ""
        assert snap["todos"] == []

    def test_unknown_subcommand(self, client):
        out = tui._render_memory(client, ["frobnicate"])
        assert "unknown" in out


class TestDispatcherIntegration:
    """Verify ``/memory ...`` routes through the slash-command dispatcher."""

    def test_slash_completion_lists_memory(self):
        completions = tui.slash_completions("/me")
        assert "/memory" in completions

    def test_help_text_mentions_memory(self):
        assert "/memory" in tui.HELP_TEXT

    def test_dispatcher_routes_show(self, tmp_path: Path):
        tm = TaskMemory(path=tmp_path / "s.json")
        tm.set_current_task("from dispatcher")
        client = _StubClient(tm)
        cfg = fs_tools.FsConfig(root=tmp_path)
        cmd = tui.parse_slash("/memory show")
        assert cmd is not None
        out, exit_flag = tui.dispatch_slash(cmd, client=client, fs_cfg=cfg, history=[])
        assert exit_flag is False
        assert "from dispatcher" in out

    def test_dispatcher_routes_task_set(self, tmp_path: Path):
        tm = TaskMemory(path=tmp_path / "s.json")
        client = _StubClient(tm)
        cfg = fs_tools.FsConfig(root=tmp_path)
        cmd = tui.parse_slash("/memory task ship loop 245")
        assert cmd is not None
        out, _ = tui.dispatch_slash(cmd, client=client, fs_cfg=cfg, history=[])
        assert "ship loop 245" in out
        assert tm.snapshot()["current_task"] == "ship loop 245"


class TestPersistence:
    def test_set_task_persists_to_disk(self, tmp_path: Path):
        path = tmp_path / "s.json"
        tm = TaskMemory(path=path)
        client = _StubClient(tm)
        tui._render_memory(client, ["task", "persisted"])
        # Reload fresh to confirm disk write
        tm2 = TaskMemory(path=path)
        assert tm2.snapshot()["current_task"] == "persisted"

    def test_clear_persists(self, tmp_path: Path):
        path = tmp_path / "s.json"
        tm = TaskMemory(path=path)
        tm.set_current_task("x")
        client = _StubClient(tm)
        tui._render_memory(client, ["clear"])
        tm2 = TaskMemory(path=path)
        assert tm2.snapshot()["current_task"] == ""
