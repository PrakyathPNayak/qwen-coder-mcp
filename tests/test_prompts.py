"""Contract tests for `prompts.py`.

These tests are deliberately strict on the *critical* instructions in
each prompt — the ones the loop's parsers rely on. A regression that
silently drops one of these sentences would make every iteration go
round in circles (every fix `not_a_unified_diff`, every review missing
NO_ISSUES, every verdict `no_verdict`) without any test failure.

The tests stay loose on incidental phrasing so that minor copy edits
don't trigger spurious failures.
"""
from __future__ import annotations

import re

from qwen_coder_mcp import prompts


# -------------------------------------------------------------- system prompts
def test_coder_system_demands_diff_only_for_diff_requests():
    text = prompts.CODER_SYSTEM.lower()
    assert "diff" in text
    # When asked for a diff, must say "single" valid unified diff.
    assert "unified diff" in text
    assert "nothing else" in text or "only" in text


def test_reviewer_system_requires_numbered_list():
    text = prompts.REVIEWER_SYSTEM.lower()
    assert "numbered list" in text
    # Must mention real bugs, not just style.
    assert "bug" in text


def test_devils_advocate_system_specifies_verdict_grammar():
    text = prompts.DEVILS_ADVOCATE_SYSTEM
    # The exact tokens the parser looks for.
    assert "VERDICT: ACCEPT" in text
    assert "VERDICT: REJECT" in text


# ----------------------------------------------------------- user-prompt builders
def test_find_bugs_user_includes_path_and_no_issues_sentinel():
    out = prompts.find_bugs_user("src/foo.py", "x = 1\n")
    assert "src/foo.py" in out
    assert "x = 1" in out
    # The exact token the parser looks for.
    assert "NO_ISSUES" in out
    # Must request a numbered list (matches reviewer system).
    assert "numbered list" in out.lower()


def test_propose_fix_user_demands_unified_diff_with_headers():
    out = prompts.propose_fix_user("src/foo.py", "x = 1\n", "rename x to y")
    assert "src/foo.py" in out
    assert "rename x to y" in out
    # Critical: the parser only accepts diffs starting with "diff --git" or "--- ".
    assert "--- a/" in out
    assert "+++ b/" in out
    assert "unified diff" in out.lower()
    # Single-fix discipline.
    assert re.search(r"single|only", out, re.IGNORECASE)


def test_devils_advocate_user_includes_diff_and_demands_verdict():
    out = prompts.devils_advocate_user(
        "src/foo.py", "original\n", "diff --git a/src/foo.py b/src/foo.py\n",
        "the issue",
    )
    assert "src/foo.py" in out
    assert "the issue" in out
    assert "diff --git" in out
    # Must instruct the model on the exact verdict grammar.
    assert "VERDICT: ACCEPT" in out
    assert "VERDICT: REJECT" in out


# ----------------------------------------------- the smaller MCP-tool builders
def test_explain_user_wraps_code_in_fence():
    out = prompts.explain_user("print(1)\n")
    assert "print(1)" in out
    assert out.count("```") >= 2


def test_complete_user_with_and_without_instruction():
    out_no_goal = prompts.complete_user("def f():\n    pass\n", None)
    out_goal = prompts.complete_user("def f():\n    pass\n", "make it return 42")
    assert "def f():" in out_no_goal
    assert "Goal" not in out_no_goal
    assert "make it return 42" in out_goal
    assert "Goal" in out_goal


def test_refactor_user_includes_goal_and_preserves_behavior_warning():
    out = prompts.refactor_user("x = 1\n", "use enums")
    assert "use enums" in out
    assert "Preserve" in out or "preserve" in out


def test_write_tests_user_includes_framework():
    out = prompts.write_tests_user("def f(): pass\n", "pytest")
    assert "pytest" in out


def test_summarize_repo_user_includes_tree():
    out = prompts.summarize_repo_user("README.md\nsrc/foo.py\n")
    assert "README.md" in out
    assert "src/foo.py" in out


# -------------------------------------------------- model-name independence
def test_path_is_quoted_safely_in_find_bugs_user():
    """Paths containing markdown-special chars don't break formatting."""
    out = prompts.find_bugs_user("src/weird name (1).py", "x=1\n")
    assert "src/weird name (1).py" in out
