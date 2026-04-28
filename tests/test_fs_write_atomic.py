"""Loop 194 — atomic write semantics for `fs_tools.write_file`."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from qwen_coder_mcp import fs_tools


@pytest.fixture()
def cfg(tmp_path: Path) -> fs_tools.FsConfig:
    return fs_tools.FsConfig(root=tmp_path)


def test_round_trip(cfg: fs_tools.FsConfig) -> None:
    res = fs_tools.write_file(cfg, "a.txt", "hello")
    assert res["size"] == 5
    assert (cfg.root / "a.txt").read_text() == "hello"


def test_no_tmp_sibling_after_success(cfg: fs_tools.FsConfig) -> None:
    fs_tools.write_file(cfg, "b.txt", "data")
    siblings = sorted(p.name for p in cfg.root.iterdir())
    assert siblings == ["b.txt"]


def test_replace_failure_preserves_original(
    cfg: fs_tools.FsConfig,
) -> None:
    fs_tools.write_file(cfg, "c.txt", "original")
    with patch("qwen_coder_mcp.fs_tools.os.replace", side_effect=OSError):
        with pytest.raises(fs_tools.FsError):
            fs_tools.write_file(cfg, "c.txt", "new")
    # Original survives.
    assert (cfg.root / "c.txt").read_text() == "original"


def test_tmp_cleaned_up_after_replace_failure(
    cfg: fs_tools.FsConfig,
) -> None:
    fs_tools.write_file(cfg, "d.txt", "x")
    with patch("qwen_coder_mcp.fs_tools.os.replace", side_effect=OSError):
        with pytest.raises(fs_tools.FsError):
            fs_tools.write_file(cfg, "d.txt", "y")
    # No leftover .tmp sibling.
    assert not (cfg.root / "d.txt.tmp").exists()


def test_atomic_replace_uses_tmp_path(cfg: fs_tools.FsConfig) -> None:
    """At the moment of replace, the source must be a .tmp sibling
    of the target — not the target itself."""
    seen: dict[str, object] = {}
    real_replace = os.replace

    def _spy(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        seen["src_was_tmp"] = str(src).endswith(".tmp")
        seen["dst"] = str(dst)
        real_replace(src, dst)

    with patch("qwen_coder_mcp.fs_tools.os.replace", side_effect=_spy):
        fs_tools.write_file(cfg, "e.txt", "z")

    assert seen.get("src_was_tmp") is True
    assert str(seen.get("dst", "")).endswith("e.txt")


def test_oversize_still_rejected_before_tmp_write(
    cfg: fs_tools.FsConfig,
) -> None:
    """Size-cap check runs *before* the atomic write so we never
    create a .tmp file just to immediately delete it."""
    cfg2 = fs_tools.FsConfig(root=cfg.root, max_write_bytes=4)
    with pytest.raises(fs_tools.FsError, match="too large"):
        fs_tools.write_file(cfg2, "f.txt", "12345")
    # No .tmp sibling created.
    assert not any(p.name.endswith(".tmp") for p in cfg.root.iterdir())


def test_create_parents_still_works(cfg: fs_tools.FsConfig) -> None:
    fs_tools.write_file(cfg, "sub/dir/g.txt", "deep", create_parents=True)
    assert (cfg.root / "sub" / "dir" / "g.txt").read_text() == "deep"
    assert not (cfg.root / "sub" / "dir" / "g.txt.tmp").exists()
