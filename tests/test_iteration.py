"""End-to-end tests for `_iteration` orchestrator.

Builds a real per-test git repo, swaps in a scripted fake client, and
verifies every major branch:
  - clean (no issue)
  - rejected (devil rejects)
  - apply_failed (model output isn't a diff)
  - out_of_scope (diff touches a different file)
  - validation_failed (diff applies but produces invalid python)
  - applied (happy path; commits without push)
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _git(*args, cwd):
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )


class _ScriptedClient:
    """Returns a pre-baked queue of strings from `system_user`.

    Records each call so tests can introspect prompt assembly.
    """

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls: list[tuple[str, str, dict]] = []

    def system_user(self, system, user, **kw):
        self.calls.append((system, user, dict(kw)))
        if not self._replies:
            raise AssertionError("scripted client out of replies")
        return self._replies.pop(0)


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    """Initialise a real git repo and reroute every loop module path
    constant into it. Returns (loop_module, repo_root)."""
    _git("-c", "init.defaultBranch=main", "init", cwd=tmp_path)
    _git("config", "user.email", "t@t", cwd=tmp_path)
    _git("config", "user.name", "t", cwd=tmp_path)
    # Mirror production `.gitignore` so `.loop/` artefacts produced by
    # the loop itself (cursor.json, runtime.log) don't show up as
    # untracked changes and trigger spurious out-of-scope rejections.
    (tmp_path / ".gitignore").write_text(".loop/\nSTATE.md\n", "utf-8")
    (tmp_path / "f.py").write_text("x = 1\n", "utf-8")
    (tmp_path / "g.py").write_text("y = 1\n", "utf-8")
    _git("add", "-A", cwd=tmp_path)
    _git("commit", "-m", "init", cwd=tmp_path)

    import agent.loop as L

    monkeypatch.setattr(L, "_REPO", tmp_path)
    monkeypatch.setattr(L, "LOOP_DIR", tmp_path / ".loop")
    monkeypatch.setattr(L, "HISTORY_DIR", tmp_path / ".loop" / "history")
    monkeypatch.setattr(L, "CURSOR_FILE", tmp_path / ".loop" / "cursor.json")
    monkeypatch.setattr(L, "LOG_FILE", tmp_path / ".loop" / "runtime.log")
    monkeypatch.setattr(L, "STATE_FILE", tmp_path / "STATE.md")
    return L, tmp_path


def _diff_for(file: str, before: str, after: str) -> str:
    return (
        f"diff --git a/{file} b/{file}\n"
        f"--- a/{file}\n"
        f"+++ b/{file}\n"
        "@@ -1 +1 @@\n"
        f"-{before}\n"
        f"+{after}\n"
    )


# ----------------------------------------------------------- clean branch
def test_clean_when_no_issue(env):
    L, repo = env
    # Empty/whitespace response -> _parse_first_issue returns None -> clean.
    client = _ScriptedClient(["\n   \n"])
    out = L._iteration(client, max_bytes=10_000, push=False)
    assert out.startswith("clean:")


# ------------------------------------------------------- rejected branch
def test_devil_rejects(env):
    L, repo = env
    client = _ScriptedClient([
        "- Variable name is unclear and could be confusing\n",
        _diff_for("f.py", "x = 1", "x = 2"),
        "VERDICT: REJECT — too speculative\n",
    ])
    out = L._iteration(client, max_bytes=10_000, push=False)
    assert out.startswith("rejected:")
    # State file should record the rejection.
    assert "rejected fix" in (repo / "STATE.md").read_text("utf-8")
    # Tree must be untouched.
    assert (repo / "f.py").read_text("utf-8") == "x = 1\n"


# --------------------------------------------------- apply_failed branch
def test_apply_failed_when_model_returns_prose(env):
    L, repo = env
    client = _ScriptedClient([
        "- Something looks off\n",
        "Sure, here is some prose, not a diff.\n",
        "VERDICT: ACCEPT\n",
    ])
    out = L._iteration(client, max_bytes=10_000, push=False)
    assert out.startswith("apply_failed:")


# --------------------------------------------------- out_of_scope branch
def test_out_of_scope_diff_is_reverted(env):
    """Targeted file is f.py but the diff touches g.py only.

    `_diff_in_scope` must catch this and `_revert_changes` must restore
    g.py to its committed state.
    """
    L, repo = env
    # Force iteration onto f.py by pre-seeding the cursor.
    L._save_cursor(0)  # candidate_files() is sorted; index 0 = f.py
    client = _ScriptedClient([
        "- f.py has an issue\n",
        _diff_for("g.py", "y = 1", "y = 999"),
        "VERDICT: ACCEPT\n",
    ])
    out = L._iteration(client, max_bytes=10_000, push=False)
    assert out.startswith("out_of_scope:")
    assert (repo / "g.py").read_text("utf-8") == "y = 1\n"


# ----------------------------------------------- validation_failed branch
def test_validation_failed_when_diff_breaks_python(env):
    L, repo = env
    L._save_cursor(0)  # f.py
    bad_diff = (
        "diff --git a/f.py b/f.py\n"
        "--- a/f.py\n"
        "+++ b/f.py\n"
        "@@ -1 +1 @@\n"
        "-x = 1\n"
        "+x = (\n"
    )
    client = _ScriptedClient([
        "- broken syntax\n",
        bad_diff,
        "VERDICT: ACCEPT\n",
    ])
    out = L._iteration(client, max_bytes=10_000, push=False)
    assert out.startswith("validation_failed:")
    assert (repo / "f.py").read_text("utf-8") == "x = 1\n"


# ---------------------------------------------------------- happy path
def test_applied_branch_commits_without_push(env):
    L, repo = env
    L._save_cursor(0)  # f.py
    client = _ScriptedClient([
        "- x is set to a magic number\n",
        _diff_for("f.py", "x = 1", "x = 2"),
        "VERDICT: ACCEPT — looks safe\n",
    ])
    out = L._iteration(client, max_bytes=10_000, push=False)
    assert out.startswith("applied:")
    assert (repo / "f.py").read_text("utf-8") == "x = 2\n"

    # Exactly one new commit on top of the seed.
    log = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip().splitlines()
    assert len(log) == 2

    # Devil's advocate ran (3 model calls total).
    assert len(client.calls) == 3
    # The third call's system prompt is the devil prompt.
    devil_system = client.calls[2][0]
    assert "devil" in devil_system.lower() or "advocate" in devil_system.lower()


def test_applied_path_works_without_gitignore(env):
    """Belt-and-suspenders: even with no `.gitignore` at all, loop-internal
    paths (`.loop/`, `STATE.md`) must not poison the scope check.

    Before the loop-11 fix, removing `.gitignore` made every iteration
    mark itself as out_of_scope because cursor.json/runtime.log were
    untracked and counted as changes.
    """
    L, repo = env
    (repo / ".gitignore").unlink()
    subprocess.run(
        ["git", "commit", "-am", "drop gitignore"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )

    L._save_cursor(0)  # f.py
    client = _ScriptedClient([
        "- x is set to a magic number\n",
        _diff_for("f.py", "x = 1", "x = 2"),
        "VERDICT: ACCEPT\n",
    ])
    out = L._iteration(client, max_bytes=10_000, push=False)
    assert out.startswith("applied:"), out
    assert (repo / "f.py").read_text("utf-8") == "x = 2\n"
