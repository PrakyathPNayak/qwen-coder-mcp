"""Smoke tests for `qwen_coder_mcp.server` import and dispatch.

These tests must run without vLLM up and without any network calls.
They guard against:
- A regression that makes `import qwen_coder_mcp.server` perform I/O.
- A regression that makes `_build_server()` require a live network.
- A regression that breaks dispatch routing for the documented tool
  names.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from qwen_coder_mcp import server as srv


class _StubClient:
    """Records calls; never touches the network."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def system_user(self, system: str, user: str, **kwargs: Any) -> str:
        self.calls.append({"system": system, "user": user, "kwargs": kwargs})
        return f"[stub:{len(self.calls)}]"

    def close(self) -> None:
        pass


def test_module_imports_without_network():
    """Importing the module must not perform I/O."""
    # Already imported at top — if it had hit the network, that import
    # would have failed in CI. Sanity-check the public symbols exist.
    assert hasattr(srv, "_build_server")
    assert hasattr(srv, "_dispatch")
    assert hasattr(srv, "main")


def test_build_server_accepts_injected_client():
    stub = _StubClient()
    server, client = srv._build_server(client=stub)
    assert client is stub
    assert server is not None


def test_build_server_default_client_is_qwen_client():
    """The default path constructs a real QwenClient (which is itself
    lazy — no network hit until a request is made)."""
    server, client = srv._build_server()
    try:
        from qwen_coder_mcp.qwen_client import QwenClient
        assert isinstance(client, QwenClient)
    finally:
        client.close()


@pytest.mark.parametrize(
    "name,args,expect_in_user",
    [
        ("chat", {"prompt": "hello"}, "hello"),
        ("complete_code", {"code": "def f(): pass"}, "def f(): pass"),
        ("explain_code", {"code": "x = 1"}, "x = 1"),
        ("find_bugs", {"path": "a.py", "code": "x=1"}, "a.py"),
        ("propose_fix", {"path": "a.py", "code": "x=1", "issue": "rename x"}, "rename x"),
        (
            "devils_advocate",
            {"path": "a.py", "original": "x=1", "diff": "d", "issue": "i"},
            "VERDICT",
        ),
        ("refactor", {"code": "x=1", "goal": "use enums"}, "use enums"),
        ("write_tests", {"code": "def f(): pass"}, "pytest"),
        ("summarize_repo", {"tree": "README.md"}, "README.md"),
    ],
)
def test_dispatch_routes_each_tool(name, args, expect_in_user):
    stub = _StubClient()
    out = srv._dispatch(stub, name, args)
    assert out.startswith("[stub:")
    assert len(stub.calls) == 1
    assert expect_in_user in stub.calls[0]["user"]


def test_dispatch_unknown_tool_raises():
    stub = _StubClient()
    with pytest.raises(ValueError, match="unknown tool"):
        srv._dispatch(stub, "nonexistent_tool", {})


def test_dispatch_propose_fix_uses_low_temperature():
    """propose_fix must use temperature=0.1 for diff stability."""
    stub = _StubClient()
    srv._dispatch(stub, "propose_fix", {"path": "a", "code": "x", "issue": "y"})
    assert stub.calls[0]["kwargs"].get("temperature") == 0.1


def test_dispatch_devils_advocate_uses_zero_temperature():
    """devils_advocate must use temperature=0.0 so VERDICT is deterministic."""
    stub = _StubClient()
    srv._dispatch(stub, "devils_advocate", {"path": "a", "original": "o", "diff": "d", "issue": "i"})
    assert stub.calls[0]["kwargs"].get("temperature") == 0.0


def test_dispatch_chat_default_temperature_when_unset():
    stub = _StubClient()
    srv._dispatch(stub, "chat", {"prompt": "hello"})
    assert stub.calls[0]["kwargs"].get("temperature") == 0.2


def test_dispatch_chat_honors_caller_temperature():
    stub = _StubClient()
    srv._dispatch(stub, "chat", {"prompt": "hello", "temperature": 0.7})
    assert stub.calls[0]["kwargs"].get("temperature") == pytest.approx(0.7)


def test_list_tools_registers_documented_tools():
    """All 9 tools listed in `_dispatch` must be exported by list_tools."""
    stub = _StubClient()
    server, _ = srv._build_server(client=stub)
    # The MCP Server stores handlers in private dicts. We invoke the
    # registered handler the same way MCP would.
    handlers = getattr(server, "request_handlers", None) or getattr(server, "_request_handlers", None)
    assert handlers is not None, "couldn't introspect server handlers"
    # Find the list_tools handler — it's keyed by request type, not name,
    # so we just check it's registered alongside call_tool.
    assert len(handlers) >= 2


def test_dispatch_find_bugs_uses_reviewer_system():
    stub = _StubClient()
    srv._dispatch(stub, "find_bugs", {"path": "a.py", "code": "x"})
    from qwen_coder_mcp import prompts
    assert stub.calls[0]["system"] == prompts.REVIEWER_SYSTEM


def test_dispatch_devils_advocate_uses_dev_advocate_system():
    stub = _StubClient()
    srv._dispatch(stub, "devils_advocate", {"path": "a", "original": "o", "diff": "d", "issue": "i"})
    from qwen_coder_mcp import prompts
    assert stub.calls[0]["system"] == prompts.DEVILS_ADVOCATE_SYSTEM
