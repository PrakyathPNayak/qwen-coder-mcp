"""Loop 198 — `/lat --json` emits the ring buffer as JSON for
downstream tooling."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from qwen_coder_mcp import fs_tools
from qwen_coder_mcp.tui import (
    TurnProfile,
    dispatch_slash,
    parse_slash,
    turn_profiles_as_json,
)


def _profile(label: str, total: float = 1.5) -> TurnProfile:
    return TurnProfile(
        started_at=100.0,
        ended_at=100.0 + total,
        ttft_s=0.2,
        tool_calls=[("fs_read", 0.05), ("fs_grep", 0.12)],
        summary_text=f"label={label}",
        summary_total_s=total,
    )


@pytest.fixture()
def fs_cfg(tmp_path: Path) -> fs_tools.FsConfig:
    return fs_tools.FsConfig(root=tmp_path)


class _StubApp:
    def __init__(self, profiles: list[TurnProfile]) -> None:
        self.turn_profiles = list(profiles)
        self.last_turn_profile = profiles[-1] if profiles else None


class TestRenderer:
    def test_empty_renders_empty_array(self) -> None:
        assert turn_profiles_as_json([]) == "[]"

    def test_single_profile(self) -> None:
        out = turn_profiles_as_json([_profile("a")])
        data = json.loads(out)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["summary_text"] == "label=a"
        assert data[0]["ttft_s"] == 0.2
        assert data[0]["total_s"] == pytest.approx(1.5)

    def test_tool_calls_flattened(self) -> None:
        out = turn_profiles_as_json([_profile("a")])
        data = json.loads(out)
        calls = data[0]["tool_calls"]
        assert calls == [
            {"name": "fs_read", "elapsed_s": 0.05},
            {"name": "fs_grep", "elapsed_s": 0.12},
        ]

    def test_indented(self) -> None:
        out = turn_profiles_as_json([_profile("a")])
        # Default indent=2 means newlines + leading spaces.
        assert "\n  " in out


class TestDispatcher:
    def test_json_flag(self, fs_cfg: fs_tools.FsConfig) -> None:
        app = _StubApp([_profile("a"), _profile("b")])
        out, _ = dispatch_slash(
            parse_slash("/lat --json"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[],
            app=app,
        )
        data = json.loads(out)
        # No N argument → renders most-recent (1 entry).
        assert len(data) == 1
        assert data[0]["summary_text"] == "label=b"

    def test_json_with_n(self, fs_cfg: fs_tools.FsConfig) -> None:
        app = _StubApp([_profile("a"), _profile("b"), _profile("c")])
        out, _ = dispatch_slash(
            parse_slash("/lat 2 --json"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[],
            app=app,
        )
        data = json.loads(out)
        assert len(data) == 2
        # Order preserves insertion order — buffer order, not recency.
        labels = [row["summary_text"] for row in data]
        assert labels == ["label=b", "label=c"]

    def test_format_json_alias(self, fs_cfg: fs_tools.FsConfig) -> None:
        app = _StubApp([_profile("a")])
        out, _ = dispatch_slash(
            parse_slash("/lat --format=json"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[],
            app=app,
        )
        json.loads(out)  # parses

    def test_json_empty_buffer(self, fs_cfg: fs_tools.FsConfig) -> None:
        app = _StubApp([])
        out, _ = dispatch_slash(
            parse_slash("/lat --json"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[],
            app=app,
        )
        assert json.loads(out) == []

    def test_json_flag_position_independent(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        app = _StubApp([_profile("a"), _profile("b")])
        out_a, _ = dispatch_slash(
            parse_slash("/lat 1 --json"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[],
            app=app,
        )
        out_b, _ = dispatch_slash(
            parse_slash("/lat --json 1"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[],
            app=app,
        )
        assert json.loads(out_a) == json.loads(out_b)

    def test_no_json_flag_still_text(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        app = _StubApp([_profile("a")])
        out, _ = dispatch_slash(
            parse_slash("/lat"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[],
            app=app,
        )
        # Plain-text rendering — not parseable as JSON.
        with pytest.raises(json.JSONDecodeError):
            json.loads(out)
