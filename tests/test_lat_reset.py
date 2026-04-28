"""Loop 190 — ``/lat reset`` clears the turn-profile ring buffer."""
from __future__ import annotations

from pathlib import Path

import pytest

from qwen_coder_mcp import fs_tools
from qwen_coder_mcp.tui import (
    TurnProfile,
    dispatch_slash,
    parse_slash,
)


def _profile(label: str) -> TurnProfile:
    return TurnProfile(
        started_at=0.0,
        ended_at=1.0,
        ttft_s=0.1,
        tool_calls=[(label, 0.05)],
        summary_text=f"label={label}",
    )


@pytest.fixture()
def fs_cfg(tmp_path: Path) -> fs_tools.FsConfig:
    return fs_tools.FsConfig(root=tmp_path)


class _StubApp:
    def __init__(self, profiles: list[TurnProfile]) -> None:
        self.turn_profiles = list(profiles)
        self.last_turn_profile = profiles[-1] if profiles else None


def test_reset_empties_ring_buffer(fs_cfg: fs_tools.FsConfig) -> None:
    app = _StubApp([_profile("a"), _profile("b"), _profile("c")])
    out, _ = dispatch_slash(
        parse_slash("/lat reset"),
        client=None,  # type: ignore[arg-type]
        fs_cfg=fs_cfg,
        history=[],
        app=app,
    )
    assert "cleared 3" in out
    assert app.turn_profiles == []
    assert app.last_turn_profile is None


def test_reset_on_empty_buffer(fs_cfg: fs_tools.FsConfig) -> None:
    app = _StubApp([])
    out, _ = dispatch_slash(
        parse_slash("/lat reset"),
        client=None,  # type: ignore[arg-type]
        fs_cfg=fs_cfg,
        history=[],
        app=app,
    )
    assert "cleared 0" in out
    assert app.turn_profiles == []


def test_reset_is_case_insensitive(fs_cfg: fs_tools.FsConfig) -> None:
    app = _StubApp([_profile("a")])
    for token in ("RESET", "Reset", "ReSeT"):
        # Re-seed each pass.
        app.turn_profiles = [_profile("a")]
        out, _ = dispatch_slash(
            parse_slash(f"/lat {token}"),
            client=None,  # type: ignore[arg-type]
            fs_cfg=fs_cfg,
            history=[],
            app=app,
        )
        assert "cleared 1" in out
        assert app.turn_profiles == []


def test_reset_does_not_crash_without_app(
    fs_cfg: fs_tools.FsConfig,
) -> None:
    out, _ = dispatch_slash(
        parse_slash("/lat reset"),
        client=None,  # type: ignore[arg-type]
        fs_cfg=fs_cfg,
        history=[],
        app=None,
    )
    assert "cleared 0" in out


def test_reset_with_old_style_stub_missing_buffer(
    fs_cfg: fs_tools.FsConfig,
) -> None:
    class _OldStub:
        last_turn_profile = _profile("solo")

    stub = _OldStub()
    out, _ = dispatch_slash(
        parse_slash("/lat reset"),
        client=None,  # type: ignore[arg-type]
        fs_cfg=fs_cfg,
        history=[],
        app=stub,
    )
    assert "cleared" in out
    # Reset should clear last_turn_profile too even on older stubs.
    assert stub.last_turn_profile is None


def test_subsequent_lat_after_reset_shows_no_data(
    fs_cfg: fs_tools.FsConfig,
) -> None:
    app = _StubApp([_profile("a"), _profile("b")])
    dispatch_slash(
        parse_slash("/lat reset"),
        client=None,  # type: ignore[arg-type]
        fs_cfg=fs_cfg,
        history=[],
        app=app,
    )
    out, _ = dispatch_slash(
        parse_slash("/lat"),
        client=None,  # type: ignore[arg-type]
        fs_cfg=fs_cfg,
        history=[],
        app=app,
    )
    # The placeholder rendered by format_turn_profile(None).
    assert "no agent turn" in out


def test_non_reset_string_still_rejected_as_int(
    fs_cfg: fs_tools.FsConfig,
) -> None:
    """The reset path is an exact-token match — random words still
    fall through to the int parser and produce the original error."""
    out, _ = dispatch_slash(
        parse_slash("/lat resetx"),
        client=None,  # type: ignore[arg-type]
        fs_cfg=fs_cfg,
        history=[],
        app=_StubApp([_profile("a")]),
    )
    assert "expected integer" in out
