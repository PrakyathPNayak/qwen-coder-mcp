"""Tests for agent-state checkpointing helpers + the ``checkpoint``
hook on ``run_agent``."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from qwen_coder_mcp import agent_loop, fs_tools
from qwen_coder_mcp.agent_loop import (
    deserialize_agent_state,
    load_agent_checkpoint,
    run_agent,
    save_agent_checkpoint,
    serialize_agent_state,
)
from qwen_coder_mcp.qwen_client import ChatMessage


def _fs_cfg(tmp_path: Path) -> fs_tools.FsConfig:
    return fs_tools.FsConfig(root=tmp_path)


class _ScriptedClient:
    """Minimal fake QwenClient that returns a queued sequence of
    replies. Each ``chat`` call pops the next reply."""

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)

    def chat(self, _history: list[ChatMessage]) -> str:
        if not self._replies:
            return "done"
        return self._replies.pop(0)


class TestSerializeRoundTrip:
    def test_round_trip_preserves_messages(self) -> None:
        h = [
            ChatMessage(role="system", content="be helpful"),
            ChatMessage(role="user", content="hi"),
            ChatMessage(role="assistant", content="hello"),
        ]
        out = deserialize_agent_state(serialize_agent_state(h))
        assert [(m.role, m.content) for m in out] == [
            ("system", "be helpful"),
            ("user", "hi"),
            ("assistant", "hello"),
        ]

    def test_serialize_emits_version(self) -> None:
        out = serialize_agent_state([])
        assert out["version"] == 1
        assert out["messages"] == []

    def test_deserialize_skips_malformed_entries(self) -> None:
        data = {
            "version": 1,
            "messages": [
                {"role": "user", "content": "ok"},
                {"role": 7, "content": "bad role"},
                {"role": "assistant"},  # missing content
                "not a dict",
                {"role": "assistant", "content": "fine"},
            ],
        }
        out = deserialize_agent_state(data)
        assert [(m.role, m.content) for m in out] == [
            ("user", "ok"),
            ("assistant", "fine"),
        ]


class TestSaveLoad:
    def test_save_then_load_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        h = [ChatMessage(role="user", content="hi"), ChatMessage(role="assistant", content="hello")]
        save_agent_checkpoint(path, h)
        out = load_agent_checkpoint(path)
        assert [(m.role, m.content) for m in out] == [
            ("user", "hi"),
            ("assistant", "hello"),
        ]

    def test_save_creates_parent_dirs(self, tmp_path: Path) -> None:
        path = tmp_path / "deep" / "nested" / "state.json"
        save_agent_checkpoint(path, [ChatMessage(role="user", content="x")])
        assert path.exists()

    def test_save_is_atomic_no_tmp_left_behind(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        save_agent_checkpoint(path, [ChatMessage(role="user", content="x")])
        # The .tmp sibling must not survive a successful write.
        assert not (tmp_path / "state.json.tmp").exists()
        # And the JSON on disk must be valid.
        json.loads(path.read_text(encoding="utf-8"))

    def test_load_missing_path_returns_empty(self, tmp_path: Path) -> None:
        assert load_agent_checkpoint(tmp_path / "no.json") == []

    def test_load_corrupt_json_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        path.write_text("not json {{{", encoding="utf-8")
        assert load_agent_checkpoint(path) == []

    def test_load_non_object_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        assert load_agent_checkpoint(path) == []


class TestRunAgentCheckpointHook:
    def test_checkpoint_called_after_each_tool_step(
        self, tmp_path: Path
    ) -> None:
        # Step 1: model emits a tool_call. Step 2: model emits a final reply.
        replies = [
            '<tool_call>{"name": "fs_list", "args": {"path": "."}}</tool_call>',
            "all done",
        ]
        client = _ScriptedClient(replies)
        seen: list[tuple[int, int]] = []

        def cb(hist: list[ChatMessage], step: int) -> None:
            seen.append((step, len(hist)))

        events = list(
            run_agent(
                history=[],
                user_text="please",
                client=client,
                fs_cfg=_fs_cfg(tmp_path),
                stream=False,
                checkpoint=cb,
            )
        )
        assert any(ev.kind == "final" for ev in events)
        # Exactly one tool round-trip → checkpoint fires once with step==1.
        assert seen == [(1, len(seen and seen[0]) and seen[0][1] or seen[0][1])] or len(seen) == 1
        assert seen[0][0] == 1

    def test_checkpoint_failure_does_not_abort_turn(
        self, tmp_path: Path
    ) -> None:
        replies = [
            '<tool_call>{"name": "fs_list", "args": {"path": "."}}</tool_call>',
            "ok",
        ]
        client = _ScriptedClient(replies)

        def boom(_h: list[ChatMessage], _s: int) -> None:
            raise RuntimeError("disk on fire")

        events = list(
            run_agent(
                history=[],
                user_text="go",
                client=client,
                fs_cfg=_fs_cfg(tmp_path),
                stream=False,
                checkpoint=boom,
            )
        )
        # Despite the failing checkpoint, the agent must reach a final.
        assert any(ev.kind == "final" for ev in events)

    def test_checkpoint_omitted_when_no_tools_called(
        self, tmp_path: Path
    ) -> None:
        # Single reply with no tool_call → loop terminates immediately
        # before the tool-result branch where the hook fires.
        client = _ScriptedClient(["just a plain answer"])
        seen: list[int] = []

        def cb(_h: list[ChatMessage], step: int) -> None:
            seen.append(step)

        list(
            run_agent(
                history=[],
                user_text="hi",
                client=client,
                fs_cfg=_fs_cfg(tmp_path),
                stream=False,
                checkpoint=cb,
            )
        )
        assert seen == []
