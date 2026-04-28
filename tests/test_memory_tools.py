"""Tests for the loop-246 memory tools exposed to the agent loop.

When ``QwenClient.task_memory`` is attached, ``run_agent`` merges the
``build_memory_tools(memory)`` registry into the active toolset so the
model can self-manage memory via tool calls. These tests pin:

  * each tool's happy path mutates the bound TaskMemory correctly
  * each tool's missing-arg path returns a usable error string
  * ``run_tool`` dispatches into the bound closures
  * the protocol doc is appended to the system prompt only when memory
    is present (non-leaky)
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from qwen_coder_mcp import agent_loop, fs_tools
from qwen_coder_mcp.agent_loop import (
    DEFAULT_TOOLS,
    MEMORY_TOOL_NAMES,
    MEMORY_TOOL_PROTOCOL_DOC,
    ToolCall,
    build_memory_tools,
    run_tool,
)
from qwen_coder_mcp.task_memory import TaskMemory


@pytest.fixture
def memory(tmp_path: Path) -> TaskMemory:
    return TaskMemory(path=tmp_path / "state.json")


@pytest.fixture
def cfg(tmp_path: Path) -> fs_tools.FsConfig:
    return fs_tools.FsConfig(root=tmp_path)


@pytest.fixture
def tools(memory: TaskMemory) -> dict:
    return build_memory_tools(memory)


class TestBuildMemoryTools:
    def test_returns_empty_for_none_memory(self):
        assert build_memory_tools(None) == {}

    def test_registry_covers_all_documented_names(self, tools):
        assert set(tools.keys()) == set(MEMORY_TOOL_NAMES)

    def test_protocol_doc_lists_each_tool(self):
        for name in MEMORY_TOOL_NAMES:
            assert name in MEMORY_TOOL_PROTOCOL_DOC


class TestSetCurrentTask:
    def test_happy_path(self, tools, cfg, memory):
        out = tools["set_current_task"]({"description": "ship loop 246"}, cfg)
        assert "ok" in out
        assert memory.snapshot()["current_task"] == "ship loop 246"

    def test_missing_arg(self, tools, cfg):
        assert tools["set_current_task"]({}, cfg).startswith("error:")

    def test_blank_arg(self, tools, cfg):
        assert tools["set_current_task"]({"description": "  "}, cfg).startswith("error:")


class TestAddTodo:
    def test_happy_path_default_status(self, tools, cfg, memory):
        out = tools["add_todo"]({"id": "t1", "description": "do thing"}, cfg)
        assert "ok" in out
        snap = memory.snapshot()
        assert snap["todos"][0]["id"] == "t1"
        assert snap["todos"][0]["status"] == "open"

    def test_explicit_status(self, tools, cfg, memory):
        tools["add_todo"](
            {"id": "t1", "description": "x", "status": "in_progress"}, cfg
        )
        assert memory.snapshot()["todos"][0]["status"] == "in_progress"

    def test_missing_id(self, tools, cfg):
        out = tools["add_todo"]({"description": "x"}, cfg)
        assert out.startswith("error:")

    def test_missing_description(self, tools, cfg):
        out = tools["add_todo"]({"id": "t1"}, cfg)
        assert out.startswith("error:")


class TestUpdateAndCompleteTodo:
    def test_update_status(self, tools, cfg, memory):
        memory.add_todo("t1", "x")
        out = tools["update_todo"]({"id": "t1", "status": "in_progress"}, cfg)
        assert "ok" in out
        assert memory.snapshot()["todos"][0]["status"] == "in_progress"

    def test_update_unknown(self, tools, cfg):
        out = tools["update_todo"]({"id": "nope", "status": "done"}, cfg)
        assert "no such todo" in out

    def test_complete(self, tools, cfg, memory):
        memory.add_todo("t1", "x")
        out = tools["complete_todo"]({"id": "t1"}, cfg)
        assert "done" in out
        assert memory.snapshot()["todos"][0]["status"] == "done"

    def test_complete_unknown(self, tools, cfg):
        out = tools["complete_todo"]({"id": "nope"}, cfg)
        assert "no such todo" in out

    def test_remove(self, tools, cfg, memory):
        memory.add_todo("t1", "x")
        out = tools["remove_todo"]({"id": "t1"}, cfg)
        assert "ok" in out
        assert memory.snapshot()["todos"] == []

    def test_remove_unknown(self, tools, cfg):
        out = tools["remove_todo"]({"id": "nope"}, cfg)
        assert "no such todo" in out


class TestFactsAndDecisions:
    def test_record_fact(self, tools, cfg, memory):
        out = tools["record_fact"]({"key": "k", "value": "v"}, cfg)
        assert "ok" in out
        assert memory.snapshot()["facts"]["k"] == "v"

    def test_record_fact_missing_value(self, tools, cfg):
        assert tools["record_fact"]({"key": "k"}, cfg).startswith("error:")

    def test_record_decision(self, tools, cfg, memory):
        out = tools["record_decision"]({"text": "use atomic writes"}, cfg)
        assert "ok" in out
        assert memory.snapshot()["decisions"][0] == "use atomic writes"

    def test_record_decision_missing(self, tools, cfg):
        assert tools["record_decision"]({}, cfg).startswith("error:")


class TestRecallState:
    def test_recall_returns_json_snapshot(self, tools, cfg, memory):
        memory.set_current_task("X")
        memory.add_todo("t1", "y")
        out = tools["recall_state"]({}, cfg)
        snap = json.loads(out)
        assert snap["current_task"] == "X"
        assert any(t["id"] == "t1" for t in snap["todos"])

    def test_recall_empty_memory(self, tools, cfg):
        out = tools["recall_state"]({}, cfg)
        snap = json.loads(out)
        assert snap["current_task"] == ""
        assert snap["todos"] == []


class TestRunToolDispatch:
    """Verify the loop's run_tool dispatcher routes memory tool calls."""

    def test_dispatch_set_current_task(self, tools, cfg, memory):
        merged = {**DEFAULT_TOOLS, **tools}
        result = run_tool(
            ToolCall(name="set_current_task", args={"description": "via dispatch"}, raw=""),
            fs_cfg=cfg,
            tools=merged,
        )
        assert result.error is False
        assert "via dispatch" in result.output or "ok" in result.output
        assert memory.snapshot()["current_task"] == "via dispatch"

    def test_dispatch_missing_arg_is_not_an_exception(self, tools, cfg):
        merged = {**DEFAULT_TOOLS, **tools}
        result = run_tool(
            ToolCall(name="add_todo", args={"id": "t1"}, raw=""),
            fs_cfg=cfg,
            tools=merged,
        )
        # The tool returns a string (truthy) starting with "error:", but
        # ToolResult.error stays False because the function did not raise.
        assert result.error is False
        assert "missing required arg" in result.output

    def test_dispatch_unknown_tool(self, tools, cfg):
        merged = {**DEFAULT_TOOLS, **tools}
        result = run_tool(
            ToolCall(name="nonexistent", args={}, raw=""),
            fs_cfg=cfg,
            tools=merged,
        )
        assert result.error is True


class TestRunAgentMergesMemoryTools:
    """End-to-end-ish: run_agent should merge memory tools when the client
    has a task_memory attached, and append the memory protocol doc."""

    def _mock_client(self, *, memory, replies):
        """Minimal QwenClient stand-in with chat() and optional task_memory."""

        class _C:
            def __init__(self):
                self.task_memory = memory
                self._replies = list(replies)

            def chat(self, history, **_kw):
                if not self._replies:
                    return ""
                return self._replies.pop(0)

        return _C()

    def test_memory_tools_invocable_in_agent_turn(self, memory, cfg):
        # First reply asks the model to set the task; second reply ends.
        reply1 = (
            'I will record this.\n'
            '<tool_call>{"name": "set_current_task", '
            '"args": {"description": "from agent turn"}}</tool_call>'
        )
        reply2 = "Done."
        client = self._mock_client(memory=memory, replies=[reply1, reply2])

        events = list(
            agent_loop.run_agent(
                history=[],
                user_text="please set the task",
                client=client,
                fs_cfg=cfg,
                stream=False,
                max_steps=4,
            )
        )
        kinds = [e.kind for e in events]
        assert "final" in kinds
        # Memory must have been mutated by the tool call.
        assert memory.snapshot()["current_task"] == "from agent turn"

    def test_protocol_doc_only_present_when_memory_attached(self, cfg):
        client = self._mock_client(memory=None, replies=["just answering"])
        history: list = []
        list(
            agent_loop.run_agent(
                history=history,
                user_text="hi",
                client=client,
                fs_cfg=cfg,
                stream=False,
                max_steps=2,
            )
        )
        assert history[0].role == "system"
        # Without memory, the memory blurb must NOT leak into the system prompt.
        assert "Memory tools" not in history[0].content

    def test_protocol_doc_present_when_memory_attached(self, memory, cfg):
        client = self._mock_client(memory=memory, replies=["ok"])
        history: list = []
        list(
            agent_loop.run_agent(
                history=history,
                user_text="hi",
                client=client,
                fs_cfg=cfg,
                stream=False,
                max_steps=2,
            )
        )
        assert history[0].role == "system"
        assert "Memory tools" in history[0].content
        assert "set_current_task" in history[0].content


# ============================================================================
# Loop 248 — run_agent auto-seeds current_task from user_text
# ============================================================================
class TestAutoSeedCurrentTask:
    """When a TaskMemory is attached, run_agent must record the user's
    request as current_task on every turn so the model literally cannot
    forget it. Skips empty prompts. Truncates very long prompts to keep
    the auto-injected system block compact."""

    def _mock_client(self, *, memory, replies):
        class _C:
            def __init__(self):
                self.task_memory = memory
                self._replies = list(replies)

            def chat(self, history, **_kw):
                if not self._replies:
                    return ""
                return self._replies.pop(0)

        return _C()

    def test_seeds_current_task_from_user_text(self, memory, cfg):
        client = self._mock_client(memory=memory, replies=["sure"])
        list(
            agent_loop.run_agent(
                history=[],
                user_text="implement loop 248 auto-seed",
                client=client,
                fs_cfg=cfg,
                stream=False,
                max_steps=2,
            )
        )
        assert memory.snapshot()["current_task"] == "implement loop 248 auto-seed"

    def test_overwrites_existing_current_task(self, memory, cfg):
        memory.set_current_task("stale task from earlier session")
        client = self._mock_client(memory=memory, replies=["ok"])
        list(
            agent_loop.run_agent(
                history=[],
                user_text="actually do this thing instead",
                client=client,
                fs_cfg=cfg,
                stream=False,
                max_steps=2,
            )
        )
        assert memory.snapshot()["current_task"] == "actually do this thing instead"

    def test_truncates_very_long_prompts(self, memory, cfg):
        client = self._mock_client(memory=memory, replies=["ok"])
        long_prompt = "x" * 500
        list(
            agent_loop.run_agent(
                history=[],
                user_text=long_prompt,
                client=client,
                fs_cfg=cfg,
                stream=False,
                max_steps=2,
            )
        )
        ct = memory.snapshot()["current_task"]
        assert len(ct) <= 240
        assert ct.endswith("...")

    def test_skips_empty_prompts(self, memory, cfg):
        memory.set_current_task("preserved")
        client = self._mock_client(memory=memory, replies=["ok"])
        list(
            agent_loop.run_agent(
                history=[],
                user_text="   ",
                client=client,
                fs_cfg=cfg,
                stream=False,
                max_steps=2,
            )
        )
        # Empty user_text must NOT clobber an existing task.
        assert memory.snapshot()["current_task"] == "preserved"

    def test_no_memory_attached_is_noop(self, cfg):
        # Sanity: when no memory, the auto-seed path is skipped silently.
        class _C:
            task_memory = None

            def chat(self, history, **_kw):
                return "ok"

        client = _C()
        # Must not raise.
        list(
            agent_loop.run_agent(
                history=[],
                user_text="anything",
                client=client,
                fs_cfg=cfg,
                stream=False,
                max_steps=2,
            )
        )

    def test_memory_failure_does_not_break_turn(self, cfg):
        """If set_current_task raises, the turn must still complete."""

        class _BoomMemory:
            def set_current_task(self, _desc):
                raise RuntimeError("boom")

            def snapshot(self):
                return {"current_task": "", "todos": [], "facts": {}, "decisions": []}

            def is_empty(self):
                return True

            def to_system_prompt(self):
                return ""

        class _C:
            task_memory = _BoomMemory()

            def chat(self, history, **_kw):
                return "final"

        client = _C()
        events = list(
            agent_loop.run_agent(
                history=[],
                user_text="hi",
                client=client,
                fs_cfg=cfg,
                stream=False,
                max_steps=2,
            )
        )
        assert any(e.kind == "final" for e in events)
