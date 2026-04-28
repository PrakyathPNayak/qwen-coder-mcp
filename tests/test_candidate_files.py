"""Tests for `_candidate_files` symlink and edge-case filtering.

Loop 35 added a symlink skip so iteration cursor slots aren't wasted
on links (in-repo links are redundant with their targets; out-of-repo
links are refused by `_read_file` anyway).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture
def repo(tmp_path: Path, monkeypatch):
    import agent.loop as L
    monkeypatch.setattr(L, "_REPO", tmp_path)
    return tmp_path, L


def test_skips_symlink_to_outside_file(repo, tmp_path_factory):
    root, L = repo
    outside = tmp_path_factory.mktemp("outside") / "secret.txt"
    outside.write_text("classified\n", "utf-8")

    (root / "real.py").write_text("x = 1\n", "utf-8")
    os.symlink(outside, root / "leak.py")

    paths = [str(p) for p in L._candidate_files()]
    assert "real.py" in paths
    assert "leak.py" not in paths


def test_skips_intra_repo_symlink(repo):
    root, L = repo
    (root / "target.py").write_text("y = 2\n", "utf-8")
    os.symlink(root / "target.py", root / "alias.py")

    paths = [str(p) for p in L._candidate_files()]
    assert "target.py" in paths
    assert "alias.py" not in paths


def test_dangling_symlink_excluded(repo):
    root, L = repo
    (root / "real.py").write_text("z = 3\n", "utf-8")
    os.symlink(root / "missing.py", root / "broken.py")

    paths = [str(p) for p in L._candidate_files()]
    assert "broken.py" not in paths
    assert "real.py" in paths


def test_empty_files_still_excluded(repo):
    root, L = repo
    (root / "empty.py").write_text("", "utf-8")
    (root / "real.py").write_text("a = 4\n", "utf-8")
    paths = [str(p) for p in L._candidate_files()]
    assert "empty.py" not in paths
    assert "real.py" in paths
