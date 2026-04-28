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


# ----------------------------------------------------- rename/copy header tests
def test_diff_paths_includes_rename_to_path():
    diff = (
        "diff --git a/foo.py b/bar.py\n"
        "similarity index 100%\n"
        "rename from foo.py\n"
        "rename to ../../etc/passwd\n"
    )
    paths = loop._diff_paths(diff)
    assert "foo.py" in paths
    assert "../../etc/passwd" in paths


def test_apply_diff_rejects_rename_to_traversal():
    diff = (
        "diff --git a/foo.py b/bar.py\n"
        "similarity index 100%\n"
        "rename from foo.py\n"
        "rename to ../../etc/passwd\n"
    )
    ok, msg = loop._apply_diff(diff)
    assert ok is False
    assert msg.startswith("unsafe_path:")
    assert "path_traversal" in msg


def test_apply_diff_rejects_copy_to_absolute():
    diff = (
        "diff --git a/foo.py b/bar.py\n"
        "similarity index 50%\n"
        "copy from foo.py\n"
        "copy to /etc/passwd\n"
    )
    ok, msg = loop._apply_diff(diff)
    assert ok is False
    assert msg.startswith("unsafe_path:")
    assert "absolute_path" in msg


def test_apply_diff_rejects_rename_from_with_backslash():
    diff = (
        "diff --git a/foo.py b/bar.py\n"
        "similarity index 100%\n"
        "rename from src\\foo.py\n"
        "rename to bar.py\n"
    )
    ok, msg = loop._apply_diff(diff)
    assert ok is False
    assert msg.startswith("unsafe_path:")
    assert "backslash_in_path" in msg


def test_diff_paths_safe_rename_passes_path_check():
    diff = (
        "diff --git a/foo.py b/bar.py\n"
        "similarity index 100%\n"
        "rename from foo.py\n"
        "rename to bar.py\n"
    )
    assert loop._has_unsafe_path(diff) is None


# --------------------------------------------------- index-line mode tests
def test_has_unsafe_mode_rejects_symlink_on_index_line():
    diff = (
        "diff --git a/link b/link\n"
        "new file mode 100644\n"  # we'll override below
        "index 0000000..abc1234 120000\n"
        "--- /dev/null\n+++ b/link\n@@ -0,0 +1 @@\n+/etc/passwd\n"
    )
    msg = loop._has_unsafe_mode(diff)
    assert msg is not None
    assert msg.startswith("symlink_mode:")


def test_has_unsafe_mode_rejects_gitlink_on_index_line():
    diff = (
        "diff --git a/sub b/sub\n"
        "index 0000000..abc1234 160000\n"
    )
    msg = loop._has_unsafe_mode(diff)
    assert msg is not None
    assert msg.startswith("gitlink_mode:")


def test_has_unsafe_mode_accepts_normal_index_mode():
    diff = (
        "diff --git a/foo.py b/foo.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-x\n+y\n"
    )
    assert loop._has_unsafe_mode(diff) is None


def test_has_unsafe_mode_ignores_short_index_line_without_mode():
    """Older git omits the mode on the index line for unchanged-mode
    edits: `index abc..def`. This must not be flagged."""
    diff = (
        "diff --git a/foo.py b/foo.py\n"
        "index 1111111..2222222\n"
        "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-x\n+y\n"
    )
    assert loop._has_unsafe_mode(diff) is None


def test_apply_diff_rejects_symlink_via_index_only():
    diff = (
        "diff --git a/link b/link\n"
        "index 0000000..abc1234 120000\n"
        "--- /dev/null\n+++ b/link\n@@ -0,0 +1 @@\n+/etc/passwd\n"
    )
    ok, msg = loop._apply_diff(diff)
    assert ok is False
    assert msg.startswith("unsafe_mode:")
    assert "symlink_mode" in msg


# ---------------------------------------------------------- size clamp tests
def test_has_oversized_diff_accepts_small():
    diff = (
        "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n"
    )
    assert loop._has_oversized_diff(diff) is None


def test_has_oversized_diff_rejects_too_many_bytes(monkeypatch):
    monkeypatch.setattr(loop, "_MAX_DIFF_BYTES", 100)
    diff = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+" + ("y" * 200) + "\n"
    msg = loop._has_oversized_diff(diff)
    assert msg is not None
    assert msg.startswith("size_bytes:")


def test_has_oversized_diff_rejects_too_many_lines(monkeypatch):
    monkeypatch.setattr(loop, "_MAX_DIFF_LINES", 5)
    diff = "--- a/x\n+++ b/x\n@@\n" + "\n".join("+l%d" % i for i in range(20)) + "\n"
    msg = loop._has_oversized_diff(diff)
    assert msg is not None
    assert msg.startswith("size_lines:")


def test_apply_diff_rejects_oversized_with_oversized_prefix(monkeypatch):
    monkeypatch.setattr(loop, "_MAX_DIFF_BYTES", 50)
    diff = (
        "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+" + ("y" * 200) + "\n"
    )
    ok, msg = loop._apply_diff(diff)
    assert ok is False
    assert msg.startswith("oversized_diff:")


def test_oversized_check_runs_before_path_check(monkeypatch):
    """Even a path-traversal diff that would have been caught by
    _has_unsafe_path is rejected as oversized first if it's larger
    than the byte cap. The earlier-stage rejection is preferred."""
    monkeypatch.setattr(loop, "_MAX_DIFF_BYTES", 50)
    diff = (
        "diff --git a/foo b/foo\n"
        "rename from foo\n"
        "rename to ../../etc/passwd\n"
        + ("padding line\n" * 50)
    )
    ok, msg = loop._apply_diff(diff)
    assert ok is False
    assert msg.startswith("oversized_diff:")


# ---------------------------------------------------- git-apply timeout tests
def test_run_git_apply_timeout_returns_124(monkeypatch):
    """When subprocess.run raises TimeoutExpired, the wrapper returns
    (124, 'timed_out_after_<N>s') instead of propagating."""
    import subprocess as sp

    def fake_run(*a, **kw):
        raise sp.TimeoutExpired(cmd="git apply", timeout=kw.get("timeout"))

    monkeypatch.setattr(loop.subprocess, "run", fake_run)
    rc, err = loop._run_git_apply(["apply", "-"], "irrelevant\n")
    assert rc == 124
    assert err.startswith("timed_out_after_")


def test_apply_diff_timeout_on_check_returns_apply_check_failed(monkeypatch):
    import subprocess as sp

    def fake_run(*a, **kw):
        raise sp.TimeoutExpired(cmd="git apply --check", timeout=kw.get("timeout"))

    monkeypatch.setattr(loop.subprocess, "run", fake_run)
    diff = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-1\n+2\n"
    ok, msg = loop._apply_diff(diff)
    assert ok is False
    assert msg.startswith("apply_check_failed:")
    assert "timed_out_after_" in msg


def test_apply_diff_timeout_only_on_apply_returns_apply_failed(monkeypatch):
    """`--check` succeeds normally; `apply` itself times out."""
    import subprocess as sp

    real_run = loop.subprocess.run
    state = {"first": True}

    def fake_run(args, *a, **kw):
        if state["first"] and "--check" in args:
            state["first"] = False
            class P:
                returncode = 0
                stderr = ""
            return P()
        raise sp.TimeoutExpired(cmd="git apply", timeout=kw.get("timeout"))

    monkeypatch.setattr(loop.subprocess, "run", fake_run)
    diff = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-1\n+2\n"
    ok, msg = loop._apply_diff(diff)
    assert ok is False
    assert msg.startswith("apply_failed:")
    assert "timed_out_after_" in msg


def test_run_git_apply_passes_timeout_kwarg(monkeypatch):
    """Verify the wrapper actually passes timeout= to subprocess.run."""
    captured = {}

    def fake_run(args, *a, **kw):
        captured.update(kw)
        class P:
            returncode = 0
            stderr = ""
        return P()

    monkeypatch.setattr(loop.subprocess, "run", fake_run)
    loop._run_git_apply(["apply", "-"], "x\n")
    assert captured.get("timeout") == loop._GIT_APPLY_TIMEOUT_SECONDS


# ---------------------------------------------------- _run_git timeout tests
def test_run_git_timeout_with_check_false_returns_124(monkeypatch):
    import subprocess as sp

    def fake_run(*a, **kw):
        raise sp.TimeoutExpired(cmd="git checkout", timeout=kw.get("timeout"))

    monkeypatch.setattr(loop.subprocess, "run", fake_run)
    cp = loop._run_git("checkout", "--", ".", check=False)
    assert cp.returncode == 124
    assert "timed_out_after_" in cp.stderr


def test_run_git_timeout_with_check_true_raises(monkeypatch):
    import subprocess as sp

    def fake_run(*a, **kw):
        raise sp.TimeoutExpired(cmd="git status", timeout=kw.get("timeout"))

    monkeypatch.setattr(loop.subprocess, "run", fake_run)
    with pytest.raises(sp.TimeoutExpired):
        loop._run_git("status", check=True)


def test_run_git_passes_timeout_kwarg(monkeypatch):
    captured = {}

    def fake_run(args, *a, **kw):
        captured.update(kw)
        return loop.subprocess.CompletedProcess(
            args=args, returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr(loop.subprocess, "run", fake_run)
    loop._run_git("status", check=False)
    assert captured.get("timeout") == loop._GIT_CMD_TIMEOUT_SECONDS


def test_revert_changes_continues_through_timeouts(monkeypatch):
    """If both `git checkout` and `git clean` time out, _revert_changes
    must not raise — the loop's recovery path keeps going."""
    import subprocess as sp

    def fake_run(*a, **kw):
        raise sp.TimeoutExpired(cmd="git", timeout=kw.get("timeout"))

    monkeypatch.setattr(loop.subprocess, "run", fake_run)
    # Should NOT raise.
    loop._revert_changes()


# ----------------------------------------- "\ No newline at end of file"
def test_no_newline_marker_passes_safety_stack():
    """A diff containing `\\ No newline at end of file` markers (legitimate
    git-diff output for files without trailing newlines) must clear
    every safety check and not be misclassified as malformed."""
    import agent.loop as L

    diff = (
        "diff --git a/foo.py b/foo.py\n"
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -1 +1 @@\n"
        "-x = 1\n"
        "\\ No newline at end of file\n"
        "+x = 2\n"
        "\\ No newline at end of file\n"
    )
    assert L._has_unsafe_path(diff) is None
    assert L._has_binary_patch(diff) is None
    assert L._has_unsafe_mode(diff) is None
    assert L._has_structural_defect(diff) is None
    assert L._has_oversized_diff(diff) is None
    # And paths parse correctly to the single target file.
    assert set(L._diff_paths(diff)) == {"foo.py"}


# -------------------------------- precise Windows-drive detection
def _diff_with_path(p: str) -> str:
    return (
        f"diff --git a/{p} b/{p}\n"
        f"--- a/{p}\n"
        f"+++ b/{p}\n"
        "@@ -1 +1 @@\n"
        "-x = 1\n"
        "+x = 2\n"
    )


def test_posix_path_with_colon_is_allowed():
    """Filenames containing `:` are legal on POSIX. The drive-letter
    check must not over-match; only a leading ASCII letter + ':' is
    a Windows drive."""
    import agent.loop as L
    # `a:b.py` — second char is ':', first char is letter → flagged
    # by the old check. Now allowed because we additionally require
    # the colon is preceded by an ASCII letter at position 0 AND
    # nothing else; for unprefixed in-repo paths a `dir/a:b.py`
    # form is also legitimate.
    # The leading `a:` form is the borderline case — keep flagged
    # because it's indistinguishable from a Windows drive.
    # So we use a path with the colon NOT at position 1.
    assert L._has_unsafe_path(_diff_with_path("dir/note:1.py")) is None


def test_windows_drive_letter_still_rejected():
    import agent.loop as L
    out = L._has_unsafe_path(_diff_with_path("C:foo/bar.py"))
    assert out is not None and out.startswith("absolute_path:")
    out = L._has_unsafe_path(_diff_with_path("z:bar.py"))
    assert out is not None and out.startswith("absolute_path:")


def test_non_letter_colon_prefix_is_allowed():
    """A path like `1:foo.py` is a weird filename, not a Windows
    drive (drive letters are ASCII letters only)."""
    import agent.loop as L
    assert L._has_unsafe_path(_diff_with_path("1:foo.py")) is None


# ----------------------------------------- quoted-path bypass (loop 38)
def test_quoted_path_traversal_in_diff_git_header_rejected():
    diff = (
        'diff --git "a/../etc/passwd" "b/../etc/passwd"\n'
        '--- "a/../etc/passwd"\n'
        '+++ "b/../etc/passwd"\n'
        '@@ -0,0 +1 @@\n'
        '+x\n'
    )
    msg = loop._has_unsafe_path(diff)
    assert msg is not None and "path_traversal" in msg


def test_quoted_path_traversal_in_minus_plus_headers_rejected():
    diff = (
        'diff --git a/x.py b/x.py\n'
        '--- "a/../etc/secret"\n'
        '+++ "b/../etc/secret"\n'
        '@@ -0,0 +1 @@\n'
        '+x\n'
    )
    msg = loop._has_unsafe_path(diff)
    assert msg is not None and "path_traversal" in msg


def test_quoted_path_octal_escapes_decoded():
    # `caf\303\251.py` decodes to `café.py` — legal, no traversal.
    diff = (
        'diff --git "a/caf\\303\\251.py" "b/caf\\303\\251.py"\n'
        '--- "a/caf\\303\\251.py"\n'
        '+++ "b/caf\\303\\251.py"\n'
        '@@ -0,0 +1 @@\n'
        '+x\n'
    )
    paths = loop._diff_paths(diff)
    assert all(p == "café.py" for p in paths)
    assert loop._has_unsafe_path(diff) is None


def test_quoted_rename_to_traversal_rejected():
    diff = (
        'diff --git a/x.py b/x.py\n'
        'similarity index 100%\n'
        'rename from x.py\n'
        'rename to "../etc/passwd"\n'
    )
    msg = loop._has_unsafe_path(diff)
    assert msg is not None and "path_traversal" in msg


def test_quoted_path_with_space_decoded():
    # Path with a space — git wraps it in quotes.
    diff = (
        'diff --git "a/my file.py" "b/my file.py"\n'
        '--- "a/my file.py"\n'
        '+++ "b/my file.py"\n'
        '@@ -0,0 +1 @@\n'
        '+x\n'
    )
    paths = loop._diff_paths(diff)
    assert all(p == "my file.py" for p in paths)
    assert loop._has_unsafe_path(diff) is None


def test_unquoted_paths_still_parsed():
    # Loop 38 must not regress the plain unquoted case.
    diff = (
        'diff --git a/src/foo.py b/src/foo.py\n'
        '--- a/src/foo.py\n'
        '+++ b/src/foo.py\n'
        '@@ -0,0 +1 @@\n'
        '+x\n'
    )
    paths = loop._diff_paths(diff)
    assert all(p == "src/foo.py" for p in paths)
    assert loop._has_unsafe_path(diff) is None


def test_quoted_path_absolute_rejected():
    diff = (
        'diff --git "a//etc/passwd" "b//etc/passwd"\n'
        '--- "a//etc/passwd"\n'
        '+++ "b//etc/passwd"\n'
        '@@ -0,0 +1 @@\n'
        '+x\n'
    )
    msg = loop._has_unsafe_path(diff)
    assert msg is not None and "absolute_path" in msg
