"""Tests for the multi-turn ``/lat N`` form (loop 188) — the
``format_turn_profiles`` renderer and the dispatcher's argument
parsing. The single-turn ``/lat`` and the underlying TurnProfile
renderer are covered by ``test_lat_slash.py``."""
from __future__ import annotations

from pathlib import Path

import pytest

from qwen_coder_mcp import fs_tools
from qwen_coder_mcp.tui import (
    DEFAULT_TURN_PROFILE_HISTORY,
    TurnProfile,
    dispatch_slash,
    format_turn_profiles,
    parse_slash,
)


def _profile(label: str, total_s: float = 1.0) -> TurnProfile:
    return TurnProfile(
        started_at=0.0,
        ended_at=total_s,
        ttft_s=0.1,
        tool_calls=[(label, 0.05)],
        summary_text=f"label={label}",
    )


@pytest.fixture()
def fs_cfg(tmp_path: Path) -> fs_tools.FsConfig:
    return fs_tools.FsConfig(root=tmp_path)


class TestFormatTurnProfiles:
    def test_empty_returns_placeholder(self) -> None:
        out = format_turn_profiles([], n=1)
        assert "no agent turn" in out

    def test_single_profile_no_header(self) -> None:
        # When n=1, output matches single-turn format (no "=== turn -1 ===").
        out = format_turn_profiles([_profile("a")], n=1)
        assert "=== turn" not in out
        assert "label=a" in out

    def test_multiple_profiles_have_headers(self) -> None:
        out = format_turn_profiles([_profile("old"), _profile("new")], n=2)
        assert "=== turn -1 ===" in out
        assert "=== turn -2 ===" in out

    def test_most_recent_first(self) -> None:
        out = format_turn_profiles([_profile("old"), _profile("new")], n=2)
        # The "-1" header (most recent) appears before the "-2" header.
        assert out.index("=== turn -1 ===") < out.index("=== turn -2 ===")
        # And the "-1" block holds the newer label.
        h1 = out.index("=== turn -1 ===")
        h2 = out.index("=== turn -2 ===")
        assert "label=new" in out[h1:h2]
        assert "label=old" in out[h2:]

    def test_n_clamped_to_buffer_length(self) -> None:
        out = format_turn_profiles([_profile("only")], n=99)
        # Only 1 profile available; renderer falls into single-turn mode.
        assert "=== turn" not in out
        assert "label=only" in out

    def test_n_zero_treated_as_one(self) -> None:
        out = format_turn_profiles([_profile("a"), _profile("b")], n=0)
        assert "=== turn" not in out  # rendered as single
        assert "label=b" in out

    def test_n_negative_treated_as_one(self) -> None:
        out = format_turn_profiles([_profile("a"), _profile("b")], n=-5)
        assert "label=b" in out


class TestLatDispatchMulti:
    def _stub(self, profiles: list[TurnProfile]) -> object:
        class _StubApp:
            turn_profiles = list(profiles)
            last_turn_profile = profiles[-1] if profiles else None

        return _StubApp()

    def test_no_arg_renders_last(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        app = self._stub([_profile("first"), _profile("second")])
        out, _ = dispatch_slash(
            parse_slash("/lat"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[],
            app=app,
        )
        assert "label=second" in out
        assert "=== turn" not in out

    def test_n_arg_renders_multi(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        app = self._stub([_profile(f"v{i}") for i in range(3)])
        out, _ = dispatch_slash(
            parse_slash("/lat 2"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[],
            app=app,
        )
        assert "=== turn -1 ===" in out
        assert "=== turn -2 ===" in out
        assert "=== turn -3 ===" not in out

    def test_n_arg_non_integer(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        out, _ = dispatch_slash(
            parse_slash("/lat foo"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[],
            app=self._stub([_profile("a")]),
        )
        assert "expected integer" in out

    def test_n_arg_zero_rejected(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        out, _ = dispatch_slash(
            parse_slash("/lat 0"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[],
            app=self._stub([_profile("a")]),
        )
        assert ">= 1" in out

    def test_n_arg_negative_rejected(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        out, _ = dispatch_slash(
            parse_slash("/lat -3"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[],
            app=self._stub([_profile("a")]),
        )
        assert ">= 1" in out

    def test_falls_back_to_last_turn_profile_when_buffer_missing(
        self, fs_cfg: fs_tools.FsConfig
    ) -> None:
        # Old-style stub: no turn_profiles attribute but last_turn_profile present.
        class _OldStub:
            last_turn_profile = _profile("only")

        out, _ = dispatch_slash(
            parse_slash("/lat"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[],
            app=_OldStub(),
        )
        assert "label=only" in out


class TestRingBufferDefault:
    def test_history_cap_is_twenty(self) -> None:
        # Pin the documented default so changing it without intent
        # trips a test.
        assert DEFAULT_TURN_PROFILE_HISTORY == 20
