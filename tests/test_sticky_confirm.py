"""Loop 273 — sticky-approve confirm hooks (Copilot-style)."""
from __future__ import annotations

import pytest

from qwen_coder_mcp.agent_loop import (
    ToolCall,
    always_allow,
    always_deny,
    make_sticky_confirm,
)


def _call(name="run_shell", args=None):
    return ToolCall(name=name, args=args or {"cmd": "echo hi"}, raw="")


class TestDenyAndAllow:
    def test_always_allow(self):
        assert always_allow(_call()) is True

    def test_always_deny(self):
        assert always_deny(_call()) is False


class TestStickyPerTool:
    def test_first_approve_then_silent(self):
        prompts = []

        def inner(c):
            prompts.append(c.name)
            return True

        sticky = make_sticky_confirm(inner)
        assert sticky(_call(name="run_shell")) is True
        assert sticky(_call(name="run_shell", args={"cmd": "ls"})) is True
        assert sticky(_call(name="run_shell", args={"cmd": "pwd"})) is True
        assert prompts == ["run_shell"]  # only first prompted

    def test_deny_does_not_stick(self):
        decisions = iter([False, True])

        def inner(c):
            return next(decisions)

        sticky = make_sticky_confirm(inner)
        assert sticky(_call()) is False
        # User said no the first time -- they should be prompted again.
        assert sticky(_call()) is True

    def test_per_tool_isolation(self):
        prompts = []

        def inner(c):
            prompts.append(c.name)
            return True

        sticky = make_sticky_confirm(inner)
        sticky(_call(name="run_shell"))
        sticky(_call(name="fs_write"))
        sticky(_call(name="run_shell"))
        assert prompts == ["run_shell", "fs_write"]


class TestStickyPerArgs:
    def test_repeat_same_args_silent(self):
        prompts = []

        def inner(c):
            prompts.append((c.name, c.args.get("cmd")))
            return True

        sticky = make_sticky_confirm(inner, sticky_per_tool=False)
        sticky(_call(args={"cmd": "ls"}))
        sticky(_call(args={"cmd": "ls"}))
        assert len(prompts) == 1

    def test_different_args_re_prompt(self):
        prompts = []

        def inner(c):
            prompts.append(c.args.get("cmd"))
            return True

        sticky = make_sticky_confirm(inner, sticky_per_tool=False)
        sticky(_call(args={"cmd": "ls"}))
        sticky(_call(args={"cmd": "pwd"}))
        sticky(_call(args={"cmd": "ls"}))  # same as first → silent
        assert prompts == ["ls", "pwd"]


class TestExceptionTolerance:
    def test_unhashable_args_dont_crash(self):
        # json.dumps fallback path: if args contain non-JSON-serialisable
        # objects, the key falls back to str() and the wrapper still works.
        sticky = make_sticky_confirm(lambda c: True, sticky_per_tool=False)
        weird = ToolCall(name="x", args={"obj": object()}, raw="")
        # Should not raise.
        assert sticky(weird) is True
        assert sticky(weird) is True
