"""Defence-in-depth tests for `_apply_diff` path safety.

`git apply` already refuses to write outside the worktree, but the loop
needs to *visibly* refuse traversal/absolute paths so the failure mode
is logged distinctly rather than buried under "apply_failed". These
tests exercise the pre-apply check so they don't actually invoke git.
"""
from __future__ import annotations

import pytest

from agent import loop


@pytest.mark.parametrize(
    "diff",
    [
        # `+++ b/` traversal
        "--- a/foo.py\n+++ b/../etc/passwd\n@@ -1 +1 @@\n-x\n+y\n",
        # `--- a/` traversal
        "--- a/../../escape.py\n+++ b/escape.py\n@@ -1 +1 @@\n-x\n+y\n",
        # `diff --git` traversal in the a-side
        "diff --git a/../bad b/../bad\n--- a/../bad\n+++ b/../bad\n@@ -1 +1 @@\n-x\n+y\n",
        # multi-segment traversal
        "--- a/src/../../../oops\n+++ b/src/../../../oops\n@@ -1 +1 @@\n-x\n+y\n",
    ],
)
def test_apply_diff_rejects_path_traversal(diff):
    ok, msg = loop._apply_diff(diff)
    assert ok is False
    assert msg.startswith("unsafe_path:")
    assert "path_traversal" in msg


def test_apply_diff_rejects_absolute_posix_path():
    diff = "--- a/foo.py\n+++ b//etc/passwd\n@@ -1 +1 @@\n-x\n+y\n"
    # `+++ b//etc/passwd` -> path "/etc/passwd" after the b/ prefix is stripped
    ok, msg = loop._apply_diff(diff)
    assert ok is False
    assert msg.startswith("unsafe_path:")
    assert "absolute_path" in msg


def test_apply_diff_rejects_windows_drive_path():
    diff = "--- a/foo.py\n+++ b/C:/Windows/system32/evil\n@@ -1 +1 @@\n-x\n+y\n"
    ok, msg = loop._apply_diff(diff)
    assert ok is False
    assert "absolute_path" in msg


def test_apply_diff_rejects_backslash_path():
    diff = "--- a/foo.py\n+++ b/src\\evil.py\n@@ -1 +1 @@\n-x\n+y\n"
    ok, msg = loop._apply_diff(diff)
    assert ok is False
    assert "backslash_in_path" in msg


def test_apply_diff_allows_normal_relative_paths(tmp_path, monkeypatch):
    """A clean diff still goes to git apply (and fails only because the
    file doesn't exist in the temp repo, not because of the safety check)."""
    import subprocess
    monkeypatch.setattr(loop, "_REPO", tmp_path)
    subprocess.run(["git", "-c", "init.defaultBranch=main", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "foo.py").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "i"], cwd=tmp_path, check=True, capture_output=True)
    diff = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-x\n+y\n"
    ok, msg = loop._apply_diff(diff)
    assert ok is True
    assert msg == "applied"


def test_apply_diff_allows_subdirectory_paths_in_check():
    """A nested-but-clean path passes the safety check (git apply may still
    fail because the file doesn't exist, but not on path safety)."""
    diff = "--- a/src/pkg/foo.py\n+++ b/src/pkg/foo.py\n@@ -1 +1 @@\n-x\n+y\n"
    # We only assert that the failure (if any) is NOT unsafe_path.
    ok, msg = loop._apply_diff(diff)
    if not ok:
        assert not msg.startswith("unsafe_path:")


def test_diff_paths_extraction():
    """Direct unit on the path extractor."""
    diff = (
        "diff --git a/src/foo.py b/src/foo.py\n"
        "--- a/src/foo.py\n"
        "+++ b/src/foo.py\n"
        "@@ -1 +1 @@\n-x\n+y\n"
    )
    paths = loop._diff_paths(diff)
    assert "src/foo.py" in paths
    # `diff --git` line contributes both, plus `---` and `+++` => 4 total.
    assert paths.count("src/foo.py") == 4


def test_diff_paths_skips_dev_null():
    """New-file diffs use `--- /dev/null` — must not be flagged absolute."""
    diff = (
        "diff --git a/new.py b/new.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/new.py\n"
        "@@ -0,0 +1 @@\n+x\n"
    )
    paths = loop._diff_paths(diff)
    assert "/dev/null" not in paths
    assert "new.py" in paths
    # And the apply-time safety check must not trip.
    diff = diff.replace("\r\n", "\n")
    if not diff.endswith("\n"):
        diff += "\n"
    assert loop._has_unsafe_path(diff) is None
