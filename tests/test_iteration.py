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

import os
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


# --------------------------------------------------- iteration budget
def test_budget_helper_default(monkeypatch):
    import agent.loop as L
    monkeypatch.delenv("QWEN_LOOP_ITER_BUDGET_S", raising=False)
    assert L._iteration_budget_seconds() == 600.0


def test_budget_helper_env_override(monkeypatch):
    import agent.loop as L
    monkeypatch.setenv("QWEN_LOOP_ITER_BUDGET_S", "12.5")
    assert L._iteration_budget_seconds() == 12.5


def test_budget_helper_invalid_falls_back(monkeypatch):
    import agent.loop as L
    monkeypatch.setenv("QWEN_LOOP_ITER_BUDGET_S", "not-a-number")
    assert L._iteration_budget_seconds() == 600.0
    monkeypatch.setenv("QWEN_LOOP_ITER_BUDGET_S", "0")
    assert L._iteration_budget_seconds() == 600.0
    monkeypatch.setenv("QWEN_LOOP_ITER_BUDGET_S", "-5")
    assert L._iteration_budget_seconds() == 600.0


def test_iteration_aborts_on_budget_after_find_bugs(env, monkeypatch):
    """If the deadline is exceeded after the first model call, the
    iteration must NOT proceed to ask for a fix."""
    L, _repo = env
    # Drive the clock manually: first read sets the deadline, every
    # subsequent read returns a value far past it.
    import itertools
    ticks = itertools.chain([1000.0], itertools.repeat(9999.0))
    monkeypatch.setattr(L.time, "monotonic", lambda: next(ticks))
    monkeypatch.setenv("QWEN_LOOP_ITER_BUDGET_S", "1")
    # Only one reply needed — second call should never be reached.
    client = _ScriptedClient(["- something is off\n"])
    out = L._iteration(client, max_bytes=10_000, push=False)
    assert out.startswith("budget_exceeded:"), out
    assert "after_find_bugs" in out
    # Only the first call was issued.
    assert len(client.calls) == 1


# ---------- iteration budget clamp (loop 39)
def test_iteration_budget_clamps_absurd_value(monkeypatch):
    from agent import loop
    monkeypatch.setenv("QWEN_LOOP_ITER_BUDGET_S", "999999999")
    # 24 hours = 86400 seconds
    assert loop._iteration_budget_seconds() == 24 * 60 * 60.0


def test_iteration_budget_at_cap_is_kept(monkeypatch):
    from agent import loop
    monkeypatch.setenv("QWEN_LOOP_ITER_BUDGET_S", str(24 * 60 * 60))
    assert loop._iteration_budget_seconds() == 24 * 60 * 60.0


def test_iteration_budget_just_under_cap(monkeypatch):
    from agent import loop
    monkeypatch.setenv("QWEN_LOOP_ITER_BUDGET_S", "3600")
    assert loop._iteration_budget_seconds() == 3600.0


class TestPhaseTimer:
    """Loop 48: per-phase timing helper."""

    def test_phase_timer_records_elapsed(self):
        from agent import loop as L
        phases: dict[str, float] = {}
        with L._PhaseTimer(phases, "x"):
            pass
        assert "x" in phases
        assert phases["x"] >= 0.0

    def test_phase_timer_records_even_on_exception(self):
        from agent import loop as L
        phases: dict[str, float] = {}
        try:
            with L._PhaseTimer(phases, "y"):
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        assert "y" in phases

    def test_write_timing_appends_jsonl(self, tmp_path, monkeypatch):
        from agent import loop as L
        monkeypatch.setattr(L, "TIMING_FILE", tmp_path / "timing.log")
        L._write_timing(Path("a.py"), "applied:a.py", {"find_bugs": 1.5})
        L._write_timing(Path("b.py"), "clean:b.py", {"find_bugs": 0.7})
        lines = (tmp_path / "timing.log").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        import json
        rec0 = json.loads(lines[0])
        assert rec0["file"] == "a.py"
        assert rec0["outcome"] == "applied:a.py"
        assert rec0["phases"]["find_bugs"] == 1.5

    def test_write_timing_swallows_io_error(self, tmp_path, monkeypatch):
        from agent import loop as L
        # Point at a path whose parent cannot be created (a regular file)
        bogus_parent = tmp_path / "not_a_dir"
        bogus_parent.write_text("x")
        monkeypatch.setattr(L, "TIMING_FILE", bogus_parent / "child.log")
        # Must not raise
        L._write_timing(Path("a.py"), "x", {})


class TestTimingRotation:
    """Loop 49: .loop/timing.log rotates when oversized."""

    def test_rotation_default_cap(self):
        from agent import loop as L
        assert L._timing_max_bytes() == L._TIMING_MAX_BYTES_DEFAULT

    def test_rotation_env_override(self, monkeypatch):
        from agent import loop as L
        monkeypatch.setenv("QWEN_TIMING_MAX_BYTES", "5000")
        assert L._timing_max_bytes() == 5000

    def test_rotation_env_clamped_to_cap(self, monkeypatch):
        from agent import loop as L
        monkeypatch.setenv("QWEN_TIMING_MAX_BYTES", "9999999999")
        assert L._timing_max_bytes() == L._TIMING_MAX_BYTES_CAP

    def test_rotation_env_invalid_falls_back(self, monkeypatch):
        from agent import loop as L
        monkeypatch.setenv("QWEN_TIMING_MAX_BYTES", "not-a-number")
        assert L._timing_max_bytes() == L._TIMING_MAX_BYTES_DEFAULT

    def test_rotation_env_nonpositive_falls_back(self, monkeypatch):
        from agent import loop as L
        monkeypatch.setenv("QWEN_TIMING_MAX_BYTES", "0")
        assert L._timing_max_bytes() == L._TIMING_MAX_BYTES_DEFAULT

    def test_rotate_when_undersized_is_noop(self, tmp_path, monkeypatch):
        from agent import loop as L
        f = tmp_path / "timing.log"
        f.write_text("small\n")
        monkeypatch.setattr(L, "TIMING_FILE", f)
        monkeypatch.setenv("QWEN_TIMING_MAX_BYTES", "1000000")
        L._rotate_timing_if_oversized()
        assert f.exists()
        assert not (tmp_path / "timing.log.1").exists()

    def test_rotate_when_oversized_renames(self, tmp_path, monkeypatch):
        from agent import loop as L
        f = tmp_path / "timing.log"
        f.write_text("x" * 1000)
        monkeypatch.setattr(L, "TIMING_FILE", f)
        monkeypatch.setenv("QWEN_TIMING_MAX_BYTES", "100")
        L._rotate_timing_if_oversized()
        assert not f.exists()
        rotated = tmp_path / "timing.log.1"
        assert rotated.exists()
        assert rotated.read_text() == "x" * 1000

    def test_rotate_overwrites_existing_rotated(self, tmp_path, monkeypatch):
        from agent import loop as L
        f = tmp_path / "timing.log"
        f.write_text("x" * 1000)
        rotated = tmp_path / "timing.log.1"
        rotated.write_text("OLD")
        monkeypatch.setattr(L, "TIMING_FILE", f)
        monkeypatch.setenv("QWEN_TIMING_MAX_BYTES", "100")
        L._rotate_timing_if_oversized()
        assert rotated.read_text() == "x" * 1000

    def test_rotate_missing_file_is_noop(self, tmp_path, monkeypatch):
        from agent import loop as L
        f = tmp_path / "no.log"
        monkeypatch.setattr(L, "TIMING_FILE", f)
        L._rotate_timing_if_oversized()  # must not raise
        assert not f.exists()

    def test_write_timing_triggers_rotation(self, tmp_path, monkeypatch):
        from agent import loop as L
        f = tmp_path / "timing.log"
        f.write_text("x" * 5000)
        monkeypatch.setattr(L, "TIMING_FILE", f)
        monkeypatch.setenv("QWEN_TIMING_MAX_BYTES", "100")
        L._write_timing(Path("a.py"), "x", {"p": 0.1})
        assert (tmp_path / "timing.log.1").exists()
        assert f.exists()  # new short file
        assert f.stat().st_size < 1000


class TestRuntimeLogRotation:
    """Loop 50: .loop/runtime.log rotates when oversized."""

    def test_runtime_default_cap(self):
        from agent import loop as L
        assert L._runtime_log_max_bytes() == L._RUNTIME_LOG_MAX_BYTES_DEFAULT

    def test_runtime_env_override(self, monkeypatch):
        from agent import loop as L
        monkeypatch.setenv("QWEN_RUNTIME_LOG_MAX_BYTES", "12345")
        assert L._runtime_log_max_bytes() == 12345

    def test_runtime_env_clamped(self, monkeypatch):
        from agent import loop as L
        monkeypatch.setenv("QWEN_RUNTIME_LOG_MAX_BYTES", "9999999999")
        assert L._runtime_log_max_bytes() == L._RUNTIME_LOG_MAX_BYTES_CAP

    def test_runtime_env_invalid_falls_back(self, monkeypatch):
        from agent import loop as L
        monkeypatch.setenv("QWEN_RUNTIME_LOG_MAX_BYTES", "abc")
        assert L._runtime_log_max_bytes() == L._RUNTIME_LOG_MAX_BYTES_DEFAULT

    def test_log_triggers_rotation(self, tmp_path, monkeypatch):
        from agent import loop as L
        f = tmp_path / "runtime.log"
        f.write_text("x" * 5000)
        monkeypatch.setattr(L, "LOG_FILE", f)
        monkeypatch.setenv("QWEN_RUNTIME_LOG_MAX_BYTES", "100")
        L._log("trigger")
        assert (tmp_path / "runtime.log.1").exists()
        assert f.stat().st_size < 1000

    def test_generic_rotation_helper_overwrites_old(self, tmp_path):
        from agent import loop as L
        f = tmp_path / "x.log"
        f.write_text("y" * 500)
        rotated = tmp_path / "x.log.1"
        rotated.write_text("STALE")
        L._rotate_log_if_oversized(f, 100)
        assert rotated.read_text() == "y" * 500
        assert not f.exists()

    def test_generic_rotation_helper_undersized_noop(self, tmp_path):
        from agent import loop as L
        f = tmp_path / "x.log"
        f.write_text("hi")
        L._rotate_log_if_oversized(f, 1000)
        assert f.exists()
        assert not (tmp_path / "x.log.1").exists()


class TestHistoryRetention:
    """Loop 52: .loop/history/*.md is bounded."""

    def test_history_default_cap(self):
        from agent import loop as L
        assert L._history_max_files() == L._HISTORY_MAX_FILES_DEFAULT

    def test_history_env_override(self, monkeypatch):
        from agent import loop as L
        monkeypatch.setenv("QWEN_HISTORY_MAX_FILES", "10")
        assert L._history_max_files() == 10

    def test_history_env_clamped(self, monkeypatch):
        from agent import loop as L
        monkeypatch.setenv("QWEN_HISTORY_MAX_FILES", "9999999")
        assert L._history_max_files() == L._HISTORY_MAX_FILES_CAP

    def test_history_env_invalid_falls_back(self, monkeypatch):
        from agent import loop as L
        monkeypatch.setenv("QWEN_HISTORY_MAX_FILES", "junk")
        assert L._history_max_files() == L._HISTORY_MAX_FILES_DEFAULT

    def test_prune_noop_when_under_cap(self, tmp_path, monkeypatch):
        from agent import loop as L
        monkeypatch.setattr(L, "HISTORY_DIR", tmp_path)
        for i in range(3):
            (tmp_path / f"{i}.md").write_text("x")
        deleted = L._prune_history(10)
        assert deleted == 0
        assert len(list(tmp_path.iterdir())) == 3

    def test_prune_deletes_oldest(self, tmp_path, monkeypatch):
        from agent import loop as L
        import time as _t
        monkeypatch.setattr(L, "HISTORY_DIR", tmp_path)
        # Create 5 files with strictly increasing mtimes
        for i in range(5):
            f = tmp_path / f"{i}.md"
            f.write_text(str(i))
            os.utime(f, (1000 + i, 1000 + i))
        deleted = L._prune_history(2)
        assert deleted == 3
        survivors = sorted(p.name for p in tmp_path.iterdir())
        assert survivors == ["3.md", "4.md"]

    def test_prune_missing_dir_is_noop(self, tmp_path, monkeypatch):
        from agent import loop as L
        monkeypatch.setattr(L, "HISTORY_DIR", tmp_path / "nope")
        assert L._prune_history(10) == 0

    def test_write_history_triggers_prune(self, tmp_path, monkeypatch):
        from agent import loop as L
        import time as _t
        monkeypatch.setattr(L, "HISTORY_DIR", tmp_path)
        monkeypatch.setenv("QWEN_HISTORY_MAX_FILES", "2")
        for i in range(3):
            f = tmp_path / f"{i}.md"
            f.write_text("seed")
            os.utime(f, (1000 + i, 1000 + i))
        # write a 4th via _write_history, should keep most-recent 2
        L._write_history("4.md", "new")
        survivors = sorted(p.name for p in tmp_path.iterdir())
        assert survivors == ["2.md", "4.md"]

    def test_prune_skips_subdirectories(self, tmp_path, monkeypatch):
        from agent import loop as L
        monkeypatch.setattr(L, "HISTORY_DIR", tmp_path)
        (tmp_path / "subdir").mkdir()
        (tmp_path / "a.md").write_text("a")
        (tmp_path / "b.md").write_text("b")
        L._prune_history(1)
        assert (tmp_path / "subdir").exists()


class TestPruneDirOldest:
    """Loop 55: canonical prune helper."""

    def test_prune_dir_oldest_basic(self, tmp_path):
        from agent import loop as L
        for i in range(4):
            f = tmp_path / f"{i}.x"
            f.write_text(str(i))
            os.utime(f, (1000 + i, 1000 + i))
        deleted = L._prune_dir_oldest(tmp_path, 2)
        assert deleted == 2
        survivors = sorted(p.name for p in tmp_path.iterdir())
        assert survivors == ["2.x", "3.x"]

    def test_prune_dir_oldest_missing(self, tmp_path):
        from agent import loop as L
        assert L._prune_dir_oldest(tmp_path / "nope", 5) == 0

    def test_prune_dir_oldest_skips_subdirs(self, tmp_path):
        from agent import loop as L
        (tmp_path / "sub").mkdir()
        (tmp_path / "a").write_text("a")
        L._prune_dir_oldest(tmp_path, 0)
        assert (tmp_path / "sub").exists()


class TestRevertChanges:
    """Loop 56: _revert_changes returns success and logs failures."""

    def test_returns_true_on_success(self, monkeypatch):
        from agent import loop as L

        def fake_run(*a, **kw):
            import subprocess
            return subprocess.CompletedProcess(
                args=["git", *a], returncode=0, stdout="", stderr="",
            )
        monkeypatch.setattr(L, "_run_git", fake_run)
        assert L._revert_changes() is True

    def test_returns_false_then_recovers_via_reset(self, monkeypatch):
        from agent import loop as L
        import subprocess
        calls = []

        def fake_run(*a, **kw):
            calls.append(a)
            # First two calls (checkout, clean) fail; reset succeeds
            if a[0] in ("checkout", "clean"):
                return subprocess.CompletedProcess(
                    args=["git", *a], returncode=1, stdout="", stderr="busy",
                )
            return subprocess.CompletedProcess(
                args=["git", *a], returncode=0, stdout="", stderr="",
            )
        monkeypatch.setattr(L, "_run_git", fake_run)
        assert L._revert_changes() is True
        # Verify reset was actually attempted
        assert any(c[0] == "reset" for c in calls)

    def test_returns_false_when_reset_also_fails(self, monkeypatch):
        from agent import loop as L
        import subprocess

        def fake_run(*a, **kw):
            return subprocess.CompletedProcess(
                args=["git", *a], returncode=1, stdout="", stderr="locked",
            )
        monkeypatch.setattr(L, "_run_git", fake_run)
        assert L._revert_changes() is False

    def test_skips_reset_when_first_pass_succeeds(self, monkeypatch):
        from agent import loop as L
        import subprocess
        calls = []

        def fake_run(*a, **kw):
            calls.append(a[0])
            return subprocess.CompletedProcess(
                args=["git", *a], returncode=0, stdout="", stderr="",
            )
        monkeypatch.setattr(L, "_run_git", fake_run)
        L._revert_changes()
        assert calls == ["checkout", "clean"]
        assert "reset" not in calls
