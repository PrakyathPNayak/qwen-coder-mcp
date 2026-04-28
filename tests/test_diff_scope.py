"""Tests for `_diff_in_scope` — the guard that prevents the model from
silently rewriting unrelated files."""
from __future__ import annotations

from pathlib import Path

from agent import loop as L


def test_single_target_path_is_in_scope():
    ok, msg = L._diff_in_scope([Path("a/b.py")], Path("a/b.py"))
    assert ok is True
    assert msg == "ok"


def test_empty_changeset_is_in_scope():
    # No changes at all is not an out-of-scope violation; the empty-diff
    # case is filtered separately by _commit_and_push's status check.
    ok, _ = L._diff_in_scope([], Path("a/b.py"))
    assert ok is True


def test_other_file_in_changeset_is_rejected():
    ok, msg = L._diff_in_scope(
        [Path("a/b.py"), Path("c/d.py")], Path("a/b.py")
    )
    assert ok is False
    assert msg.startswith("out_of_scope")
    assert "c/d.py" in msg


def test_only_other_file_is_rejected():
    ok, msg = L._diff_in_scope([Path("c/d.py")], Path("a/b.py"))
    assert ok is False
    assert "c/d.py" in msg


def test_path_separators_normalised():
    # On platforms where `git diff --name-only` returns posix and the
    # caller passes a Path, equality must still hold.
    ok, _ = L._diff_in_scope([Path("a/b.py")], Path("a") / "b.py")
    assert ok is True


def test_too_many_out_of_scope_truncated():
    ok, msg = L._diff_in_scope(
        [Path(f"x/{i}.py") for i in range(10)], Path("y/keep.py")
    )
    assert ok is False
    # Message lists at most 3 offenders.
    assert msg.count(",") <= 2
