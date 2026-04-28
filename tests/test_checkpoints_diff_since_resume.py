"""Loop 195 — `/checkpoints diff --since-resume` auto-picks the
snapshot that `/resume` would load, so users don't have to count
indices by hand to preview a recovery."""
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


def _write_rotation(
    fs_cfg: fs_tools.FsConfig, name: str, history: list[ChatMessage]
) -> Path:
    p = fs_cfg.root / ".agent" / "checkpoints" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(agent_loop.serialize_agent_state(history)),
        encoding="utf-8",
    )
    return p


def test_no_checkpoint_returns_friendly_message(
    fs_cfg: fs_tools.FsConfig,
) -> None:
    out, _ = dispatch_slash(
        parse_slash("/checkpoints diff --since-resume"),
        client=None,  # type: ignore[arg-type]
        fs_cfg=fs_cfg,
        history=[_msg("user", "x")],
    )
    assert "/resume could load" in out


def test_picks_primary_when_present(fs_cfg: fs_tools.FsConfig) -> None:
    primary = _write_primary(fs_cfg, [_msg("user", "old")])
    out, _ = dispatch_slash(
        parse_slash("/checkpoints diff --since-resume"),
        client=None,  # type: ignore[arg-type]
        fs_cfg=fs_cfg,
        history=[_msg("user", "new")],
    )
    assert primary.name in out
    assert "changed=1" in out


def test_falls_back_to_rotation_when_primary_missing(
    fs_cfg: fs_tools.FsConfig,
) -> None:
    snap = _write_rotation(
        fs_cfg, "agent_state-2024.json", [_msg("user", "rot")]
    )
    out, _ = dispatch_slash(
        parse_slash("/checkpoints diff --since-resume"),
        client=None,  # type: ignore[arg-type]
        fs_cfg=fs_cfg,
        history=[_msg("user", "live")],
    )
    assert snap.name in out


def test_inline_flag_works_with_since_resume(
    fs_cfg: fs_tools.FsConfig,
) -> None:
    _write_primary(fs_cfg, [_msg("user", "alpha\nbravo")])
    out, _ = dispatch_slash(
        parse_slash("/checkpoints diff --since-resume --inline"),
        client=None,  # type: ignore[arg-type]
        fs_cfg=fs_cfg,
        history=[_msg("user", "alpha\nBRAVO")],
    )
    assert "-bravo" in out
    assert "+BRAVO" in out


def test_since_resume_no_history(fs_cfg: fs_tools.FsConfig) -> None:
    out, _ = dispatch_slash(
        parse_slash("/checkpoints diff --since-resume"),
        client=None,  # type: ignore[arg-type]
        fs_cfg=fs_cfg,
        history=None,
    )
    assert "no history available" in out


def test_usage_message_lists_since_resume(fs_cfg: fs_tools.FsConfig) -> None:
    out, _ = dispatch_slash(
        parse_slash("/checkpoints diff"),
        client=None,  # type: ignore[arg-type]
        fs_cfg=fs_cfg,
        history=[_msg("user", "x")],
    )
    assert "--since-resume" in out


def test_flag_order_independent(fs_cfg: fs_tools.FsConfig) -> None:
    _write_primary(fs_cfg, [_msg("user", "a")])
    out_a, _ = dispatch_slash(
        parse_slash("/checkpoints diff --since-resume --inline"),
        client=None,  # type: ignore[arg-type]
        fs_cfg=fs_cfg,
        history=[_msg("user", "b")],
    )
    out_b, _ = dispatch_slash(
        parse_slash("/checkpoints diff --inline --since-resume"),
        client=None,  # type: ignore[arg-type]
        fs_cfg=fs_cfg,
        history=[_msg("user", "b")],
    )
    assert out_a == out_b
