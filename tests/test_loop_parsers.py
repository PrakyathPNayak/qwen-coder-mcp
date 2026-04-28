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


# ------------------------------------------------------- _python_syntax_ok
class TestPythonSyntaxOk:
    def test_no_py_paths_is_ok(self, tmp_path, monkeypatch):
        monkeypatch.setattr(L, "_REPO", tmp_path)
        ok, msg = L._python_syntax_ok([Path("README.md")])
        assert ok is True
        assert msg == "no_py"

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
