"""Tests for `_apply_diff`: line-ending normalisation and basic
contract checks. Real per-test git repo so we exercise the actual
git plumbing.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _git(*args, cwd):
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )


@pytest.fixture
def repo(tmp_path: Path, monkeypatch):
    _git("-c", "init.defaultBranch=main", "init", cwd=tmp_path)
    _git("config", "user.email", "t@t", cwd=tmp_path)
    _git("config", "user.name", "t", cwd=tmp_path)
    (tmp_path / "f.py").write_text("x = 1\n", "utf-8")
    _git("add", "-A", cwd=tmp_path)
    _git("commit", "-m", "init", cwd=tmp_path)

    import agent.loop as L

    monkeypatch.setattr(L, "_REPO", tmp_path)
    return tmp_path


_LF_DIFF = (
    "diff --git a/f.py b/f.py\n"
    "--- a/f.py\n"
    "+++ b/f.py\n"
    "@@ -1 +1 @@\n"
    "-x = 1\n"
    "+x = 2\n"
)


def test_lf_diff_applies(repo):
    import agent.loop as L

    ok, msg = L._apply_diff(_LF_DIFF)
    assert ok, msg
    assert (repo / "f.py").read_text("utf-8") == "x = 2\n"


def test_crlf_diff_applies_after_normalisation(repo):
    """The bug: a CRLF-terminated diff used to be rejected by git apply."""
    import agent.loop as L

    crlf = _LF_DIFF.replace("\n", "\r\n")
    ok, msg = L._apply_diff(crlf)
    assert ok, f"CRLF diff should apply after normalisation: {msg}"
    assert (repo / "f.py").read_text("utf-8") == "x = 2\n"


def test_bare_cr_diff_applies_after_normalisation(repo):
    """Old-Mac line endings (bare CR) also normalised."""
    import agent.loop as L

    cr = _LF_DIFF.replace("\n", "\r")
    ok, msg = L._apply_diff(cr)
    assert ok, f"CR diff should apply after normalisation: {msg}"


def test_diff_inside_fence_with_crlf(repo):
    import agent.loop as L

    fenced = "Here is the patch:\n```diff\n" + _LF_DIFF + "```\n"
    fenced = fenced.replace("\n", "\r\n")
    ok, msg = L._apply_diff(fenced)
    assert ok, msg


def test_non_diff_input_rejected(repo):
    import agent.loop as L

    ok, msg = L._apply_diff("just a sentence, not a diff\n")
    assert not ok
    assert msg == "not_a_unified_diff"


def test_broken_diff_check_fails_cleanly(repo):
    """A diff against a wrong base is rejected at apply --check."""
    import agent.loop as L

    bad = (
        "diff --git a/f.py b/f.py\n"
        "--- a/f.py\n"
        "+++ b/f.py\n"
        "@@ -1 +1 @@\n"
        "-y = 1\n"
        "+y = 2\n"
    )
    ok, msg = L._apply_diff(bad)
    assert not ok
    assert "apply_check_failed" in msg
