"""Loop 208 — `/lat --json --top K` filters the ring buffer to the
slowest turns. Mirror of loop 207 (`/tokens --json --top K`)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from qwen_coder_mcp import fs_tools
from qwen_coder_mcp.tui import TurnProfile, dispatch_slash, parse_slash


def _profile(label: str, total: float) -> TurnProfile:
    return TurnProfile(
        started_at=100.0,
        ended_at=100.0 + total,
        ttft_s=0.1,
        tool_calls=[],
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


@pytest.fixture()
def four_profiles() -> list[TurnProfile]:
    # Fastest -> slowest mixed; total_s differs by index.
    return [
        _profile("a", 0.5),
        _profile("b", 5.0),
        _profile("c", 1.5),
        _profile("d", 3.0),
    ]


class TestTopBasic:
    def test_top_picks_slowest(
        self, fs_cfg: fs_tools.FsConfig, four_profiles: list[TurnProfile]
    ) -> None:
        app = _StubApp(four_profiles)
        text, _ = dispatch_slash(
            parse_slash("/lat 10 --json --top 2"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=None,
            app=app,
        )
        data = json.loads(text)
        assert isinstance(data, list)
        assert len(data) == 2
        # Slowest first: b (5.0), then d (3.0).
        labels = [d["summary_text"] for d in data]
        assert labels == ["label=b", "label=d"]

    def test_top_eq_form(
        self, fs_cfg: fs_tools.FsConfig, four_profiles: list[TurnProfile]
    ) -> None:
        app = _StubApp(four_profiles)
        text, _ = dispatch_slash(
            parse_slash("/lat 10 --json --top=1"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=None,
            app=app,
        )
        data = json.loads(text)
        assert len(data) == 1
        assert data[0]["summary_text"] == "label=b"

    def test_top_applies_after_n_slice(
        self, fs_cfg: fs_tools.FsConfig, four_profiles: list[TurnProfile]
    ) -> None:
        # /lat 2 takes the LAST two -> [c, d]; top 1 of those is d (3.0).
        app = _StubApp(four_profiles)
        text, _ = dispatch_slash(
            parse_slash("/lat 2 --json --top 1"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=None,
            app=app,
        )
        data = json.loads(text)
        assert len(data) == 1
        assert data[0]["summary_text"] == "label=d"


class TestTopEdges:
    def test_top_zero(
        self, fs_cfg: fs_tools.FsConfig, four_profiles: list[TurnProfile]
    ) -> None:
        app = _StubApp(four_profiles)
        text, _ = dispatch_slash(
            parse_slash("/lat 10 --json --top 0"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=None,
            app=app,
        )
        assert json.loads(text) == []

    def test_top_oversized_returns_all(
        self, fs_cfg: fs_tools.FsConfig, four_profiles: list[TurnProfile]
    ) -> None:
        app = _StubApp(four_profiles)
        text, _ = dispatch_slash(
            parse_slash("/lat 10 --json --top 999"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=None,
            app=app,
        )
        data = json.loads(text)
        assert len(data) == 4


class TestTopErrors:
    def test_top_without_json(
        self, fs_cfg: fs_tools.FsConfig, four_profiles: list[TurnProfile]
    ) -> None:
        app = _StubApp(four_profiles)
        text, _ = dispatch_slash(
            parse_slash("/lat --top 2"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=None,
            app=app,
        )
        assert "--top requires --json" in text

    def test_top_negative(
        self, fs_cfg: fs_tools.FsConfig, four_profiles: list[TurnProfile]
    ) -> None:
        app = _StubApp(four_profiles)
        text, _ = dispatch_slash(
            parse_slash("/lat --json --top -1"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=None,
            app=app,
        )
        assert "non-negative" in text

    def test_top_non_integer(
        self, fs_cfg: fs_tools.FsConfig, four_profiles: list[TurnProfile]
    ) -> None:
        app = _StubApp(four_profiles)
        text, _ = dispatch_slash(
            parse_slash("/lat --json --top xyz"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=None,
            app=app,
        )
        assert "not an integer" in text

    def test_top_missing(
        self, fs_cfg: fs_tools.FsConfig, four_profiles: list[TurnProfile]
    ) -> None:
        app = _StubApp(four_profiles)
        text, _ = dispatch_slash(
            parse_slash("/lat --json --top"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=None,
            app=app,
        )
        assert "usage" in text.lower()


class TestBackwardsCompat:
    def test_bare_json_unchanged(
        self, fs_cfg: fs_tools.FsConfig, four_profiles: list[TurnProfile]
    ) -> None:
        app = _StubApp(four_profiles)
        text, _ = dispatch_slash(
            parse_slash("/lat 10 --json"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=None,
            app=app,
        )
        data = json.loads(text)
        # All four turns, in original (chronological) order.
        labels = [d["summary_text"] for d in data]
        assert labels == ["label=a", "label=b", "label=c", "label=d"]

    def test_no_json_no_top_renders_text(
        self, fs_cfg: fs_tools.FsConfig, four_profiles: list[TurnProfile]
    ) -> None:
        app = _StubApp(four_profiles)
        text, _ = dispatch_slash(
            parse_slash("/lat"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=None,
            app=app,
        )
        # Definitely not JSON.
        with pytest.raises(json.JSONDecodeError):
            json.loads(text)
