"""Loop 207 — `/tokens --json --top K` exposes the heaviest messages.

Operators triaging context-bloat want a quick "which messages are
eating the budget?" answer without scrolling the full per_message
list. ``--top K`` adds a sorted-descending ``top`` field to the JSON
payload while leaving the no-flag and bare ``--json`` paths untouched.
"""
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
    # Index 1 is intentionally the heaviest, then 3, then 0, then 2.
    return [
        ChatMessage(role="system", content="x" * 40),         # ~10 tokens
        ChatMessage(role="user", content="x" * 400),          # ~100 tokens
        ChatMessage(role="assistant", content="ok"),          # ~1 token
        ChatMessage(role="user", content="x" * 200),          # ~50 tokens
    ]


class TestTopBasic:
    def test_top_field_present(self, fs_cfg: fs_tools.FsConfig) -> None:
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/tokens --json --top 2"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=_hist(),
        )
        data = json.loads(text)
        assert data["top_k"] == 2
        assert len(data["top"]) == 2

    def test_top_sorted_descending_by_tokens(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/tokens --json --top 3"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=_hist(),
        )
        data = json.loads(text)
        # Heaviest is index 1 (400 chars), then index 3 (200), then 0 (40).
        assert [e["index"] for e in data["top"]] == [1, 3, 0]
        toks = [e["tokens_estimated"] for e in data["top"]]
        assert toks == sorted(toks, reverse=True)

    def test_top_entries_have_full_shape(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/tokens --json --top 1"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=_hist(),
        )
        data = json.loads(text)
        entry = data["top"][0]
        assert set(entry.keys()) == {"index", "role", "tokens_estimated"}

    def test_top_eq_form_works(self, fs_cfg: fs_tools.FsConfig) -> None:
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/tokens --json --top=2"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=_hist(),
        )
        data = json.loads(text)
        assert data["top_k"] == 2
        assert len(data["top"]) == 2


class TestTopEdges:
    def test_top_zero_yields_empty_list(self, fs_cfg: fs_tools.FsConfig) -> None:
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/tokens --json --top 0"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=_hist(),
        )
        data = json.loads(text)
        assert data["top_k"] == 0
        assert data["top"] == []

    def test_top_larger_than_history_returns_all(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/tokens --json --top 999"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=_hist(),
        )
        data = json.loads(text)
        assert len(data["top"]) == len(_hist())

    def test_top_preserves_per_message_full_listing(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        # `top` is in addition to per_message, not a replacement.
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/tokens --json --top 1"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=_hist(),
        )
        data = json.loads(text)
        assert len(data["per_message"]) == len(_hist())


class TestTopErrors:
    def test_top_without_json_rejected(self, fs_cfg: fs_tools.FsConfig) -> None:
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/tokens --top 2"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=_hist(),
        )
        assert "--top requires --json" in text

    def test_top_negative_rejected(self, fs_cfg: fs_tools.FsConfig) -> None:
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/tokens --json --top -3"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=_hist(),
        )
        assert "non-negative" in text

    def test_top_non_integer_rejected(self, fs_cfg: fs_tools.FsConfig) -> None:
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/tokens --json --top abc"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=_hist(),
        )
        assert "not an integer" in text

    def test_top_missing_value_rejected(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/tokens --json --top"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=_hist(),
        )
        assert "usage" in text.lower()

    def test_unknown_arg_rejected(self, fs_cfg: fs_tools.FsConfig) -> None:
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/tokens --json --bogus"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=_hist(),
        )
        assert "unknown argument" in text


class TestBackwardsCompat:
    def test_no_args_unchanged(self, fs_cfg: fs_tools.FsConfig) -> None:
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/tokens"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=_hist(),
        )
        assert "tokens across" in text

    def test_bare_json_has_no_top_keys(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/tokens --json"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=_hist(),
        )
        data = json.loads(text)
        assert "top" not in data
        assert "top_k" not in data
