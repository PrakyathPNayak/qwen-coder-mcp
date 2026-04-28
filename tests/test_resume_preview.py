"""Loop 197 — `/resume --preview` runs the recovery diff without mutating
the live chat history."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from qwen_coder_mcp import agent_loop, fs_tools
from qwen_coder_mcp.tui import ChatMessage, dispatch_slash, parse_slash


@pytest.fixture()
def fs_cfg(tmp_path: Path) -> fs_tools.FsConfig:
    return fs_tools.FsConfig(root=tmp_path)


def _msg(role: str, content: str) -> ChatMessage:
    return ChatMessage(role=role, content=content)


def _write_primary(fs_cfg: fs_tools.FsConfig, history: list[ChatMessage]) -> Path:
    p = fs_cfg.root / ".agent" / "agent_state.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(agent_loop.serialize_agent_state(history)),
        encoding="utf-8",
    )
    return p


def test_preview_does_not_mutate_history(fs_cfg: fs_tools.FsConfig) -> None:
    _write_primary(fs_cfg, [_msg("user", "old")])
    history = [_msg("user", "live")]
    snapshot = list(history)
    out, _ = dispatch_slash(
        parse_slash("/resume --preview"),
        client=None,  # type: ignore[arg-type]
        fs_cfg=fs_cfg,
        history=history,
    )
    assert "preview only" in out
    assert "history unchanged" in out
    assert "changed=1" in out
    # The live history list is the same object, same contents.
    assert history == snapshot


def test_dry_run_alias(fs_cfg: fs_tools.FsConfig) -> None:
    _write_primary(fs_cfg, [_msg("user", "old")])
    history = [_msg("user", "live")]
    out, _ = dispatch_slash(
        parse_slash("/resume --dry-run"),
        client=None,  # type: ignore[arg-type]
        fs_cfg=fs_cfg,
        history=history,
    )
    assert "preview only" in out
    assert history == [_msg("user", "live")]


def test_preview_no_checkpoint(fs_cfg: fs_tools.FsConfig) -> None:
    history = [_msg("user", "live")]
    out, _ = dispatch_slash(
        parse_slash("/resume --preview"),
        client=None,  # type: ignore[arg-type]
        fs_cfg=fs_cfg,
        history=history,
    )
    assert "no checkpoint found" in out
    assert history == [_msg("user", "live")]


def test_resume_without_preview_still_loads(
    fs_cfg: fs_tools.FsConfig,
) -> None:
    _write_primary(fs_cfg, [_msg("user", "snap"), _msg("assistant", "yo")])
    history = [_msg("user", "live")]
    out, _ = dispatch_slash(
        parse_slash("/resume"),
        client=None,  # type: ignore[arg-type]
        fs_cfg=fs_cfg,
        history=history,
    )
    assert "resumed 2" in out
    # In-place mutation actually happened.
    assert len(history) == 2
    assert history[0].content == "snap"


def test_preview_with_no_history_object(
    fs_cfg: fs_tools.FsConfig,
) -> None:
    out, _ = dispatch_slash(
        parse_slash("/resume --preview"),
        client=None,  # type: ignore[arg-type]
        fs_cfg=fs_cfg,
        history=None,
    )
    assert "no history available" in out
