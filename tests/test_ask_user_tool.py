"""Loop 284: ask_user tool tests.

The tool routes through a thread-local handler installed by the host
(TUI). When no handler is installed, the tool returns a clear marker
so the model can fall back. Tests exercise both branches.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from qwen_coder_mcp import agent_loop, fs_tools


@pytest.fixture
def cfg(tmp_path: Path) -> fs_tools.FsConfig:
    return fs_tools.FsConfig(root=tmp_path)


@pytest.fixture(autouse=True)
def _clear_handler():
    prev = agent_loop.set_ask_user_handler(None)
    yield
    agent_loop.set_ask_user_handler(prev)


def test_no_handler_returns_marker(cfg: fs_tools.FsConfig) -> None:
    out = agent_loop._tool_ask_user({"question": "ok?"}, cfg)
    assert "no interactive operator" in out.lower()


def test_missing_question_returns_error(cfg: fs_tools.FsConfig) -> None:
    out = agent_loop._tool_ask_user({}, cfg)
    assert out.startswith("error:")


def test_handler_free_form(cfg: fs_tools.FsConfig) -> None:
    seen: list[tuple[str, list[str]]] = []

    def handler(q: str, choices: list[str]) -> str:
        seen.append((q, list(choices)))
        return "  yes please  "

    agent_loop.set_ask_user_handler(handler)
    out = agent_loop._tool_ask_user({"question": "proceed?"}, cfg)
    assert out == "  yes please  "
    assert seen == [("proceed?", [])]


def test_handler_with_choices(cfg: fs_tools.FsConfig) -> None:
    captured: list[list[str]] = []

    def handler(q: str, choices: list[str]) -> str:
        captured.append(list(choices))
        return choices[0]

    agent_loop.set_ask_user_handler(handler)
    out = agent_loop._tool_ask_user(
        {"question": "pick one", "choices": ["a", "b", "c"]}, cfg
    )
    assert out == "a"
    assert captured == [["a", "b", "c"]]


def test_handler_filters_non_string_choices(cfg: fs_tools.FsConfig) -> None:
    captured: list[list[str]] = []

    def handler(q: str, choices: list[str]) -> str:
        captured.append(list(choices))
        return "ok"

    agent_loop.set_ask_user_handler(handler)
    agent_loop._tool_ask_user(
        {"question": "x", "choices": ["a", "", 5, None, "b"]}, cfg
    )
    assert captured == [["a", "b"]]


def test_handler_none_returns_canceled(cfg: fs_tools.FsConfig) -> None:
    agent_loop.set_ask_user_handler(lambda q, c: None)  # type: ignore[arg-type]
    out = agent_loop._tool_ask_user({"question": "?"}, cfg)
    assert out == "user_canceled"


def test_handler_exception_surfaces(cfg: fs_tools.FsConfig) -> None:
    def handler(q: str, c: list[str]) -> str:
        raise RuntimeError("boom")

    agent_loop.set_ask_user_handler(handler)
    out = agent_loop._tool_ask_user({"question": "?"}, cfg)
    assert out.startswith("error:") and "RuntimeError" in out and "boom" in out


def test_set_handler_returns_previous(cfg: fs_tools.FsConfig) -> None:
    h1 = lambda q, c: "1"  # noqa: E731
    h2 = lambda q, c: "2"  # noqa: E731
    prev = agent_loop.set_ask_user_handler(h1)
    assert prev is None
    prev2 = agent_loop.set_ask_user_handler(h2)
    assert prev2 is h1
    prev3 = agent_loop.set_ask_user_handler(None)
    assert prev3 is h2


def test_in_default_registry() -> None:
    assert "ask_user" in agent_loop.DEFAULT_TOOLS


def test_in_tool_blurbs() -> None:
    assert "ask_user" in agent_loop.TOOL_BLURBS
    assert "operator" in agent_loop.TOOL_BLURBS["ask_user"].lower()


def test_not_destructive() -> None:
    # ask_user prompts the human, doesn't modify state, so it doesn't
    # need to go through the destructive-tool confirm hook.
    assert "ask_user" not in agent_loop.DESTRUCTIVE_TOOLS


def test_thread_local_isolation(cfg: fs_tools.FsConfig) -> None:
    """Handlers installed in one thread don't leak into another."""
    import threading

    agent_loop.set_ask_user_handler(lambda q, c: "main-thread")
    other_seen: list[str] = []

    def worker():
        out = agent_loop._tool_ask_user({"question": "?"}, cfg)
        other_seen.append(out)

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert "no interactive operator" in other_seen[0].lower()
    # Main thread still sees its handler.
    assert agent_loop._tool_ask_user({"question": "?"}, cfg) == "main-thread"
