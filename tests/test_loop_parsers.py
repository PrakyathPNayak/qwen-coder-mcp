"""Tests for the parser / diff / syntax helpers in `agent.loop`.

These helpers are the loop's contract with model output. They must be
robust to common formatting drift the model emits.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent import loop as L


# ----------------------------------------------------------- _strip_fence
class TestStripFence:
    def test_returns_inner_for_clean_fenced_block(self):
        text = "```python\nprint('hi')\n```"
        assert L._strip_fence(text) == "print('hi')"

    def test_returns_inner_for_diff_fence(self):
        text = "```diff\n--- a/x\n+++ b/x\n@@\n-1\n+2\n```"
        assert L._strip_fence(text) == "--- a/x\n+++ b/x\n@@\n-1\n+2"

    def test_unfenced_text_is_returned_unchanged(self):
        text = "no fence here"
        assert L._strip_fence(text) == "no fence here"

    def test_strips_outer_whitespace(self):
        text = "\n\n```\nbody\n```\n\n"
        assert L._strip_fence(text) == "body"

    def test_empty_string(self):
        assert L._strip_fence("") == ""

    def test_prose_before_fence_extracts_inner(self):
        text = "Here is the patch you asked for:\n\n```diff\nbody line\n```"
        assert L._strip_fence(text) == "body line"

    def test_prose_after_fence_extracts_inner(self):
        text = "```diff\nbody line\n```\n\nHope this helps!"
        assert L._strip_fence(text) == "body line"

    def test_prose_both_sides(self):
        text = "Sure thing.\n```\nbody\n```\nLet me know if you have questions."
        assert L._strip_fence(text) == "body"

    def test_multiple_fences_returns_first(self):
        text = (
            "```diff\nfirst body\n```\n\n"
            "Also note this example:\n"
            "```python\nsecond body\n```"
        )
        assert L._strip_fence(text) == "first body"

    def test_raw_unified_diff_returned_as_is(self):
        text = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@\n-1\n+2\n"
        assert L._strip_fence(text).startswith("diff --git")

    def test_raw_minus_minus_diff_returned_as_is(self):
        text = "--- a/x\n+++ b/x\n@@\n-1\n+2\n"
        assert L._strip_fence(text).startswith("--- a/x")

    def test_no_fence_no_diff_returns_stripped_original(self):
        text = "  just prose with no markers  "
        assert L._strip_fence(text) == "just prose with no markers"


# ------------------------------------------------------ _parse_first_issue
class TestParseFirstIssue:
    def test_no_issues_sentinel_returns_none(self):
        assert L._parse_first_issue("NO_ISSUES") is None

    def test_no_issues_sentinel_case_insensitive(self):
        assert L._parse_first_issue("no_issues") is None

    def test_no_issues_with_trailing_text(self):
        assert L._parse_first_issue("NO_ISSUES — looks good") is None

    def test_empty_returns_none(self):
        assert L._parse_first_issue("") is None
        assert L._parse_first_issue("   \n   ") is None

    def test_numbered_list_picks_first(self):
        text = "1. Off-by-one in foo\n2. Memory leak in bar\n"
        assert L._parse_first_issue(text) == "Off-by-one in foo"

    def test_numbered_list_with_paren(self):
        text = "1) First problem\n2) Second\n"
        assert L._parse_first_issue(text) == "First problem"

    def test_numbered_list_multiline_first_item(self):
        text = "1. First problem\n   continuation line\n2. Second\n"
        out = L._parse_first_issue(text)
        assert out is not None
        assert out.startswith("First problem")
        assert "continuation line" in out

    def test_bullet_list_fallback(self):
        text = "- alpha bug\n- beta bug\n"
        assert L._parse_first_issue(text) == "alpha bug"

    def test_plain_prose_fallback_first_line(self):
        text = "There is a race condition in foo()."
        assert L._parse_first_issue(text) == "There is a race condition in foo()."

    # ---- benign no-issue replies must not become spurious "issues" ----
    @pytest.mark.parametrize(
        "reply",
        [
            "No issues found.",
            "No issues.",
            "No bugs found in this file.",
            "No bugs.",
            "No problems with the code.",
            "No findings.",
            "No defects found.",
            "Looks good.",
            "Looks good to me.",
            "Looks fine.",
            "looks ok",
            "Everything looks good.",
            "This code looks correct.",
            "The code looks clean.",
            "LGTM",
            "lgtm.",
            "Nothing to fix.",
            "Nothing to change.",
            "Nothing wrong.",
            "Clean.",
            "All good.",
            "  No issues found.   ",  # leading/trailing whitespace
        ],
    )
    def test_benign_no_issue_replies_return_none(self, reply):
        assert L._parse_first_issue(reply) is None, (
            f"benign reply was misread as an issue: {reply!r}"
        )

    def test_real_issue_containing_word_no_is_still_parsed(self):
        """A bullet that mentions 'no' must not be swallowed by the
        no-issue short-circuit."""
        text = "- There is no bound check on the index variable."
        out = L._parse_first_issue(text)
        assert out == "There is no bound check on the index variable."

    def test_multiline_with_bullet_after_benign_intro_uses_bullet(self):
        """Defensive: model says 'looks good but...' then lists. The
        bullet must win over the benign-intro short-circuit because the
        text contains list markers."""
        text = "Looks fine, but here are some issues:\n- Off-by-one in foo()\n"
        assert L._parse_first_issue(text) == "Off-by-one in foo()"


# ---------------------------------------------------------- _verdict_accepts
class TestVerdictAccepts:
    def test_accept(self):
        ok, reason = L._verdict_accepts("Looks good.\nVERDICT: ACCEPT")
        assert ok is True
        assert reason == "accept"

    def test_reject(self):
        ok, reason = L._verdict_accepts("VERDICT: REJECT — wrong scope")
        assert ok is False
        assert "wrong scope" in reason

    def test_no_verdict_is_rejected(self):
        ok, reason = L._verdict_accepts("hmm")
        assert ok is False
        assert reason == "no_verdict"

    def test_lowercase_verdict_token(self):
        ok, _ = L._verdict_accepts("verdict: accept")
        assert ok is True

    def test_extra_spaces_around_colon_accept(self):
        ok, _ = L._verdict_accepts("VERDICT : ACCEPT")
        assert ok is True

    def test_no_space_after_colon_accept(self):
        ok, _ = L._verdict_accepts("VERDICT:ACCEPT")
        assert ok is True

    def test_multiple_spaces_after_colon_accept(self):
        ok, _ = L._verdict_accepts("VERDICT:   ACCEPT")
        assert ok is True

    def test_extra_spaces_around_colon_reject(self):
        ok, reason = L._verdict_accepts("VERDICT : REJECT scope is wrong")
        assert ok is False
        assert "scope is wrong" in reason

    def test_reject_reason_truncated_to_first_line(self):
        text = "VERDICT: REJECT short reason\nthen pages of rambling commentary..."
        ok, reason = L._verdict_accepts(text)
        assert ok is False
        assert reason == "short reason"

    def test_accept_word_boundary_does_not_match_acceptance(self):
        """`VERDICT: ACCEPTANCE` is not a valid verdict; word boundary
        should prevent the false-accept. A reject ramble that includes
        the word 'acceptance' must not fire."""
        ok, _ = L._verdict_accepts(
            "VERDICT: REJECT scope creep, no clear path to ACCEPTANCE here"
        )
        assert ok is False

    def test_reject_word_boundary(self):
        ok, reason = L._verdict_accepts("VERDICT: REJECTED for cause")
        # `REJECTED` should not match `REJECT\b`; this falls through to
        # no_verdict (conservative reject).
        assert ok is False
        assert reason == "no_verdict"


# ---------------------------------------------------------------- _apply_diff
@pytest.fixture
def repo_root(monkeypatch, tmp_path: Path):
    """Initialise a throwaway git repo and rebind agent.loop._REPO."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True
    )
    target = tmp_path / "hello.py"
    target.write_text("print('a')\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-q", "-m", "init"], check=True
    )
    monkeypatch.setattr(L, "_REPO", tmp_path)
    return tmp_path


class TestApplyDiff:
    def test_rejects_non_diff(self, repo_root):
        ok, msg = L._apply_diff("hello world")
        assert ok is False
        assert msg == "not_a_unified_diff"

    def test_rejects_diff_with_garbage_context(self, repo_root):
        ok, msg = L._apply_diff(
            "diff --git a/hello.py b/hello.py\n--- a/hello.py\n+++ b/hello.py\n"
            "@@ -1 +1 @@\n-print('NOT THE REAL LINE')\n+print('b')\n"
        )
        assert ok is False
        assert msg.startswith("apply_check_failed")

    def test_applies_clean_diff(self, repo_root: Path):
        diff = (
            "diff --git a/hello.py b/hello.py\n"
            "--- a/hello.py\n"
            "+++ b/hello.py\n"
            "@@ -1 +1 @@\n"
            "-print('a')\n"
            "+print('b')\n"
        )
        ok, msg = L._apply_diff(diff)
        assert ok is True, msg
        assert (repo_root / "hello.py").read_text() == "print('b')\n"

    def test_unwraps_fenced_diff(self, repo_root: Path):
        diff = (
            "```diff\n"
            "diff --git a/hello.py b/hello.py\n"
            "--- a/hello.py\n"
            "+++ b/hello.py\n"
            "@@ -1 +1 @@\n"
            "-print('a')\n"
            "+print('b')\n"
            "```"
        )
        ok, msg = L._apply_diff(diff)
        assert ok is True, msg
        assert (repo_root / "hello.py").read_text() == "print('b')\n"


# ------------------------------------------------------- _validate_changed_files
class TestValidateChangedFiles:
    def test_no_validatable_paths_is_ok(self, tmp_path, monkeypatch):
        monkeypatch.setattr(L, "_REPO", tmp_path)
        ok, _ = L._validate_changed_files([Path("README.md")])
        assert ok is True

    def test_valid_py_passes(self, tmp_path, monkeypatch):
        (tmp_path / "good.py").write_text("x = 1\n")
        monkeypatch.setattr(L, "_REPO", tmp_path)
        ok, msg = L._validate_changed_files([Path("good.py")])
        assert ok is True, msg

    def test_python_syntax_error_fails(self, tmp_path, monkeypatch):
        (tmp_path / "bad.py").write_text("def f(:\n  pass\n")
        monkeypatch.setattr(L, "_REPO", tmp_path)
        ok, msg = L._validate_changed_files([Path("bad.py")])
        assert ok is False
        assert msg.startswith("py_invalid")

    def test_json_invalid_fails(self, tmp_path, monkeypatch):
        (tmp_path / "x.json").write_text("{ not json")
        monkeypatch.setattr(L, "_REPO", tmp_path)
        ok, msg = L._validate_changed_files([Path("x.json")])
        assert ok is False
        assert "json_invalid" in msg

    def test_json_valid_passes(self, tmp_path, monkeypatch):
        (tmp_path / "x.json").write_text('{"a": 1}')
        monkeypatch.setattr(L, "_REPO", tmp_path)
        ok, _ = L._validate_changed_files([Path("x.json")])
        assert ok is True

    def test_toml_invalid_fails(self, tmp_path, monkeypatch):
        (tmp_path / "p.toml").write_text("name = 'unterminated\n[oops")
        monkeypatch.setattr(L, "_REPO", tmp_path)
        ok, msg = L._validate_changed_files([Path("p.toml")])
        assert ok is False
        assert "toml_invalid" in msg

    def test_toml_valid_passes(self, tmp_path, monkeypatch):
        (tmp_path / "p.toml").write_text("[a]\nb = 1\n")
        monkeypatch.setattr(L, "_REPO", tmp_path)
        ok, _ = L._validate_changed_files([Path("p.toml")])
        assert ok is True

    def test_missing_file_skipped(self, tmp_path, monkeypatch):
        # a path can be in `git diff --name-only` because it was deleted —
        # validation must not crash on a non-existent path.
        monkeypatch.setattr(L, "_REPO", tmp_path)
        ok, _ = L._validate_changed_files([Path("gone.json")])
        assert ok is True


# ------------------------------------------------------- _python_syntax_ok
class TestPythonSyntaxOk:
    """Kept for backward compatibility — alias of _validate_changed_files."""

    def test_no_py_paths_is_ok(self, tmp_path, monkeypatch):
        monkeypatch.setattr(L, "_REPO", tmp_path)
        ok, _ = L._python_syntax_ok([Path("README.md")])
        assert ok is True

    def test_valid_py_passes(self, tmp_path, monkeypatch):
        (tmp_path / "good.py").write_text("x = 1\n")
        monkeypatch.setattr(L, "_REPO", tmp_path)
        ok, msg = L._python_syntax_ok([Path("good.py")])
        assert ok is True, msg

    def test_syntax_error_fails(self, tmp_path, monkeypatch):
        (tmp_path / "bad.py").write_text("def f(:\n  pass\n")
        monkeypatch.setattr(L, "_REPO", tmp_path)
        ok, _ = L._python_syntax_ok([Path("bad.py")])
        assert ok is False


# ----------------------------------------------------- unclosed-fence salvage
class TestStripFenceUnclosedSalvage:
    def test_unclosed_fence_with_lang_returns_body(self):
        text = "```diff\n--- a/x\n+++ b/x\n@@\n-1\n+2"
        assert L._strip_fence(text) == "--- a/x\n+++ b/x\n@@\n-1\n+2"

    def test_unclosed_bare_fence_returns_body(self):
        text = "```\nbody line one\nbody line two"
        assert L._strip_fence(text) == "body line one\nbody line two"

    def test_unclosed_fence_with_prose_before_does_not_salvage(self):
        """Salvage only fires when text *starts* with ```; prose before
        means the model didn't try a single block at all."""
        text = "Here you go:\n```diff\n--- a/x\n+++ b/x"
        # Falls through to return-as-is; downstream rejects as not_a_unified_diff
        assert L._strip_fence(text).startswith("Here you go:")

    def test_open_fence_with_trailing_close_still_salvageable(self):
        text = "```diff\nbody\n```"  # already covered by inner regex, sanity
        assert L._strip_fence(text) == "body"

    def test_unclosed_fence_strips_dangling_close_at_end(self):
        text = "```diff\nbody1\nbody2\n```"
        assert L._strip_fence(text) == "body1\nbody2"
