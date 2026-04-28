"""Loop 204 — `/checkpoints diff` and `/resume --preview` use the
auto-width preview from loop 203."""
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


def _write_snapshot(target: Path, history: list[ChatMessage]) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(agent_loop.serialize_agent_state(history)),
        encoding="utf-8",
    )
    return target


LONG = "alpha bravo charlie delta echo foxtrot golf hotel " * 8


class TestDispatcherWiresAutoWidth:
    def test_diff_n_uses_terminal_width(
        self, fs_cfg: fs_tools.FsConfig, monkeypatch
    ) -> None:
        import shutil

        monkeypatch.setattr(
            shutil,
            "get_terminal_size",
            lambda default=(80, 24): shutil.os.terminal_size((220, 30)),
        )
        snap_dir = fs_cfg.root / ".agent" / "checkpoints"
        _write_snapshot(
            snap_dir / "agent_state-2024.json",
            [_msg("user", LONG), _msg("assistant", "yo")],
        )
        history = [_msg("user", LONG[:-3]), _msg("assistant", "yo")]
        out, _ = dispatch_slash(
            parse_slash("/checkpoints diff 1"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=history,
        )
        # Find the ~ row.
        row = next(line for line in out.splitlines() if line.startswith("~"))
        # 220-col terminal → preview much longer than the legacy 60.
        # Be conservative: just check it's substantially over 60.
        assert len(row) > 100

    def test_diff_n_narrow_terminal_clamped(
        self, fs_cfg: fs_tools.FsConfig, monkeypatch
    ) -> None:
        import shutil

        monkeypatch.setattr(
            shutil,
            "get_terminal_size",
            lambda default=(80, 24): shutil.os.terminal_size((40, 24)),
        )
        snap_dir = fs_cfg.root / ".agent" / "checkpoints"
        _write_snapshot(
            snap_dir / "agent_state-2024.json",
            [_msg("user", LONG)],
        )
        history = [_msg("user", LONG[:-3])]
        out, _ = dispatch_slash(
            parse_slash("/checkpoints diff 1"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=history,
        )
        row = next(line for line in out.splitlines() if line.startswith("~"))
        # Narrow terminal → preview clamped, ellipsis present.
        assert "…" in row

    def test_resume_preview_uses_terminal_width(
        self, fs_cfg: fs_tools.FsConfig, monkeypatch
    ) -> None:
        import shutil

        monkeypatch.setattr(
            shutil,
            "get_terminal_size",
            lambda default=(80, 24): shutil.os.terminal_size((180, 30)),
        )
        target = fs_cfg.root / ".agent" / "agent_state.json"
        _write_snapshot(
            target,
            [_msg("user", LONG)],
        )
        history = [_msg("user", LONG[:-3])]
        out, _ = dispatch_slash(
            parse_slash("/resume --preview"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=history,
        )
        row = next(line for line in out.splitlines() if line.startswith("~"))
        assert len(row) > 90

    def test_diff_since_resume_uses_terminal_width(
        self, fs_cfg: fs_tools.FsConfig, monkeypatch
    ) -> None:
        import shutil

        monkeypatch.setattr(
            shutil,
            "get_terminal_size",
            lambda default=(80, 24): shutil.os.terminal_size((180, 30)),
        )
        target = fs_cfg.root / ".agent" / "agent_state.json"
        _write_snapshot(target, [_msg("user", LONG)])
        history = [_msg("user", LONG[:-3])]
        out, _ = dispatch_slash(
            parse_slash("/checkpoints diff --since-resume"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=history,
        )
        row = next(line for line in out.splitlines() if line.startswith("~"))
        assert len(row) > 90
