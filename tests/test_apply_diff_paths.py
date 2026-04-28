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


# ---------------------------------------------------- structural-defect tests
@pytest.mark.parametrize(
    "diff,expected",
    [
        # `--- a/` only, no `+++`
        ("--- a/foo.py\n@@ -1 +1 @@\n-x\n+y\n", "missing_plus_header"),
        # both headers but no hunks
        ("--- a/foo.py\n+++ b/foo.py\n", "no_hunks"),
    ],
)
def test_apply_diff_rejects_malformed(diff, expected):
    ok, msg = loop._apply_diff(diff)
    assert ok is False
    assert msg.startswith("malformed_diff:")
    assert expected in msg


def test_apply_diff_plus_only_caught_as_not_a_unified_diff():
    """`+++` without `---` doesn't reach the structural check — it's
    caught by the prefix check at the top of `_apply_diff` and tagged
    `not_a_unified_diff`. Either outcome is fine; we just want
    *not-applied*."""
    diff = "+++ b/foo.py\n@@ -1 +1 @@\n-x\n+y\n"
    ok, msg = loop._apply_diff(diff)
    assert ok is False
    assert msg == "not_a_unified_diff"


def test_apply_diff_accepts_well_formed_diff_with_dev_null(tmp_path, monkeypatch):
    """A new-file diff using /dev/null is well-formed and must pass the
    structural check (it'll fail at git apply if file already exists, but
    not on structural grounds)."""
    import subprocess
    monkeypatch.setattr(loop, "_REPO", tmp_path)
    subprocess.run(["git", "-c", "init.defaultBranch=main", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "seed").write_text("seed\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "i"], cwd=tmp_path, check=True, capture_output=True)
    diff = (
        "diff --git a/new.py b/new.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/new.py\n"
        "@@ -0,0 +1 @@\n"
        "+x\n"
    )
    ok, msg = loop._apply_diff(diff)
    assert ok is True
    assert msg == "applied"


# ----------------------------------------------------------- mode-safety tests
@pytest.mark.parametrize(
    "mode_line,reason",
    [
        ("new file mode 120000", "symlink_mode"),
        ("new mode 120000", "symlink_mode"),
        ("old mode 120000", "symlink_mode"),
        ("deleted file mode 120000", "symlink_mode"),
        ("new file mode 160000", "gitlink_mode"),
    ],
)
def test_apply_diff_rejects_symlink_and_gitlink_modes(mode_line, reason):
    diff = (
        f"diff --git a/x b/x\n"
        f"{mode_line}\n"
        f"--- a/x\n+++ b/x\n@@ -0,0 +1 @@\n+/etc/passwd\n"
    )
    ok, msg = loop._apply_diff(diff)
    assert ok is False
    assert msg.startswith("unsafe_mode:")
    assert reason in msg


def test_apply_diff_allows_normal_file_modes():
    """100644 (regular) and 100755 (executable) must NOT be flagged."""
    for mode in ("100644", "100755"):
        diff = (
            f"diff --git a/foo b/foo\n"
            f"new file mode {mode}\n"
            f"--- /dev/null\n+++ b/foo\n@@ -0,0 +1 @@\n+x\n"
        )
        # Direct unit on the predicate (avoids needing a real git repo).
        diff_n = diff if diff.endswith("\n") else diff + "\n"
        assert loop._has_unsafe_mode(diff_n) is None


def test_has_unsafe_mode_ignores_mode_in_content_lines():
    """A `+` content line that contains '120000' is not a mode header."""
    diff = (
        "diff --git a/foo.py b/foo.py\n"
        "--- a/foo.py\n+++ b/foo.py\n"
        "@@ -1 +1 @@\n-x\n+# permissions: 120000 was the old mode\n"
    )
    assert loop._has_unsafe_mode(diff) is None


# ----------------------------------------------------------- binary-patch tests
def test_apply_diff_rejects_git_binary_patch():
    diff = (
        "diff --git a/img.png b/img.png\n"
        "index 0000000..abcdef 100644\n"
        "GIT binary patch\n"
        "literal 16\n"
        "zcmZ?w...base85...\n"
    )
    ok, msg = loop._apply_diff(diff)
    assert ok is False
    assert msg.startswith("binary_patch:")
    assert "git_binary_patch" in msg


def test_apply_diff_rejects_binary_files_differ_marker():
    diff = (
        "diff --git a/img.png b/img.png\n"
        "Binary files a/img.png and b/img.png differ\n"
    )
    ok, msg = loop._apply_diff(diff)
    assert ok is False
    assert msg.startswith("binary_patch:")
    assert "binary_files_marker" in msg


def test_has_binary_patch_ignores_phrase_in_content():
    """A `+` content line that mentions 'Binary files' (e.g. in docs) is
    not a binary-patch marker."""
    diff = (
        "--- a/README.md\n+++ b/README.md\n"
        "@@ -1 +1,2 @@\n x\n"
        "+# Note: 'Binary files differ' is git's marker for non-text diffs.\n"
    )
    assert loop._has_binary_patch(diff) is None


def test_has_binary_patch_ignores_phrase_in_minus_line():
    """A `-` content line removing prose that contains the phrase must
    not be flagged either (it's deletion, not binary)."""
    diff = (
        "--- a/README.md\n+++ b/README.md\n"
        "@@ -1,2 +1 @@\n"
        "-Binary files explained\n x\n"
    )
    assert loop._has_binary_patch(diff) is None


def test_has_binary_patch_ignores_context_line_inside_hunk():
    """A context line (leading space) inside a hunk that happens to read
    'Binary files X and Y differ' is data, not a marker."""
    diff = (
        "--- a/README.md\n+++ b/README.md\n"
        "@@ -1,3 +1,3 @@\n"
        " Binary files a/x and b/y differ\n"  # context line, not marker
        "-old\n+new\n"
    )
    assert loop._has_binary_patch(diff) is None


def test_has_binary_patch_resumes_header_check_in_next_file():
    """A multi-file diff where the second file is binary — the marker
    appears outside the first file's hunk, so it must be caught."""
    diff = (
        "--- a/README.md\n+++ b/README.md\n"
        "@@ -1 +1 @@\n-x\n+y\n"
        "diff --git a/img.png b/img.png\n"
        "Binary files a/img.png and b/img.png differ\n"
    )
    assert loop._has_binary_patch(diff) == "binary_files_marker"
