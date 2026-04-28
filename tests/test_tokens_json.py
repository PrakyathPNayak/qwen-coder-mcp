"""Loop 201 — `/tokens --json` rounds out the JSON-export trilogy."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from qwen_coder_mcp import fs_tools, tui
from qwen_coder_mcp.tui import ChatMessage


@pytest.fixture()
def fs_cfg(tmp_path: Path) -> fs_tools.FsConfig:
    return fs_tools.FsConfig(root=tmp_path)


def _hist() -> list[ChatMessage]:
    return [
        ChatMessage(role="system", content="be helpful"),
        ChatMessage(role="user", content="hello there"),
        ChatMessage(role="assistant", content="hi"),
    ]


class TestTokensJson:
    def test_parses_as_json(self, fs_cfg: fs_tools.FsConfig) -> None:
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/tokens --json"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=_hist(),
        )
        data = json.loads(text)
        assert data["messages"] == 3
        assert data["tokens_estimated"] > 0
        assert data["estimator"] == "four-chars-per-token"
        assert len(data["per_message"]) == 3

    def test_per_message_shape(self, fs_cfg: fs_tools.FsConfig) -> None:
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/tokens --json"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=_hist(),
        )
        data = json.loads(text)
        first = data["per_message"][0]
        assert set(first.keys()) == {"index", "role", "tokens_estimated"}
        assert first["index"] == 0
        assert first["role"] == "system"
        # Per-message totals sum to the headline number.
        per_total = sum(m["tokens_estimated"] for m in data["per_message"])
        assert per_total == data["tokens_estimated"]

    def test_format_json_alias(self, fs_cfg: fs_tools.FsConfig) -> None:
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/tokens --format=json"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=_hist(),
        )
        json.loads(text)  # parses

    def test_empty_history(self, fs_cfg: fs_tools.FsConfig) -> None:
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/tokens --json"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[],
        )
        data = json.loads(text)
        assert data["messages"] == 0
        assert data["tokens_estimated"] == 0
        assert data["per_message"] == []

    def test_no_history_still_text(self, fs_cfg: fs_tools.FsConfig) -> None:
        # The `history is None` guard fires before flag parsing —
        # downstream tooling using --json must still notice this.
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/tokens --json"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=None,
        )
        assert "no history available" in text

    def test_no_flag_keeps_text(self, fs_cfg: fs_tools.FsConfig) -> None:
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/tokens"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=_hist(),
        )
        with pytest.raises(json.JSONDecodeError):
            json.loads(text)
        assert "tokens across" in text
