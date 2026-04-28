"""End-to-end test: when `git pull --rebase` conflicts, `_commit_and_push`
must abort the rebase so the next iteration starts with a clean tree.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent import loop as L


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _git_rc(cwd: Path, *args: str) -> int:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True
    ).returncode


@pytest.fixture
def conflicting_repo(tmp_path: Path, monkeypatch):
    """Create a bare 'remote' and a local clone where remote and local have
    diverged on the same line — `git pull --rebase` will conflict."""
    remote = tmp_path / "remote.git"
    local = tmp_path / "local"
    other = tmp_path / "other"
    subprocess.run(
        ["git", "-c", "init.defaultBranch=main", "init", "-q", "--bare", str(remote)],
        check=True,
    )

    # Seed remote via a temp clone.
    subprocess.run(
        ["git", "-c", "init.defaultBranch=main", "clone", "-q", str(remote), str(other)],
        check=True,
    )
    for k, v in [
        ("user.email", "a@a"),
        ("user.name", "a"),
        ("commit.gpgsign", "false"),
        ("init.defaultBranch", "main"),
    ]:
        _git(other, "config", k, v)
    _git(other, "checkout", "-q", "-b", "main")
    (other / "f.txt").write_text("base\n")
    _git(other, "add", "-A")
    _git(other, "commit", "-q", "-m", "init")
    _git(other, "push", "-q", "-u", "origin", "main")

    # Clone for our loop.
    subprocess.run(["git", "clone", "-q", "-b", "main", str(remote), str(local)], check=True)
    for k, v in [
        ("user.email", "b@b"),
        ("user.name", "b"),
        ("commit.gpgsign", "false"),
    ]:
        _git(local, "config", k, v)

    # `other` advances main with a conflicting change.
    (other / "f.txt").write_text("from-other\n")
    _git(other, "add", "-A")
    _git(other, "commit", "-q", "-m", "other")
    _git(other, "push", "-q", "origin", "main")

    # Local makes a conflicting change (uncommitted; will be staged by
    # _commit_and_push).
    (local / "f.txt").write_text("from-local\n")

    monkeypatch.setattr(L, "_REPO", local)
    return local


def test_commit_and_push_aborts_failed_rebase(conflicting_repo: Path):
    head_before = _git(conflicting_repo, "rev-parse", "HEAD").strip()

    ok = L._commit_and_push("loop fix", push=True)

    assert ok is False, "push should fail because of rebase conflict"

    # Tree must be clean — no rebase in progress, no leftover staged changes.
    assert not (conflicting_repo / ".git" / "rebase-merge").exists()
    assert not (conflicting_repo / ".git" / "rebase-apply").exists()

    # Working tree should be reset to a clean state.
    status = _git(conflicting_repo, "status", "--porcelain")
    assert status == "", f"tree not clean after failed rebase: {status!r}"

    # HEAD should still be reachable as a valid commit (not detached weirdly).
    assert _git_rc(conflicting_repo, "rev-parse", "HEAD") == 0


def test_abort_rebase_if_any_is_safe_when_no_rebase(tmp_path, monkeypatch):
    # Plain repo, no rebase in progress — must be a no-op.
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    _git(tmp_path, "config", "user.email", "x@x")
    _git(tmp_path, "config", "user.name", "x")
    (tmp_path / "a").write_text("hi")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    monkeypatch.setattr(L, "_REPO", tmp_path)

    L._abort_rebase_if_any()  # should not raise

    assert _git(tmp_path, "status", "--porcelain") == ""


def test_abort_rebase_if_any_resets_dirty_tree(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    _git(tmp_path, "config", "user.email", "x@x")
    _git(tmp_path, "config", "user.name", "x")
    (tmp_path / "a").write_text("hi")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    (tmp_path / "a").write_text("dirty")  # uncommitted
    monkeypatch.setattr(L, "_REPO", tmp_path)

    L._abort_rebase_if_any()

    assert _git(tmp_path, "status", "--porcelain") == ""
    assert (tmp_path / "a").read_text() == "hi"
