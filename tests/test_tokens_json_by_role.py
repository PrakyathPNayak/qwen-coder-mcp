"""Loop 210 — `/tokens --json --top K --by-role` buckets the
heaviest messages per role. Useful when an operator wants to see
which user message AND which assistant message AND which system
message are heaviest, separately."""
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
    # Mixed roles, varying weights.
    return [
        ChatMessage(role="system", content="x" * 40),       # idx 0, ~10
        ChatMessage(role="user", content="x" * 400),        # idx 1, ~100
        ChatMessage(role="assistant", content="ok"),        # idx 2, ~1
        ChatMessage(role="user", content="x" * 200),        # idx 3, ~50
        ChatMessage(role="assistant", content="x" * 300),   # idx 4, ~75
        ChatMessage(role="user", content="x" * 100),        # idx 5, ~25
    ]


class TestByRoleBasic:
    def test_top_by_role_keys_match_history_roles(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/tokens --json --top 1 --by-role"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=_hist(),
        )
        data = json.loads(text)
        assert "top_by_role" in data
        assert set(data["top_by_role"].keys()) == {"system", "user", "assistant"}

    def test_per_role_top_picks_heaviest(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/tokens --json --top 1 --by-role"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=_hist(),
        )
        data = json.loads(text)
        # Heaviest per role:
        # user -> idx 1 (~100), assistant -> idx 4 (~75), system -> idx 0 (~10).
        assert [e["index"] for e in data["top_by_role"]["user"]] == [1]
        assert [e["index"] for e in data["top_by_role"]["assistant"]] == [4]
        assert [e["index"] for e in data["top_by_role"]["system"]] == [0]

    def test_top_2_per_role(self, fs_cfg: fs_tools.FsConfig) -> None:
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/tokens --json --top 2 --by-role"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=_hist(),
        )
        data = json.loads(text)
        # user has 3 messages -> top 2 are idx 1 (~100) and idx 3 (~50).
        assert [e["index"] for e in data["top_by_role"]["user"]] == [1, 3]
        # assistant has 2 messages -> both included, sorted desc: idx 4, 2.
        assert [e["index"] for e in data["top_by_role"]["assistant"]] == [4, 2]
        # system has 1 -> just idx 0.
        assert [e["index"] for e in data["top_by_role"]["system"]] == [0]

    def test_top_k_echoed(self, fs_cfg: fs_tools.FsConfig) -> None:
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/tokens --json --top 3 --by-role"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=_hist(),
        )
        data = json.loads(text)
        assert data["top_k"] == 3


class TestByRoleErrors:
    def test_by_role_without_json(self, fs_cfg: fs_tools.FsConfig) -> None:
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/tokens --by-role --top 2"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=_hist(),
        )
        # --top requires --json fires first; either error is acceptable.
        assert "requires --json" in text

    def test_by_role_without_top(self, fs_cfg: fs_tools.FsConfig) -> None:
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/tokens --json --by-role"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=_hist(),
        )
        assert "--by-role requires --top" in text


class TestByRoleSuppressesPlainTop:
    def test_top_by_role_replaces_top_field(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        # When --by-role is given, the plain flat `top` list is NOT
        # emitted (avoid double-encoding the same data).
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/tokens --json --top 1 --by-role"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=_hist(),
        )
        data = json.loads(text)
        assert "top" not in data
        assert "top_by_role" in data


class TestBackwardsCompat:
    def test_plain_top_unchanged(self, fs_cfg: fs_tools.FsConfig) -> None:
        # Without --by-role, the plain `top` field still appears.
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/tokens --json --top 2"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=_hist(),
        )
        data = json.loads(text)
        assert "top" in data
        assert "top_by_role" not in data

    def test_bare_json_unchanged(self, fs_cfg: fs_tools.FsConfig) -> None:
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/tokens --json"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=_hist(),
        )
        data = json.loads(text)
        assert "top" not in data
        assert "top_by_role" not in data
        assert "top_k" not in data
