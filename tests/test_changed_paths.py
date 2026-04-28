"""Tests for `_changed_paths` and `_revert_changes` covering the
scope-guard hole where a diff creating a brand-new file used to bypass
`_diff_in_scope` because `git diff --name-only` doesn't list untracked
files.

We materialize a real git repo per test so we exercise the actual git
plumbing rather than mocking it.
"""
from __future__ import annotations

import importlib
import os
import subprocess
from pathlib import Path

import pytest


def _git(*args, cwd):
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def repo(tmp_path: Path, monkeypatch):
    """A throwaway git repo wired into agent.loop._REPO."""
    _git("-c", "init.defaultBranch=main", "init", cwd=tmp_path)
    _git("config", "user.email", "t@t", cwd=tmp_path)
    _git("config", "user.name", "t", cwd=tmp_path)
    (tmp_path / "tracked.py").write_text("x = 1\n", "utf-8")
    _git("add", "-A", cwd=tmp_path)
    _git("commit", "-m", "init", cwd=tmp_path)

    import agent.loop as L

    monkeypatch.setattr(L, "_REPO", tmp_path)
    return tmp_path


def test_changed_paths_modified_file(repo, monkeypatch):
    import agent.loop as L

    (repo / "tracked.py").write_text("x = 2\n", "utf-8")
    paths = [str(p) for p in L._changed_paths()]
    assert "tracked.py" in paths


def test_changed_paths_includes_untracked(repo):
    """The bug: `git diff --name-only` would miss this."""
    import agent.loop as L

    (repo / "stray.py").write_text("# new file\n", "utf-8")
    paths = [str(p) for p in L._changed_paths()]
    assert "stray.py" in paths


def test_changed_paths_includes_untracked_in_subdir(repo):
    import agent.loop as L

    sub = repo / "subdir"
    sub.mkdir()
    (sub / "x.py").write_text("# new\n", "utf-8")
    paths = [str(p).replace(os.sep, "/") for p in L._changed_paths()]
    assert "subdir/x.py" in paths


def test_changed_paths_path_with_space(repo):
    import agent.loop as L

    (repo / "spaced name.txt").write_text("hi\n", "utf-8")
    paths = [str(p) for p in L._changed_paths()]
    assert "spaced name.txt" in paths


def test_changed_paths_deleted_file(repo):
    import agent.loop as L

    (repo / "tracked.py").unlink()
    paths = [str(p) for p in L._changed_paths()]
    assert "tracked.py" in paths


def test_revert_removes_untracked_file(repo):
    """The bug: `git checkout -- .` left untracked files in place."""
    import agent.loop as L

    (repo / "stray.py").write_text("# new file\n", "utf-8")
    assert (repo / "stray.py").exists()
    L._revert_changes()
    assert not (repo / "stray.py").exists()


def test_revert_restores_modified_file(repo):
    import agent.loop as L

    (repo / "tracked.py").write_text("x = 999\n", "utf-8")
    L._revert_changes()
    assert (repo / "tracked.py").read_text("utf-8") == "x = 1\n"


def test_revert_clears_modified_and_untracked_together(repo):
    import agent.loop as L

    (repo / "tracked.py").write_text("x = 999\n", "utf-8")
    (repo / "stray.py").write_text("# new\n", "utf-8")
    sub = repo / "evil"
    sub.mkdir()
    (sub / "shadow.py").write_text("# new\n", "utf-8")

    L._revert_changes()

    assert (repo / "tracked.py").read_text("utf-8") == "x = 1\n"
    assert not (repo / "stray.py").exists()
    assert not sub.exists()


def test_diff_in_scope_catches_untracked_new_file(repo):
    """End-to-end: `_diff_in_scope` now sees an untracked file produced
    by an out-of-scope diff and rejects it."""
    import agent.loop as L

    (repo / "stray.py").write_text("# new file\n", "utf-8")
    changed = L._changed_paths()
    ok, msg = L._diff_in_scope(changed, Path("tracked.py"))
    assert not ok
    assert "stray.py" in msg or "out_of_scope" in msg
