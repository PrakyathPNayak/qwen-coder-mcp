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
    # Loop 83: _iteration now also calls time.monotonic() once at the
    # top to capture iter_monotonic. Provide that initial tick in addition
    # to the deadline-base tick.
    ticks = itertools.chain([1000.0, 1000.0], itertools.repeat(9999.0))
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

    def test_recovers_via_origin_main_when_head_broken(self, monkeypatch):
        """Loop 73: HEAD-broken final fallback uses origin/main."""
        from agent import loop as L
        import subprocess
        calls = []

        def fake_run(*a, **kw):
            calls.append(a)
            # checkout, clean, reset HEAD all fail; reset origin/main works.
            if a[0] == "reset" and len(a) >= 3 and a[2] == "origin/main":
                return subprocess.CompletedProcess(
                    args=["git", *a], returncode=0, stdout="", stderr="",
                )
            return subprocess.CompletedProcess(
                args=["git", *a], returncode=1, stdout="", stderr="broken",
            )
        monkeypatch.setattr(L, "_run_git", fake_run)
        log_lines = []
        monkeypatch.setattr(L, "_log", lambda m: log_lines.append(m))
        assert L._revert_changes() is True
        # Verify origin/main reset was attempted as the FINAL fallback.
        origin_resets = [c for c in calls if c[0] == "reset" and "origin/main" in c]
        assert len(origin_resets) == 1
        assert any("recovered via reset --hard origin/main" in l for l in log_lines)

    def test_returns_false_when_origin_main_fallback_also_fails(self, monkeypatch):
        """Loop 73: both HEAD and origin/main failures still return False.
        Loop 78: forces linear cadence so all 4 failures log."""
        from agent import loop as L
        import subprocess

        L._REVERT_SWALLOW_LOG.reset()
        monkeypatch.setattr(L._REVERT_SWALLOW_LOG, "schedule", "linear")
        monkeypatch.setattr(L._REVERT_SWALLOW_LOG, "every", 1)

        def fake_run(*a, **kw):
            return subprocess.CompletedProcess(
                args=["git", *a], returncode=128, stdout="",
                stderr="fatal: bad object",
            )
        monkeypatch.setattr(L, "_run_git", fake_run)
        log_lines = []
        monkeypatch.setattr(L, "_log", lambda m: log_lines.append(m))
        assert L._revert_changes() is False
        # Both fallback failure logs must appear (loop 78: now via _REVERT_SWALLOW_LOG).
        assert any("reset --hard HEAD" in l for l in log_lines)
        assert any("reset --hard origin/main" in l for l in log_lines)
        L._REVERT_SWALLOW_LOG.reset()

    def test_origin_main_fallback_skipped_when_head_reset_succeeds(self, monkeypatch):
        """Loop 73: don't try origin/main if HEAD reset already worked."""
        from agent import loop as L
        import subprocess
        calls = []

        def fake_run(*a, **kw):
            calls.append(a)
            if a[0] in ("checkout", "clean"):
                return subprocess.CompletedProcess(
                    args=["git", *a], returncode=1, stdout="", stderr="busy",
                )
            return subprocess.CompletedProcess(
                args=["git", *a], returncode=0, stdout="", stderr="",
            )
        monkeypatch.setattr(L, "_run_git", fake_run)
        assert L._revert_changes() is True
        # HEAD reset succeeded, so origin/main reset must NOT be attempted.
        origin_calls = [c for c in calls if "origin/main" in c]
        assert origin_calls == []


class TestRevertFailedPropagation:
    """Loop 57: failed revert surfaces as a distinct outcome category."""

    def test_outcome_strings_use_revert_failed_prefix(self):
        # The strings appear as literal in the source; ensure the contract
        # is documented and won't drift silently.
        from agent import loop as L
        src = (Path(L.__file__)).read_text(encoding="utf-8")
        assert "revert_failed:" in src
        assert "after_out_of_scope" in src
        assert "after_validation" in src
        assert "after_commit_push" in src


class TestApplyFailedOutcomeCategoryTag:
    """Loop 59: apply_failed outcomes embed the structured category."""

    def test_outcome_format_includes_category(self):
        from agent import loop as L
        src = Path(L.__file__).read_text(encoding="utf-8")
        assert 'f"apply_failed:{category}:{rel}:{msg[:60]}"' in src

    def test_category_extractor_used_in_iteration(self):
        from agent import loop as L
        src = Path(L.__file__).read_text(encoding="utf-8")
        # The iteration must call the canonical extractor (not duplicate logic).
        assert "_apply_error_category(msg)" in src

    def test_each_known_category_round_trips(self):
        from agent import loop as L
        # Every known category should map cleanly through _apply_error_category
        # for a representative message; this guards against a category being
        # added to the frozenset without a matching extractor branch.
        sample = {
            "not_a_unified_diff": "not_a_unified_diff",
            "oversized_diff": "oversized_diff: 999999 > 5",
            "unsafe_path": "unsafe_path: ../etc/passwd",
            "binary_patch": "binary_patch: foo.png",
            "unsafe_mode": "unsafe_mode: 100755",
            "malformed_diff": "malformed_diff: missing hunk header",
            "dir_conflict": "dir_conflict: foo is a directory",
            "apply_check_failed": "apply_check_failed: patch does not apply",
            "apply_failed": "apply_failed: rejected hunk #1",
        }
        for expected, msg in sample.items():
            got = L._apply_error_category(msg)
            assert got in L.APPLY_ERROR_CATEGORIES, f"{got!r} not in frozenset"


class TestOuterOutcomeCategories:
    """Loop 60: stable category set for the outer iteration outcome string."""

    def test_helper_extracts_leading_token(self):
        from agent import loop as L
        assert L._outer_outcome_category("applied:foo/bar.py") == "applied"
        assert L._outer_outcome_category("clean:foo.py") == "clean"
        assert L._outer_outcome_category("no_candidate_files") == "no_candidate_files"
        assert L._outer_outcome_category("revert_failed:x:after_validation") == "revert_failed"

    def test_helper_handles_empty_string(self):
        from agent import loop as L
        assert L._outer_outcome_category("") == ""

    def test_frozenset_is_immutable(self):
        from agent import loop as L
        with pytest.raises(AttributeError):
            L.OUTER_OUTCOME_CATEGORIES.add("nope")  # type: ignore[attr-defined]

    def test_frozenset_includes_all_known_outcomes(self):
        from agent import loop as L
        expected = {
            "applied", "clean", "skip", "rejected",
            "out_of_scope", "validation_failed",
            "commit_failed", "commit_skipped_empty",
            "revert_failed", "apply_failed",
            "qwen_error_find_bugs", "qwen_error_propose_fix",
            "qwen_error_devils_advocate",
            "budget_exceeded",
            "no_candidate_files", "no_hunks",
        }
        assert expected.issubset(L.OUTER_OUTCOME_CATEGORIES)

    def test_every_finish_call_in_source_uses_known_category(self):
        """AST-level audit: every `_finish(...)` call's first arg must
        start with a known category. Guards against drift when new
        outcomes are added without updating the frozenset.

        Implemented via AST so that calls split across lines, with
        comments, or wrapped in parentheses are still seen.
        """
        from agent import loop as L
        import ast
        src = Path(L.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        tokens: set[str] = set()
        unrecognised_shapes: list[str] = []

        def _leading_token_from_constant(value: str) -> str | None:
            return value.split(":", 1)[0] if value else None

        def _leading_token_from_joinedstr(node: ast.JoinedStr) -> str | None:
            # First value MUST be a literal Constant for the contract to
            # hold; record otherwise so the test fails loudly.
            if not node.values:
                return None
            first = node.values[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                return _leading_token_from_constant(first.value)
            return None

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Name) and func.id == "_finish"):
                continue
            if not node.args:
                unrecognised_shapes.append(f"_finish() with no args at line {node.lineno}")
                continue
            arg = node.args[0]
            tok: str | None
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                tok = _leading_token_from_constant(arg.value)
            elif isinstance(arg, ast.JoinedStr):
                tok = _leading_token_from_joinedstr(arg)
            else:
                unrecognised_shapes.append(
                    f"_finish(<{type(arg).__name__}>) at line {node.lineno}"
                )
                continue
            if tok is None:
                unrecognised_shapes.append(f"_finish empty-string at line {node.lineno}")
                continue
            tokens.add(tok)

        assert not unrecognised_shapes, (
            f"_finish call shapes the audit doesn't recognise: {unrecognised_shapes}"
        )
        unknown = tokens - L.OUTER_OUTCOME_CATEGORIES
        assert not unknown, (
            f"_iteration emits outcome categories not in "
            f"OUTER_OUTCOME_CATEGORIES: {sorted(unknown)}"
        )

    def test_no_extras_beyond_emitted(self):
        """Inverse audit: every category in the frozenset is actually
        emitted somewhere in the source. Prevents stale tokens."""
        from agent import loop as L
        src = Path(L.__file__).read_text(encoding="utf-8")
        for cat in L.OUTER_OUTCOME_CATEGORIES:
            assert cat in src, f"{cat!r} declared but never emitted"


class TestTimingLogCategoryField:
    """Loop 61: timing.log records include the structured category field."""

    def test_record_includes_category_for_known_outcome(self, tmp_path, monkeypatch):
        from agent import loop as L
        timing = tmp_path / "timing.log"
        monkeypatch.setattr(L, "TIMING_FILE", timing)
        L._write_timing(Path("foo/bar.py"), "applied:foo/bar.py", {"apply_diff": 1.5})
        line = timing.read_text(encoding="utf-8").strip()
        import json as _json
        rec = _json.loads(line)
        assert rec["category"] == "applied"
        assert rec["outcome"] == "applied:foo/bar.py"
        assert rec["phases"] == {"apply_diff": 1.5}

    def test_category_for_no_colon_outcome(self, tmp_path, monkeypatch):
        from agent import loop as L
        timing = tmp_path / "timing.log"
        monkeypatch.setattr(L, "TIMING_FILE", timing)
        L._write_timing(Path("x.py"), "no_candidate_files", {})
        import json as _json
        rec = _json.loads(timing.read_text(encoding="utf-8").strip())
        assert rec["category"] == "no_candidate_files"

    def test_category_for_unknown_outcome_passes_through(self, tmp_path, monkeypatch):
        # _outer_outcome_category returns the leading token regardless;
        # this guards against silent normalization that would obscure drift.
        from agent import loop as L
        timing = tmp_path / "timing.log"
        monkeypatch.setattr(L, "TIMING_FILE", timing)
        L._write_timing(Path("x.py"), "weird_unknown:detail", {})
        import json as _json
        rec = _json.loads(timing.read_text(encoding="utf-8").strip())
        assert rec["category"] == "weird_unknown"


class TestCommitAndPushEmptyTreeLog:
    """Loop 62: empty staged tree path emits a log line for forensics."""

    def test_empty_staged_tree_logs_message(self, tmp_path, monkeypatch):
        from agent import loop as L

        # Stub _run_git: add succeeds, status returns empty.
        calls = []
        def fake_run_git(*args, check=True):
            calls.append(args)
            import subprocess
            if args[0] == "add":
                return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")
            if args[0] == "status":
                return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")
            return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")
        monkeypatch.setattr(L, "_run_git", fake_run_git)

        log_lines = []
        monkeypatch.setattr(L, "_log", lambda msg: log_lines.append(msg))

        result = L._commit_and_push("test: msg", push=False)
        assert result == "empty"
        assert any("empty staged tree" in line for line in log_lines), log_lines
        # Must not have attempted commit/pull/push
        assert not any(c[0] in ("commit", "pull", "push") for c in calls)


class TestCommitAndPushTriState:
    """Loop 63: _commit_and_push returns 'ok' | 'empty' | 'failed'."""

    def test_add_failure_returns_failed(self, monkeypatch):
        from agent import loop as L
        import subprocess
        def fake(*args, check=True):
            if args[0] == "add":
                return subprocess.CompletedProcess(args=list(args), returncode=1, stdout="", stderr="boom")
            return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")
        monkeypatch.setattr(L, "_run_git", fake)
        monkeypatch.setattr(L, "_log", lambda m: None)
        assert L._commit_and_push("m", push=False) == "failed"

    def test_commit_failure_returns_failed(self, monkeypatch):
        from agent import loop as L
        import subprocess
        def fake(*args, check=True):
            if args[0] == "status":
                return subprocess.CompletedProcess(args=list(args), returncode=0, stdout=" M file\n", stderr="")
            if args[0] == "commit":
                return subprocess.CompletedProcess(args=list(args), returncode=1, stdout="", stderr="commit boom")
            return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")
        monkeypatch.setattr(L, "_run_git", fake)
        monkeypatch.setattr(L, "_log", lambda m: None)
        assert L._commit_and_push("m", push=False) == "failed"

    def test_success_no_push_returns_ok(self, monkeypatch):
        from agent import loop as L
        import subprocess
        def fake(*args, check=True):
            if args[0] == "status":
                return subprocess.CompletedProcess(args=list(args), returncode=0, stdout=" M file\n", stderr="")
            return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")
        monkeypatch.setattr(L, "_run_git", fake)
        monkeypatch.setattr(L, "_log", lambda m: None)
        assert L._commit_and_push("m", push=False) == "ok"

    def test_iteration_emits_commit_skipped_empty_when_tree_empty(self):
        from agent import loop as L
        src = Path(L.__file__).read_text(encoding="utf-8")
        # Source-level audit: the new outcome string is reachable in _iteration.
        assert 'f"commit_skipped_empty:{rel}"' in src
        # And the tri-state branch exists.
        assert 'commit_status == "empty"' in src
        assert 'commit_status == "ok"' in src


class TestRuntimeLogCategoryPrefix:
    """Loop 64: runtime.log iteration line includes category bracket."""

    def test_main_loop_log_format(self):
        from agent import loop as L
        src = Path(L.__file__).read_text(encoding="utf-8")
        assert 'f"iteration [{_outer_outcome_category(outcome)}] -> {outcome}"' in src

    def test_log_line_format_for_known_categories(self, monkeypatch):
        # Direct format check: simulate the f-string with known outcomes.
        from agent import loop as L
        cases = {
            "applied:foo.py": "iteration [applied] -> applied:foo.py",
            "no_candidate_files": "iteration [no_candidate_files] -> no_candidate_files",
            "revert_failed:x:after_validation": "iteration [revert_failed] -> revert_failed:x:after_validation",
            "commit_skipped_empty:y": "iteration [commit_skipped_empty] -> commit_skipped_empty:y",
        }
        for outcome, expected in cases.items():
            line = f"iteration [{L._outer_outcome_category(outcome)}] -> {outcome}"
            assert line == expected


class TestLogNeverRaises:
    """Loop 67: `_log` is observability and must never raise — broad except."""

    def test_log_swallows_oserror_on_open(self, tmp_path, monkeypatch):
        from agent import loop as L
        bad = tmp_path / "no" / "such" / "dir" / "a.log"
        # Simulate a chmod 000 dir would be flaky in CI; instead patch
        # the rotation helper to OK and break the open call.
        monkeypatch.setattr(L, "LOG_FILE", bad)
        # mkdir succeeds in tmp_path but we'll patch open to raise.
        orig_open = Path.open
        def boom(self, *a, **kw):
            raise OSError("disk full")
        monkeypatch.setattr(Path, "open", boom)
        try:
            L._log("test")  # must not raise
        finally:
            monkeypatch.setattr(Path, "open", orig_open)

    def test_log_swallows_unicode_error_on_print(self, tmp_path, monkeypatch):
        from agent import loop as L
        monkeypatch.setattr(L, "LOG_FILE", tmp_path / "a.log")
        # Patch print to raise; verify the file write still happens.
        import builtins
        orig_print = builtins.print
        def boom(*a, **kw):
            raise UnicodeEncodeError("ascii", "x", 0, 1, "")
        monkeypatch.setattr(builtins, "print", boom)
        try:
            L._log("hello")
        finally:
            monkeypatch.setattr(builtins, "print", orig_print)
        # File write should still have succeeded.
        text = (tmp_path / "a.log").read_text(encoding="utf-8")
        assert "hello" in text

    def test_log_swallows_arbitrary_exception_from_write(self, tmp_path, monkeypatch):
        """Even non-OSError exceptions from the write path are swallowed."""
        from agent import loop as L
        monkeypatch.setattr(L, "LOG_FILE", tmp_path / "a.log")

        class WeirdHandle:
            def write(self, *a, **kw):
                raise RuntimeError("not OSError, not allowed to crash _log")
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        orig_open = Path.open
        def fake_open(self, *a, **kw):
            return WeirdHandle()
        monkeypatch.setattr(Path, "open", fake_open)
        try:
            L._log("test")  # must not raise
        finally:
            monkeypatch.setattr(Path, "open", orig_open)


class TestObservabilityNeverRaises:
    """Loop 68: `_append_state` and `_write_history` swallow all exceptions
    so disk pressure never kills an iteration."""

    def test_append_state_swallows_oserror(self, tmp_path, monkeypatch):
        from agent import loop as L
        monkeypatch.setattr(L, "STATE_FILE", tmp_path / "STATE.md")
        # Patch _rotate_state_if_needed to raise — should still not crash.
        monkeypatch.setattr(L, "_rotate_state_if_needed", lambda: (_ for _ in ()).throw(OSError("disk full")))
        log_lines = []
        monkeypatch.setattr(L, "_log", lambda m: log_lines.append(m))
        L._append_state("entry\n")
        assert any("_append_state failed" in l for l in log_lines)

    def test_append_state_swallows_runtime_error(self, tmp_path, monkeypatch):
        from agent import loop as L
        monkeypatch.setattr(L, "STATE_FILE", tmp_path / "STATE.md")
        monkeypatch.setattr(L, "_rotate_state_if_needed", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        monkeypatch.setattr(L, "_log", lambda m: None)
        L._append_state("entry\n")  # must not raise

    def test_write_history_swallows_oserror_returns_none(self, tmp_path, monkeypatch):
        from agent import loop as L
        # Point HISTORY_DIR at a path where mkdir will fail (we'll make
        # the parent a regular file).
        bad_root = tmp_path / "blocker"
        bad_root.write_text("not a dir")
        monkeypatch.setattr(L, "HISTORY_DIR", bad_root / "history")
        log_lines = []
        monkeypatch.setattr(L, "_log", lambda m: log_lines.append(m))
        result = L._write_history("foo.md", "body")
        assert result is None
        assert any("_write_history failed" in l for l in log_lines)

    def test_write_history_success_returns_path(self, tmp_path, monkeypatch):
        from agent import loop as L
        monkeypatch.setattr(L, "HISTORY_DIR", tmp_path / "history")
        result = L._write_history("foo.md", "body")
        assert result is not None
        assert result.read_text(encoding="utf-8") == "body"

    def test_write_history_swallows_arbitrary_exception(self, tmp_path, monkeypatch):
        from agent import loop as L
        monkeypatch.setattr(L, "HISTORY_DIR", tmp_path / "history")
        # Patch _prune_history to raise after the write — write succeeds
        # but prune blows up; the function still returns successfully
        # because the exception is swallowed.
        monkeypatch.setattr(L, "_prune_history", lambda n: (_ for _ in ()).throw(RuntimeError("prune boom")))
        monkeypatch.setattr(L, "_log", lambda m: None)
        result = L._write_history("foo.md", "body")
        # Note: prune raises BEFORE return path runs — so result is None.
        # The contract is "swallow exceptions, log, return None".
        assert result is None


class TestWriteTimingFailureCounter:
    """Loop 69 (refactored loop 70 to share `_RateLimitedSwallowLogger`):
    `_write_timing` rate-limits its swallow-log so a persistent disk
    fault doesn't spam runtime.log."""

    def test_first_failure_is_logged_with_count_1(self, tmp_path, monkeypatch):
        from agent import loop as L
        from pathlib import Path
        L._TIMING_SWALLOW_LOG.reset()
        monkeypatch.setattr(L, "TIMING_FILE", tmp_path / "x" / "timing.log")
        # Force failure: rotate raises.
        monkeypatch.setattr(L, "_rotate_timing_if_oversized", lambda: (_ for _ in ()).throw(OSError("disk full")))
        log_lines = []
        monkeypatch.setattr(L, "_log", lambda m: log_lines.append(m))
        L._write_timing(Path("a.py"), "applied:a.py", {})
        assert any("_write_timing failed" in l and "count=1" in l for l in log_lines)
        assert L._TIMING_SWALLOW_LOG.count == 1
        L._TIMING_SWALLOW_LOG.reset()

    def test_repeated_failures_are_rate_limited(self, tmp_path, monkeypatch):
        from agent import loop as L
        from pathlib import Path
        L._TIMING_SWALLOW_LOG.reset()
        # Module logger uses exponential schedule. With every=100 default,
        # 50 failures fire at counts: 1, 2, 4, 8, 16, 32 = 6 logs.
        monkeypatch.setattr(L._TIMING_SWALLOW_LOG, "every", 100)
        monkeypatch.setattr(L._TIMING_SWALLOW_LOG, "schedule", "exponential")
        monkeypatch.setattr(L, "TIMING_FILE", tmp_path / "x" / "timing.log")
        monkeypatch.setattr(L, "_rotate_timing_if_oversized", lambda: (_ for _ in ()).throw(OSError("disk full")))
        log_lines = []
        monkeypatch.setattr(L, "_log", lambda m: log_lines.append(m))
        for _ in range(50):
            L._write_timing(Path("a.py"), "applied:a.py", {})
        assert len(log_lines) == 6  # 1, 2, 4, 8, 16, 32
        assert L._TIMING_SWALLOW_LOG.count == 50
        L._TIMING_SWALLOW_LOG.reset()

    def test_logs_every_nth_failure(self, tmp_path, monkeypatch):
        from agent import loop as L
        from pathlib import Path
        L._TIMING_SWALLOW_LOG.reset()
        # Force linear schedule for predictable cadence verification.
        monkeypatch.setattr(L._TIMING_SWALLOW_LOG, "every", 5)
        monkeypatch.setattr(L._TIMING_SWALLOW_LOG, "schedule", "linear")
        monkeypatch.setattr(L, "TIMING_FILE", tmp_path / "x" / "timing.log")
        monkeypatch.setattr(L, "_rotate_timing_if_oversized", lambda: (_ for _ in ()).throw(OSError("disk full")))
        log_lines = []
        monkeypatch.setattr(L, "_log", lambda m: log_lines.append(m))
        for _ in range(15):
            L._write_timing(Path("a.py"), "applied:a.py", {})
        # Logs at: count=1, 5, 10, 15 → 4 total.
        assert len(log_lines) == 4
        assert "count=1" in log_lines[0]
        assert "count=5" in log_lines[1]
        assert "count=10" in log_lines[2]
        assert "count=15" in log_lines[3]
        L._TIMING_SWALLOW_LOG.reset()

    def test_success_does_not_increment_counter(self, tmp_path, monkeypatch):
        from agent import loop as L
        from pathlib import Path
        L._TIMING_SWALLOW_LOG.reset()
        monkeypatch.setattr(L, "TIMING_FILE", tmp_path / "timing.log")
        L._write_timing(Path("a.py"), "applied:a.py", {"phase1": 0.5})
        assert L._TIMING_SWALLOW_LOG.count == 0
        assert (tmp_path / "timing.log").exists()


class TestRateLimitedSwallowLogger:
    """Loop 70: shared rate-limited swallow logger helper."""

    def test_first_failure_logs_count_1(self, monkeypatch):
        from agent import loop as L
        logger = L._RateLimitedSwallowLogger("xyz", every=100)
        log_lines = []
        monkeypatch.setattr(L, "_log", lambda m: log_lines.append(m))
        logger.report(OSError("e1"))
        assert log_lines == ["xyz failed (count=1): e1"]

    def test_rate_limit_with_every(self, monkeypatch):
        from agent import loop as L
        logger = L._RateLimitedSwallowLogger("xyz", every=3)
        log_lines = []
        monkeypatch.setattr(L, "_log", lambda m: log_lines.append(m))
        for i in range(7):
            logger.report(OSError(f"e{i}"))
        # Logs at 1, 3, 6 → 3 total.
        assert len(log_lines) == 3
        assert logger.count == 7

    def test_reset_resets_counter(self, monkeypatch):
        from agent import loop as L
        logger = L._RateLimitedSwallowLogger("xyz")
        monkeypatch.setattr(L, "_log", lambda m: None)
        logger.report(OSError("e1"))
        assert logger.count == 1
        logger.reset()
        assert logger.count == 0

    def test_state_logger_used_by_append_state(self, tmp_path, monkeypatch):
        from agent import loop as L
        L._STATE_SWALLOW_LOG.reset()
        monkeypatch.setattr(L._STATE_SWALLOW_LOG, "schedule", "linear")
        monkeypatch.setattr(L._STATE_SWALLOW_LOG, "every", 100)
        monkeypatch.setattr(L, "_rotate_state_if_needed", lambda: (_ for _ in ()).throw(OSError("disk full")))
        log_lines = []
        monkeypatch.setattr(L, "_log", lambda m: log_lines.append(m))
        for _ in range(3):
            L._append_state("entry\n")
        assert L._STATE_SWALLOW_LOG.count == 3
        # Linear with every=100: only count=1 fires.
        assert len(log_lines) == 1
        assert "_append_state failed" in log_lines[0]
        L._STATE_SWALLOW_LOG.reset()

    def test_history_logger_used_by_write_history(self, tmp_path, monkeypatch):
        from agent import loop as L
        L._HISTORY_SWALLOW_LOG.reset()
        monkeypatch.setattr(L._HISTORY_SWALLOW_LOG, "schedule", "linear")
        monkeypatch.setattr(L._HISTORY_SWALLOW_LOG, "every", 100)
        bad_root = tmp_path / "blocker"
        bad_root.write_text("not a dir")
        monkeypatch.setattr(L, "HISTORY_DIR", bad_root / "history")
        log_lines = []
        monkeypatch.setattr(L, "_log", lambda m: log_lines.append(m))
        for _ in range(3):
            assert L._write_history("foo.md", "body") is None
        assert L._HISTORY_SWALLOW_LOG.count == 3
        assert len(log_lines) == 1
        assert "_write_history failed" in log_lines[0]
        L._HISTORY_SWALLOW_LOG.reset()

    def test_exponential_schedule_logs_powers_of_two(self, monkeypatch):
        from agent import loop as L
        logger = L._RateLimitedSwallowLogger("xyz", every=100, schedule="exponential")
        log_lines = []
        monkeypatch.setattr(L, "_log", lambda m: log_lines.append(m))
        for _ in range(64):
            logger.report(OSError("e"))
        # Powers of two ≤ 100: 1, 2, 4, 8, 16, 32, 64 = 7 logs.
        assert len(log_lines) == 7
        for expected in (1, 2, 4, 8, 16, 32, 64):
            assert any(f"count={expected})" in l for l in log_lines)

    def test_exponential_falls_back_to_linear_past_every(self, monkeypatch):
        from agent import loop as L
        logger = L._RateLimitedSwallowLogger("xyz", every=8, schedule="exponential")
        log_lines = []
        monkeypatch.setattr(L, "_log", lambda m: log_lines.append(m))
        for _ in range(20):
            logger.report(OSError("e"))
        # Powers of two ≤ 8: 1, 2, 4, 8. Then linear every 8: 16.
        # = 5 logs at counts 1, 2, 4, 8, 16.
        assert len(log_lines) == 5
        for expected in (1, 2, 4, 8, 16):
            assert any(f"count={expected})" in l for l in log_lines)


class TestIterationTimestampCached:
    """Loop 72: `_iteration` caches one `_now()` value as `iter_ts` and
    uses it for every state.md / history.md narrative line so all
    records emitted by the same iteration share an identical timestamp.
    """

    def test_rejected_branch_state_and_history_share_ts(self, env, monkeypatch):
        L, repo = env
        # Spy on _now so we can verify it's called exactly once during
        # the iteration (the timing-log path's own _now() is unrelated
        # because that's inside _write_timing — we patch _write_timing
        # to a no-op to isolate the iteration body's calls).
        ts_seq = ["2099-01-01T00:00:00", "WRONG_TS_2", "WRONG_TS_3"]
        idx = {"i": 0}
        def fake_now():
            i = idx["i"]
            idx["i"] += 1
            return ts_seq[i] if i < len(ts_seq) else "EXHAUSTED"
        monkeypatch.setattr(L, "_now", fake_now)
        # Suppress _write_timing to avoid its own _now() call confusing
        # the count.
        monkeypatch.setattr(L, "_write_timing", lambda *a, **kw: None)
        # Suppress _log to avoid runtime.log _now() calls.
        monkeypatch.setattr(L, "_log", lambda m: None)

        client = _ScriptedClient([
            "- bug\n",
            _diff_for("f.py", "x = 1", "x = 2"),
            "VERDICT: REJECT — too speculative\n",
        ])
        out = L._iteration(client, max_bytes=10_000, push=False)
        assert out.startswith("rejected:")
        state_text = (repo / "STATE.md").read_text("utf-8")
        # The cached timestamp from the FIRST _now() call must appear;
        # WRONG_TS_2 and WRONG_TS_3 must NOT appear in state.md.
        assert "2099-01-01T00:00:00" in state_text
        assert "WRONG_TS_2" not in state_text
        assert "WRONG_TS_3" not in state_text

    def test_apply_failed_branch_state_and_history_share_ts(self, env, monkeypatch):
        L, repo = env
        ts_seq = ["2099-02-02T00:00:00"] + [f"WRONG_TS_{i}" for i in range(2, 20)]
        idx = {"i": 0}
        def fake_now():
            i = idx["i"]
            idx["i"] += 1
            return ts_seq[i] if i < len(ts_seq) else "EXHAUSTED"
        monkeypatch.setattr(L, "_now", fake_now)
        monkeypatch.setattr(L, "_write_timing", lambda *a, **kw: None)
        monkeypatch.setattr(L, "_log", lambda m: None)

        client = _ScriptedClient([
            "- bug\n",
            "this is not a diff at all",
            "VERDICT: ACCEPT\n",
        ])
        out = L._iteration(client, max_bytes=10_000, push=False)
        assert out.startswith("apply_failed:")
        state_text = (repo / "STATE.md").read_text("utf-8")
        assert "2099-02-02T00:00:00" in state_text
        for i in range(2, 20):
            assert f"WRONG_TS_{i}" not in state_text


class TestRateLimitedSwallowLoggerSummary:
    """Loop 74: `summary()` exposes suppression state for diagnostics."""

    def test_summary_zero_at_construction(self):
        from agent import loop as L
        logger = L._RateLimitedSwallowLogger("xyz")
        s = logger.summary()
        assert s["label"] == "xyz"
        assert s["count"] == 0
        assert s["last_logged_count"] == 0
        assert s["suppressed"] == 0

    def test_summary_after_one_logged_failure(self, monkeypatch):
        from agent import loop as L
        logger = L._RateLimitedSwallowLogger("xyz", every=100, schedule="linear")
        monkeypatch.setattr(L, "_log", lambda m: None)
        logger.report(OSError("e"))
        s = logger.summary()
        assert s["count"] == 1
        assert s["last_logged_count"] == 1
        assert s["suppressed"] == 0

    def test_summary_after_suppressed_failures(self, monkeypatch):
        from agent import loop as L
        logger = L._RateLimitedSwallowLogger("xyz", every=100, schedule="linear")
        monkeypatch.setattr(L, "_log", lambda m: None)
        for _ in range(50):
            logger.report(OSError("e"))
        s = logger.summary()
        # Linear every=100: only count=1 was logged. 49 suppressed.
        assert s["count"] == 50
        assert s["last_logged_count"] == 1
        assert s["suppressed"] == 49

    def test_summary_advances_at_every_n_log(self, monkeypatch):
        from agent import loop as L
        logger = L._RateLimitedSwallowLogger("xyz", every=5, schedule="linear")
        monkeypatch.setattr(L, "_log", lambda m: None)
        for _ in range(12):
            logger.report(OSError("e"))
        # Logs at 1, 5, 10. last_logged_count = 10. count=12. suppressed=2.
        s = logger.summary()
        assert s["count"] == 12
        assert s["last_logged_count"] == 10
        assert s["suppressed"] == 2

    def test_reset_clears_last_logged_count(self, monkeypatch):
        from agent import loop as L
        logger = L._RateLimitedSwallowLogger("xyz")
        monkeypatch.setattr(L, "_log", lambda m: None)
        logger.report(OSError("e"))
        assert logger.last_logged_count == 1
        logger.reset()
        assert logger.summary()["last_logged_count"] == 0
        assert logger.summary()["count"] == 0
        assert logger.summary()["suppressed"] == 0

    def test_summary_exponential_schedule(self, monkeypatch):
        from agent import loop as L
        logger = L._RateLimitedSwallowLogger("xyz", every=8, schedule="exponential")
        monkeypatch.setattr(L, "_log", lambda m: None)
        for _ in range(10):
            logger.report(OSError("e"))
        # Expo with every=8: logs at 1, 2, 4, 8. count=10, last=8, suppressed=2.
        s = logger.summary()
        assert s["count"] == 10
        assert s["last_logged_count"] == 8
        assert s["suppressed"] == 2
        assert s["schedule"] == "exponential"


class TestSwallowSummaries:
    """Loop 75: `_log_swallow_summaries()` surfaces ongoing suppression at
    iteration boundaries without re-logging stale snapshots."""

    def test_no_summary_when_no_failures(self, monkeypatch):
        from agent import loop as L
        # Reset global state
        for lg in L._swallow_loggers():
            lg.reset()
        L._LAST_SWALLOW_SUMMARY_COUNTS.clear()
        log_lines = []
        monkeypatch.setattr(L, "_log", lambda m: log_lines.append(m))
        L._log_swallow_summaries()
        assert log_lines == []

    def test_summary_emitted_when_suppressed_grows(self, monkeypatch):
        from agent import loop as L
        for lg in L._swallow_loggers():
            lg.reset()
        L._LAST_SWALLOW_SUMMARY_COUNTS.clear()
        log_lines = []
        monkeypatch.setattr(L, "_log", lambda m: log_lines.append(m))
        # Use linear schedule so we know exactly when logger logs vs suppresses.
        monkeypatch.setattr(L._TIMING_SWALLOW_LOG, "schedule", "linear")
        monkeypatch.setattr(L._TIMING_SWALLOW_LOG, "every", 1000)
        # Drive 5 failures: count=1 logs, counts 2-5 suppress.
        for _ in range(5):
            L._TIMING_SWALLOW_LOG.report(OSError("e"))
        assert L._TIMING_SWALLOW_LOG.summary()["suppressed"] == 4
        log_lines.clear()
        L._log_swallow_summaries()
        # First summary call: count=5 grew from last=0 → emit.
        assert any("swallow-summary _write_timing" in l for l in log_lines)
        assert any("count=5" in l and "suppressed=4" in l for l in log_lines)

    def test_no_summary_when_count_unchanged(self, monkeypatch):
        from agent import loop as L
        for lg in L._swallow_loggers():
            lg.reset()
        L._LAST_SWALLOW_SUMMARY_COUNTS.clear()
        monkeypatch.setattr(L._TIMING_SWALLOW_LOG, "schedule", "linear")
        monkeypatch.setattr(L._TIMING_SWALLOW_LOG, "every", 1000)
        monkeypatch.setattr(L, "_log", lambda m: None)  # silent during driving
        for _ in range(5):
            L._TIMING_SWALLOW_LOG.report(OSError("e"))
        log_lines = []
        monkeypatch.setattr(L, "_log", lambda m: log_lines.append(m))
        L._log_swallow_summaries()
        assert any("count=5" in l for l in log_lines)
        # Second call without any new failures must NOT emit again.
        log_lines.clear()
        L._log_swallow_summaries()
        assert log_lines == []

    def test_summary_re_emits_after_more_failures(self, monkeypatch):
        from agent import loop as L
        for lg in L._swallow_loggers():
            lg.reset()
        L._LAST_SWALLOW_SUMMARY_COUNTS.clear()
        monkeypatch.setattr(L._STATE_SWALLOW_LOG, "schedule", "linear")
        monkeypatch.setattr(L._STATE_SWALLOW_LOG, "every", 1000)
        monkeypatch.setattr(L, "_log", lambda m: None)
        for _ in range(3):
            L._STATE_SWALLOW_LOG.report(OSError("e"))
        log_lines = []
        monkeypatch.setattr(L, "_log", lambda m: log_lines.append(m))
        L._log_swallow_summaries()
        assert any("count=3" in l for l in log_lines)
        log_lines.clear()
        # Two more failures.
        for _ in range(2):
            L._STATE_SWALLOW_LOG.report(OSError("e"))
        L._log_swallow_summaries()
        assert any("count=5" in l for l in log_lines)

    def test_summary_never_raises(self, monkeypatch):
        from agent import loop as L
        for lg in L._swallow_loggers():
            lg.reset()
        L._LAST_SWALLOW_SUMMARY_COUNTS.clear()
        # Make _swallow_loggers return a broken object whose .summary() raises.
        class _Bad:
            def summary(self):
                raise RuntimeError("boom")
        monkeypatch.setattr(L, "_swallow_loggers", lambda: (_Bad(),))
        L._log_swallow_summaries()  # must not raise

    def test_finish_calls_summary(self, env, monkeypatch):
        from agent import loop as L
        for lg in L._swallow_loggers():
            lg.reset()
        L._LAST_SWALLOW_SUMMARY_COUNTS.clear()
        # Drive a real suppression then run an iteration to its end.
        monkeypatch.setattr(L._TIMING_SWALLOW_LOG, "schedule", "linear")
        monkeypatch.setattr(L._TIMING_SWALLOW_LOG, "every", 1000)
        for _ in range(3):
            L._TIMING_SWALLOW_LOG.report(OSError("e"))
        log_lines = []
        monkeypatch.setattr(L, "_log", lambda m: log_lines.append(m))

        client = _ScriptedClient(["\n   \n"])  # empty issue, clean branch
        L._iteration(client, max_bytes=10_000, push=False)
        assert any("swallow-summary _write_timing" in l for l in log_lines)


class TestPruneAndCursorRateLimited:
    """Loop 76: `_prune_dir_oldest` and `_save_cursor` failures route
    through the rate-limited swallow loggers so persistent disk faults
    don't spam one log line per iteration."""

    def test_prune_failure_uses_rate_limited_logger(self, monkeypatch, tmp_path):
        from agent import loop as L
        L._PRUNE_SWALLOW_LOG.reset()
        log_lines = []
        monkeypatch.setattr(L, "_log", lambda m: log_lines.append(m))
        monkeypatch.setattr(L._PRUNE_SWALLOW_LOG, "schedule", "linear")
        monkeypatch.setattr(L._PRUNE_SWALLOW_LOG, "every", 5)

        # Bad directory (file, not dir) → iterdir raises.
        bad = tmp_path / "afile"
        bad.write_text("x")
        # Drive 7 calls.
        for _ in range(7):
            L._prune_dir_oldest(bad, 0)
        # Linear every=5: count=1 logs, count=5 logs, others suppressed → 2 lines.
        prune_lines = [l for l in log_lines if "_prune_dir_oldest failed" in l]
        assert len(prune_lines) == 2
        assert L._PRUNE_SWALLOW_LOG.count == 7
        # Context (the bad path) appears in the emitted line.
        assert any(str(bad) in l for l in prune_lines)

    def test_cursor_save_failure_uses_rate_limited_logger(self, monkeypatch, tmp_path):
        import os
        from agent import loop as L
        L._CURSOR_SWALLOW_LOG.reset()
        monkeypatch.setattr(L, "CURSOR_FILE", tmp_path / ".loop" / "cursor.json")
        log_lines = []
        monkeypatch.setattr(L, "_log", lambda m: log_lines.append(m))
        monkeypatch.setattr(L._CURSOR_SWALLOW_LOG, "schedule", "linear")
        monkeypatch.setattr(L._CURSOR_SWALLOW_LOG, "every", 4)

        real_replace = os.replace
        L.os.replace = lambda *a, **k: (_ for _ in ()).throw(OSError("nope"))
        try:
            for i in range(6):
                L._save_cursor(i)
        finally:
            L.os.replace = real_replace
        cur_lines = [l for l in log_lines if "_save_cursor failed" in l]
        # Linear every=4: count=1 logs, count=4 logs → 2 lines.
        assert len(cur_lines) == 2
        # Last logged carries idx context (idx=3 logged at count=4).
        assert any("idx=3" in l for l in cur_lines)

    def test_swallow_loggers_includes_prune_and_cursor(self):
        from agent import loop as L
        labels = {lg.label for lg in L._swallow_loggers()}
        assert "_prune_dir_oldest" in labels
        assert "_save_cursor" in labels

    def test_report_context_appears_in_log_line(self, monkeypatch):
        from agent import loop as L
        lg = L._RateLimitedSwallowLogger("test_ctx", every=1, schedule="linear")
        log_lines = []
        monkeypatch.setattr(L, "_log", lambda m: log_lines.append(m))
        lg.report(OSError("boom"), context="path=/tmp/x")
        assert log_lines == ["test_ctx failed (count=1) [path=/tmp/x]: boom"]

    def test_report_no_context_keeps_legacy_format(self, monkeypatch):
        from agent import loop as L
        lg = L._RateLimitedSwallowLogger("test_noctx", every=1, schedule="linear")
        log_lines = []
        monkeypatch.setattr(L, "_log", lambda m: log_lines.append(m))
        lg.report(OSError("boom"))
        assert log_lines == ["test_noctx failed (count=1): boom"]


class TestGitFailureRateLimited:
    """Loop 77: git add/commit/pull/push failures route through
    rate-limited swallow loggers so a persistent network or repo fault
    stops spamming one log line per iteration."""

    def _stub_run_git(self, monkeypatch, *, add_rc=0, commit_rc=0,
                     pull_rc=0, push_rc=0, status_out=" M f.py\n"):
        from agent import loop as L
        from types import SimpleNamespace
        def fake(*args, **kw):
            sub = args[0] if args else ""
            if sub == "add":
                return SimpleNamespace(returncode=add_rc, stdout="", stderr="add-err")
            if sub == "status":
                return SimpleNamespace(returncode=0, stdout=status_out, stderr="")
            if sub == "commit":
                return SimpleNamespace(returncode=commit_rc, stdout="", stderr="commit-err")
            if sub == "pull":
                return SimpleNamespace(returncode=pull_rc, stdout="", stderr="pull-err")
            if sub == "push":
                return SimpleNamespace(returncode=push_rc, stdout="", stderr="push-err")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        monkeypatch.setattr(L, "_run_git", fake)
        monkeypatch.setattr(L, "_abort_rebase_if_any", lambda: None)

    def test_git_push_failure_rate_limited(self, monkeypatch):
        from agent import loop as L
        L._GIT_REMOTE_SWALLOW_LOG.reset()
        log_lines = []
        monkeypatch.setattr(L, "_log", lambda m: log_lines.append(m))
        monkeypatch.setattr(L._GIT_REMOTE_SWALLOW_LOG, "schedule", "linear")
        monkeypatch.setattr(L._GIT_REMOTE_SWALLOW_LOG, "every", 5)
        self._stub_run_git(monkeypatch, push_rc=1)
        for _ in range(7):
            assert L._commit_and_push("msg", push=True) == "failed"
        push_lines = [l for l in log_lines if "git_remote failed" in l and "git push" in l]
        # Linear every=5: count=1 logs, count=5 logs.
        assert len(push_lines) == 2
        assert L._GIT_REMOTE_SWALLOW_LOG.count == 7

    def test_git_pull_failure_rate_limited(self, monkeypatch):
        from agent import loop as L
        L._GIT_REMOTE_SWALLOW_LOG.reset()
        log_lines = []
        monkeypatch.setattr(L, "_log", lambda m: log_lines.append(m))
        monkeypatch.setattr(L._GIT_REMOTE_SWALLOW_LOG, "schedule", "linear")
        monkeypatch.setattr(L._GIT_REMOTE_SWALLOW_LOG, "every", 100)
        self._stub_run_git(monkeypatch, pull_rc=1)
        L._commit_and_push("msg", push=True)
        pull_lines = [l for l in log_lines if "git pull --rebase" in l]
        assert len(pull_lines) == 1
        assert "pull-err" in pull_lines[0]

    def test_git_add_failure_rate_limited(self, monkeypatch):
        from agent import loop as L
        L._GIT_LOCAL_SWALLOW_LOG.reset()
        log_lines = []
        monkeypatch.setattr(L, "_log", lambda m: log_lines.append(m))
        monkeypatch.setattr(L._GIT_LOCAL_SWALLOW_LOG, "schedule", "linear")
        monkeypatch.setattr(L._GIT_LOCAL_SWALLOW_LOG, "every", 4)
        self._stub_run_git(monkeypatch, add_rc=1)
        for _ in range(5):
            assert L._commit_and_push("msg", push=True) == "failed"
        add_lines = [l for l in log_lines if "git_local failed" in l and "git add" in l]
        # count=1 logs, count=4 logs.
        assert len(add_lines) == 2

    def test_git_commit_failure_rate_limited(self, monkeypatch):
        from agent import loop as L
        L._GIT_LOCAL_SWALLOW_LOG.reset()
        log_lines = []
        monkeypatch.setattr(L, "_log", lambda m: log_lines.append(m))
        monkeypatch.setattr(L._GIT_LOCAL_SWALLOW_LOG, "schedule", "linear")
        monkeypatch.setattr(L._GIT_LOCAL_SWALLOW_LOG, "every", 100)
        self._stub_run_git(monkeypatch, commit_rc=1)
        L._commit_and_push("msg", push=True)
        c_lines = [l for l in log_lines if "git commit" in l and "git_local failed" in l]
        assert len(c_lines) == 1
        assert "commit-err" in c_lines[0]

    def test_git_loggers_registered_for_summary(self):
        from agent import loop as L
        labels = {lg.label for lg in L._swallow_loggers()}
        assert "git_remote" in labels
        assert "git_local" in labels


class TestRevertChangesRateLimited:
    """Loop 78: `_revert_changes` failure paths route through
    `_REVERT_SWALLOW_LOG` so persistent corrupt-repo states stop
    spamming."""

    def test_revert_logger_registered(self):
        from agent import loop as L
        labels = {lg.label for lg in L._swallow_loggers()}
        assert "_revert_changes" in labels

    def test_repeated_failures_rate_limited(self, monkeypatch):
        from agent import loop as L
        import subprocess
        L._REVERT_SWALLOW_LOG.reset()
        monkeypatch.setattr(L._REVERT_SWALLOW_LOG, "schedule", "linear")
        monkeypatch.setattr(L._REVERT_SWALLOW_LOG, "every", 100)
        log_lines = []
        monkeypatch.setattr(L, "_log", lambda m: log_lines.append(m))

        def fake_run(*a, **kw):
            return subprocess.CompletedProcess(
                args=["git", *a], returncode=128, stdout="",
                stderr="fatal: bad object",
            )
        monkeypatch.setattr(L, "_run_git", fake_run)
        L._revert_changes()
        # 4 failures (checkout, clean, HEAD reset, origin reset).
        # With linear every=100: count=1 logs (checkout), 2-4 suppressed.
        first_pass_lines = [l for l in log_lines if "_revert_changes failed" in l]
        assert len(first_pass_lines) == 1
        assert L._REVERT_SWALLOW_LOG.count == 4
        L._REVERT_SWALLOW_LOG.reset()

    def test_success_recovery_log_not_rate_limited(self, monkeypatch):
        """The 'recovered via' info logs are still bare _log calls so
        operators always see successful recoveries."""
        from agent import loop as L
        import subprocess
        L._REVERT_SWALLOW_LOG.reset()
        log_lines = []
        monkeypatch.setattr(L, "_log", lambda m: log_lines.append(m))
        # checkout fails, clean fails, reset HEAD succeeds.
        rcs = {"checkout": 1, "clean": 1, "reset": 0}
        def fake_run(*a, **kw):
            sub = a[0]
            return subprocess.CompletedProcess(
                args=["git", *a], returncode=rcs.get(sub, 0),
                stdout="", stderr="boom",
            )
        monkeypatch.setattr(L, "_run_git", fake_run)
        assert L._revert_changes() is True
        assert any("recovered via reset --hard" in l for l in log_lines)
        L._REVERT_SWALLOW_LOG.reset()


class TestModuleDocstringRecoveryContract:
    """Loop 79: keep the recovery contract documented at module level."""

    def test_docstring_mentions_revert_cascade(self):
        from agent import loop as L
        doc = L.__doc__ or ""
        assert "_abort_rebase_if_any" in doc
        assert "_revert_changes" in doc
        assert "origin/main" in doc

    def test_docstring_lists_swallow_logger_sinks(self):
        from agent import loop as L
        doc = L.__doc__ or ""
        for label in {lg.label for lg in L._swallow_loggers()}:
            assert label in doc, f"missing logger label {label!r} in module docstring"


class TestRunGitTimeoutRateLimited:
    """Loop 80: `_run_git` timeouts route through
    `_GIT_TIMEOUT_SWALLOW_LOG` so a hung git binary or unreachable
    remote stops spamming one log line per call."""

    def test_timeout_logger_registered(self):
        from agent import loop as L
        labels = {lg.label for lg in L._swallow_loggers()}
        assert "_run_git_timeout" in labels

    def test_repeated_timeouts_rate_limited(self, monkeypatch):
        from agent import loop as L
        import subprocess
        L._GIT_TIMEOUT_SWALLOW_LOG.reset()
        monkeypatch.setattr(L._GIT_TIMEOUT_SWALLOW_LOG, "schedule", "linear")
        monkeypatch.setattr(L._GIT_TIMEOUT_SWALLOW_LOG, "every", 5)
        log_lines = []
        monkeypatch.setattr(L, "_log", lambda m: log_lines.append(m))

        def hung_run(*a, **kw):
            raise subprocess.TimeoutExpired(cmd=a, timeout=kw.get("timeout", 60))
        monkeypatch.setattr(L.subprocess, "run", hung_run)

        for _ in range(7):
            cp = L._run_git("status", "--porcelain", check=False)
            assert cp.returncode == 124
            assert "timed_out_after_" in cp.stderr
        timeout_lines = [l for l in log_lines if "_run_git_timeout failed" in l]
        # Linear every=5: count=1 logs, count=5 logs.
        assert len(timeout_lines) == 2
        assert L._GIT_TIMEOUT_SWALLOW_LOG.count == 7
        # Context (the git args) appears in the line.
        assert any("git status --porcelain" in l for l in timeout_lines)
        L._GIT_TIMEOUT_SWALLOW_LOG.reset()

    def test_timeout_with_check_true_still_raises(self, monkeypatch):
        from agent import loop as L
        import subprocess
        L._GIT_TIMEOUT_SWALLOW_LOG.reset()
        monkeypatch.setattr(L, "_log", lambda m: None)

        def hung_run(*a, **kw):
            raise subprocess.TimeoutExpired(cmd=a, timeout=kw.get("timeout", 60))
        monkeypatch.setattr(L.subprocess, "run", hung_run)

        import pytest
        with pytest.raises(subprocess.TimeoutExpired):
            L._run_git("status", check=True)
        # check=True path bypasses the rate limiter entirely.
        assert L._GIT_TIMEOUT_SWALLOW_LOG.count == 0


class TestSwallowLoggerReportReturnsBool:
    """Loop 81: `report()` returns True iff it logged this call."""

    def test_first_report_returns_true(self, monkeypatch):
        from agent import loop as L
        lg = L._RateLimitedSwallowLogger("rb1", every=100, schedule="linear")
        monkeypatch.setattr(L, "_log", lambda m: None)
        assert lg.report(OSError("e")) is True

    def test_suppressed_reports_return_false(self, monkeypatch):
        from agent import loop as L
        lg = L._RateLimitedSwallowLogger("rb2", every=10, schedule="linear")
        monkeypatch.setattr(L, "_log", lambda m: None)
        assert lg.report(OSError("e")) is True   # count=1 logs
        assert lg.report(OSError("e")) is False  # count=2 suppressed
        assert lg.report(OSError("e")) is False  # count=3 suppressed

    def test_periodic_emit_returns_true(self, monkeypatch):
        from agent import loop as L
        lg = L._RateLimitedSwallowLogger("rb3", every=3, schedule="linear")
        monkeypatch.setattr(L, "_log", lambda m: None)
        results = [lg.report(OSError("e")) for _ in range(7)]
        # count=1 True, 2 False, 3 True (3%3=0), 4 False, 5 False, 6 True, 7 False.
        assert results == [True, False, True, False, False, True, False]

    def test_exponential_schedule_powers_of_two(self, monkeypatch):
        from agent import loop as L
        lg = L._RateLimitedSwallowLogger("rb4", every=8, schedule="exponential")
        monkeypatch.setattr(L, "_log", lambda m: None)
        results = [lg.report(OSError("e")) for _ in range(10)]
        # 1=T, 2=T, 3=F, 4=T, 5-7=F, 8=T, 9=F, 10=F.
        assert results == [True, True, False, True, False, False, False,
                           True, False, False]

    def test_callsite_can_bind_extra_work(self, monkeypatch):
        """Demonstrates the intended use case: extra diagnostic dumps
        only on logging iterations."""
        from agent import loop as L
        lg = L._RateLimitedSwallowLogger("rb5", every=10, schedule="linear")
        monkeypatch.setattr(L, "_log", lambda m: None)
        extra_dumps = []
        for _ in range(5):
            if lg.report(OSError("e")):
                extra_dumps.append("dumped")
        assert extra_dumps == ["dumped"]  # only count=1


class TestAggregateSwallowSummary:
    """Loop 82: `main()` periodic aggregate snapshot of every swallow
    logger's cumulative count, env-tunable cadence, default every 100
    iterations."""

    def test_emits_nothing_when_all_counts_zero(self, monkeypatch):
        from agent import loop as L
        for lg in L._swallow_loggers():
            lg.reset()
        log_lines = []
        monkeypatch.setattr(L, "_log", lambda m: log_lines.append(m))
        L._log_aggregate_swallow_summary(100)
        assert log_lines == []

    def test_emits_when_any_count_nonzero(self, monkeypatch):
        from agent import loop as L
        for lg in L._swallow_loggers():
            lg.reset()
        L._TIMING_SWALLOW_LOG.report(OSError("boom"))
        log_lines = []
        monkeypatch.setattr(L, "_log", lambda m: log_lines.append(m))
        L._log_aggregate_swallow_summary(250)
        assert len(log_lines) == 1
        line = log_lines[0]
        assert "aggregate-swallow-summary" in line
        assert "iter=250" in line
        assert "_write_timing=1" in line
        # Other loggers still appear with =0.
        for lg in L._swallow_loggers():
            if lg is not L._TIMING_SWALLOW_LOG:
                assert f"{lg.label}=0" in line
        for lg in L._swallow_loggers():
            lg.reset()

    def test_never_raises_on_broken_logger(self, monkeypatch):
        from agent import loop as L
        class _Bad:
            label = "bad"
            def summary(self):
                raise RuntimeError("x")
        good = L._RateLimitedSwallowLogger("good", every=1, schedule="linear")
        good.report(OSError("e"))
        monkeypatch.setattr(L, "_swallow_loggers", lambda: (_Bad(), good))
        log_lines = []
        monkeypatch.setattr(L, "_log", lambda m: log_lines.append(m))
        L._log_aggregate_swallow_summary(1)
        # Only good logger contributes; broken one is skipped silently.
        assert len(log_lines) == 1
        assert "good=1" in log_lines[0]

    def test_aggregate_every_clamped(self, monkeypatch):
        from agent import loop as L
        monkeypatch.setenv("QWEN_AGGREGATE_SUMMARY_EVERY", "0")
        assert L._aggregate_summary_every() == L._AGGREGATE_SUMMARY_EVERY_DEFAULT
        monkeypatch.setenv("QWEN_AGGREGATE_SUMMARY_EVERY", "999999999")
        assert L._aggregate_summary_every() == L._AGGREGATE_SUMMARY_EVERY_MAX
        monkeypatch.setenv("QWEN_AGGREGATE_SUMMARY_EVERY", "garbage")
        assert L._aggregate_summary_every() == L._AGGREGATE_SUMMARY_EVERY_DEFAULT
        monkeypatch.setenv("QWEN_AGGREGATE_SUMMARY_EVERY", "50")
        assert L._aggregate_summary_every() == 50


class TestTimingWallSeconds:
    """Loop 83: timing.log records include `wall_s` total iteration
    wallclock so analytics can distinguish slow Qwen response from slow
    scaffolding."""

    def test_write_timing_includes_wall_s_when_iter_monotonic_given(
        self, env, monkeypatch
    ):
        from agent import loop as L
        import json
        # _write_timing makes one time.monotonic() call when iter_monotonic
        # is provided. Force it to return 105.5 → delta = 5.5.
        monkeypatch.setattr(L.time, "monotonic", lambda: 105.5)
        from pathlib import Path
        L._write_timing(Path("f.py"), "ok", {"x": 0.1}, iter_monotonic=100.0)
        # Read last line.
        line = L.TIMING_FILE.read_text().splitlines()[-1]
        rec = json.loads(line)
        assert "wall_s" in rec
        assert rec["wall_s"] == 5.5

    def test_write_timing_omits_wall_s_when_no_iter_monotonic(self, env):
        from agent import loop as L
        import json
        from pathlib import Path
        L._write_timing(Path("f.py"), "ok", {"x": 0.1})
        rec = json.loads(L.TIMING_FILE.read_text().splitlines()[-1])
        assert "wall_s" not in rec

    def test_iteration_writes_wall_s_to_timing(self, env, monkeypatch):
        """End-to-end: a real iteration produces a timing record with
        wall_s populated (since `_iteration` always passes it through)."""
        from agent import loop as L
        import json
        client = _ScriptedClient(["\n   \n"])  # empty issue → quick exit
        L._iteration(client, max_bytes=10_000, push=False)
        rec = json.loads(L.TIMING_FILE.read_text().splitlines()[-1])
        assert "wall_s" in rec
        assert isinstance(rec["wall_s"], (int, float))
        assert rec["wall_s"] >= 0.0


class TestSwallowLoggerLastMessage:
    """Loop 84: `_RateLimitedSwallowLogger.last_log_message` stores the
    most recent emitted line so a future SIGUSR1 dump (and operators
    inspecting from a debugger) can see what the last surfaced failure
    actually said. Suppressed reports do not overwrite it."""

    def test_initial_last_log_message_is_none(self):
        from agent import loop as L
        lg = L._RateLimitedSwallowLogger("test_init", every=1)
        try:
            assert lg.last_log_message is None
        finally:
            lg.reset()

    def test_first_emit_sets_last_log_message(self, monkeypatch):
        from agent import loop as L
        lg = L._RateLimitedSwallowLogger("test_first", every=1)
        try:
            emitted = lg.report(RuntimeError("boom"))
            assert emitted is True
            assert lg.last_log_message is not None
            assert "test_first failed" in lg.last_log_message
            assert "boom" in lg.last_log_message
        finally:
            lg.reset()

    def test_suppressed_reports_do_not_overwrite_last_message(
        self, monkeypatch
    ):
        from agent import loop as L
        lg = L._RateLimitedSwallowLogger(
            "test_supp", every=10, schedule="linear"
        )
        try:
            lg.report(RuntimeError("first"))
            first_msg = lg.last_log_message
            assert first_msg is not None
            # Counts 2..9 are suppressed under linear every=10.
            for _ in range(8):
                lg.report(RuntimeError("suppressed"))
            assert lg.last_log_message == first_msg
        finally:
            lg.reset()

    def test_subsequent_emit_overwrites_last_message(self):
        from agent import loop as L
        lg = L._RateLimitedSwallowLogger("test_over", every=1)
        try:
            lg.report(RuntimeError("first"))
            lg.report(RuntimeError("second"))
            assert "second" in lg.last_log_message
        finally:
            lg.reset()

    def test_reset_clears_last_log_message(self):
        from agent import loop as L
        lg = L._RateLimitedSwallowLogger("test_reset", every=1)
        lg.report(RuntimeError("boom"))
        assert lg.last_log_message is not None
        lg.reset()
        assert lg.last_log_message is None

    def test_summary_includes_last_log_message(self):
        from agent import loop as L
        lg = L._RateLimitedSwallowLogger("test_sum", every=1)
        try:
            lg.report(RuntimeError("boom"))
            s = lg.summary()
            assert "last_log_message" in s
            assert "boom" in s["last_log_message"]
        finally:
            lg.reset()

    def test_context_is_included_in_last_log_message(self):
        from agent import loop as L
        lg = L._RateLimitedSwallowLogger("test_ctx", every=1)
        try:
            lg.report(RuntimeError("boom"), context="idx=42")
            assert "[idx=42]" in lg.last_log_message
        finally:
            lg.reset()


class TestDumpLoggerState:
    """Loop 85: `_dump_logger_state()` snapshot writer + SIGUSR1 handler."""

    def test_dump_logger_state_emits_begin_and_end_markers(
        self, tmp_path, monkeypatch
    ):
        from agent import loop as L
        log_path = tmp_path / "loop.log"
        monkeypatch.setattr(L, "LOG_FILE", log_path)
        L._dump_logger_state(reason="test")
        text = log_path.read_text()
        assert "logger-state-dump reason=test begin" in text
        assert "logger-state-dump reason=test end" in text

    def test_dump_logger_state_emits_one_line_per_logger(
        self, tmp_path, monkeypatch
    ):
        from agent import loop as L
        log_path = tmp_path / "loop.log"
        monkeypatch.setattr(L, "LOG_FILE", log_path)
        L._dump_logger_state(reason="test")
        text = log_path.read_text()
        # Each logger's summary line contains "logger-state-dump {".
        body_lines = [
            ln for ln in text.splitlines() if "logger-state-dump {" in ln
        ]
        assert len(body_lines) == len(L._swallow_loggers())

    def test_dump_logger_state_includes_last_log_message_field(
        self, tmp_path, monkeypatch
    ):
        from agent import loop as L
        log_path = tmp_path / "loop.log"
        monkeypatch.setattr(L, "LOG_FILE", log_path)
        # Trigger one report so at least one logger has a non-None message.
        L._STATE_SWALLOW_LOG.reset()
        try:
            monkeypatch.setattr(L._STATE_SWALLOW_LOG, "every", 1)
            L._STATE_SWALLOW_LOG.report(RuntimeError("seeded"))
            L._dump_logger_state(reason="test")
            text = log_path.read_text()
            assert "last_log_message" in text
            assert "seeded" in text
        finally:
            L._STATE_SWALLOW_LOG.reset()

    def test_dump_logger_state_swallows_internal_errors(
        self, tmp_path, monkeypatch
    ):
        """Even if one logger's summary blows up, the dump must finish."""
        from agent import loop as L
        log_path = tmp_path / "loop.log"
        monkeypatch.setattr(L, "LOG_FILE", log_path)

        class _Bad:
            label = "bad"
            def summary(self):
                raise RuntimeError("cannot summarize")

        original = L._swallow_loggers
        monkeypatch.setattr(
            L, "_swallow_loggers",
            lambda: tuple(list(original()) + [_Bad()])
        )
        # Should not raise.
        L._dump_logger_state(reason="badtest")
        text = log_path.read_text()
        assert "logger-state-dump reason=badtest end" in text
        assert "summary failed" in text

    def test_install_sigusr1_handler_returns_true_on_posix(self):
        from agent import loop as L
        import sys
        if sys.platform == "win32":
            assert L._install_sigusr1_handler() is False
        else:
            assert L._install_sigusr1_handler() is True


class TestMainAggregateCadence:
    """Loop 86: end-to-end cadence test for `main()` calling
    `_log_aggregate_swallow_summary` exactly once per `aggregate_every`
    iterations and never on iterations that don't cleanly divide."""

    def _run_main_for_n_iterations(self, monkeypatch, n: int, every: int):
        from agent import loop as L

        # Stub the heavy bits.
        agg_calls: list[int] = []
        monkeypatch.setattr(
            L, "_log_aggregate_swallow_summary",
            lambda i: agg_calls.append(i),
        )
        monkeypatch.setattr(L, "_aggregate_summary_every", lambda: every)
        monkeypatch.setattr(L, "_iteration", lambda *a, **kw: "ok:noop")
        monkeypatch.setattr(L, "_install_sigusr1_handler", lambda: True)
        monkeypatch.setattr(L, "_log", lambda m: None)
        # Settings stub.
        from types import SimpleNamespace
        fake_settings = SimpleNamespace(
            model="x", base_url="y", loop_interval_seconds=0,
            loop_max_file_bytes=10_000, loop_push=False,
        )
        import sys as _sys
        config_mod = _sys.modules.get("qwen_coder_mcp.config")
        if config_mod is None:
            import types
            config_mod = types.ModuleType("qwen_coder_mcp.config")
            _sys.modules["qwen_coder_mcp"] = types.ModuleType("qwen_coder_mcp")
            _sys.modules["qwen_coder_mcp.config"] = config_mod
        monkeypatch.setattr(
            config_mod, "load_settings", lambda: fake_settings, raising=False
        )

        class _StubClient:
            def __init__(self, *a, **kw): pass
            def close(self): pass
        monkeypatch.setattr(L, "QwenClient", _StubClient)

        # Break out after n iterations by raising from time.sleep.
        sleep_calls = {"n": 0}
        def _stop_after_n(_s):
            sleep_calls["n"] += 1
            if sleep_calls["n"] >= n:
                raise KeyboardInterrupt()
        monkeypatch.setattr(L.time, "sleep", _stop_after_n)
        try:
            L.main()
        except KeyboardInterrupt:
            pass
        return agg_calls

    def test_aggregate_called_at_correct_cadence(self, monkeypatch):
        # every=3 over 9 iterations: should fire at iter 3, 6, 9.
        agg = self._run_main_for_n_iterations(monkeypatch, n=9, every=3)
        assert agg == [3, 6, 9]

    def test_aggregate_not_called_when_every_is_zero(self, monkeypatch):
        # every<=0 disables aggregate emission entirely.
        agg = self._run_main_for_n_iterations(monkeypatch, n=5, every=0)
        assert agg == []

    def test_aggregate_not_called_before_first_cadence_boundary(
        self, monkeypatch
    ):
        # every=10 over 7 iterations: never fires.
        agg = self._run_main_for_n_iterations(monkeypatch, n=7, every=10)
        assert agg == []

    def test_aggregate_fires_on_iteration_crash_too(self, monkeypatch):
        # Even when _iteration raises, the count still advances and
        # the cadence boundary still fires.
        from agent import loop as L

        agg_calls: list[int] = []
        monkeypatch.setattr(
            L, "_log_aggregate_swallow_summary",
            lambda i: agg_calls.append(i),
        )
        monkeypatch.setattr(L, "_aggregate_summary_every", lambda: 2)

        def _boom(*a, **kw):
            raise RuntimeError("boom")
        monkeypatch.setattr(L, "_iteration", _boom)
        monkeypatch.setattr(L, "_install_sigusr1_handler", lambda: True)
        monkeypatch.setattr(L, "_log", lambda m: None)

        from types import SimpleNamespace
        fake_settings = SimpleNamespace(
            model="x", base_url="y", loop_interval_seconds=0,
            loop_max_file_bytes=10_000, loop_push=False,
        )
        import sys as _sys
        config_mod = _sys.modules.get("qwen_coder_mcp.config")
        monkeypatch.setattr(
            config_mod, "load_settings", lambda: fake_settings, raising=False
        )

        class _StubClient:
            def __init__(self, *a, **kw): pass
            def close(self): pass
        monkeypatch.setattr(L, "QwenClient", _StubClient)

        sleep_calls = {"n": 0}
        def _stop_after_n(_s):
            sleep_calls["n"] += 1
            if sleep_calls["n"] >= 4:
                raise KeyboardInterrupt()
        monkeypatch.setattr(L.time, "sleep", _stop_after_n)
        try:
            L.main()
        except KeyboardInterrupt:
            pass
        # 4 iterations, cadence=2 -> fires at 2, 4.
        assert agg_calls == [2, 4]


class TestDumpLoggerStateExtended:
    """Loop 87: _dump_logger_state now includes iteration marker and
    last-summary-counts snapshot for full diagnostic context."""

    def test_iteration_marker_in_begin_and_end_when_provided(
        self, tmp_path, monkeypatch
    ):
        from agent import loop as L
        log_path = tmp_path / "loop.log"
        monkeypatch.setattr(L, "LOG_FILE", log_path)
        L._dump_logger_state(reason="test", iteration=42)
        text = log_path.read_text()
        assert "logger-state-dump reason=test iter=42 begin" in text
        assert "logger-state-dump reason=test iter=42 end" in text

    def test_iteration_marker_omitted_when_none(self, tmp_path, monkeypatch):
        from agent import loop as L
        log_path = tmp_path / "loop.log"
        monkeypatch.setattr(L, "LOG_FILE", log_path)
        L._dump_logger_state(reason="test")
        text = log_path.read_text()
        assert "logger-state-dump reason=test begin" in text
        assert "iter=" not in text.split("begin")[0]

    def test_last_summary_counts_emitted(self, tmp_path, monkeypatch):
        from agent import loop as L
        log_path = tmp_path / "loop.log"
        monkeypatch.setattr(L, "LOG_FILE", log_path)
        # Seed the cache.
        L._LAST_SWALLOW_SUMMARY_COUNTS["seeded_label"] = 99
        try:
            L._dump_logger_state(reason="test")
            text = log_path.read_text()
            assert "last-summary-counts" in text
            assert "seeded_label" in text
            assert "99" in text
        finally:
            L._LAST_SWALLOW_SUMMARY_COUNTS.pop("seeded_label", None)

    def test_main_updates_current_iteration_for_signal_handler(
        self, monkeypatch
    ):
        """The module-level _CURRENT_ITERATION must track main()'s loop
        counter so a SIGUSR1 mid-run dumps the right number."""
        from agent import loop as L
        monkeypatch.setattr(L, "_aggregate_summary_every", lambda: 0)
        monkeypatch.setattr(L, "_iteration", lambda *a, **kw: "ok:noop")
        monkeypatch.setattr(L, "_install_sigusr1_handler", lambda: True)
        monkeypatch.setattr(L, "_log", lambda m: None)
        from types import SimpleNamespace
        fake_settings = SimpleNamespace(
            model="x", base_url="y", loop_interval_seconds=0,
            loop_max_file_bytes=10_000, loop_push=False,
        )
        import sys as _sys
        config_mod = _sys.modules.get("qwen_coder_mcp.config")
        monkeypatch.setattr(
            config_mod, "load_settings", lambda: fake_settings, raising=False
        )

        class _StubClient:
            def __init__(self, *a, **kw): pass
            def close(self): pass
        monkeypatch.setattr(L, "QwenClient", _StubClient)

        observed: list[int] = []
        sleep_calls = {"n": 0}
        def _sleep(_s):
            observed.append(L._CURRENT_ITERATION)
            sleep_calls["n"] += 1
            if sleep_calls["n"] >= 5:
                raise KeyboardInterrupt()
        monkeypatch.setattr(L.time, "sleep", _sleep)

        # Reset to ensure clean baseline.
        L._CURRENT_ITERATION = 0
        try:
            L.main()
        except KeyboardInterrupt:
            pass
        assert observed == [1, 2, 3, 4, 5]


class TestStartupDiagnosticsLog:
    """Loop 88: main() should log aggregate-summary cadence + SIGUSR1
    handler status at startup so operators can see what to expect from
    the very first log line."""

    def test_startup_logs_aggregate_every_and_sigusr1(self, monkeypatch):
        from agent import loop as L
        log_lines: list[str] = []
        monkeypatch.setattr(L, "_log", lambda m: log_lines.append(m))
        monkeypatch.setattr(L, "_aggregate_summary_every", lambda: 250)
        monkeypatch.setattr(L, "_install_sigusr1_handler", lambda: True)
        monkeypatch.setattr(L, "_iteration", lambda *a, **kw: "ok")
        from types import SimpleNamespace
        fake_settings = SimpleNamespace(
            model="x", base_url="y", loop_interval_seconds=0,
            loop_max_file_bytes=10_000, loop_push=False,
        )
        import sys as _sys
        config_mod = _sys.modules.get("qwen_coder_mcp.config")
        monkeypatch.setattr(
            config_mod, "load_settings", lambda: fake_settings, raising=False
        )

        class _StubClient:
            def __init__(self, *a, **kw): pass
            def close(self): pass
        monkeypatch.setattr(L, "QwenClient", _StubClient)
        monkeypatch.setattr(
            L.time, "sleep", lambda _s: (_ for _ in ()).throw(KeyboardInterrupt())
        )
        try:
            L.main()
        except KeyboardInterrupt:
            pass
        diag = [l for l in log_lines if "loop diagnostics" in l]
        assert len(diag) == 1
        assert "aggregate_summary_every=250" in diag[0]
        assert "sigusr1_handler=installed" in diag[0]

    def test_startup_logs_sigusr1_unavailable_on_windows_path(self, monkeypatch):
        from agent import loop as L
        log_lines: list[str] = []
        monkeypatch.setattr(L, "_log", lambda m: log_lines.append(m))
        monkeypatch.setattr(L, "_aggregate_summary_every", lambda: 100)
        monkeypatch.setattr(L, "_install_sigusr1_handler", lambda: False)
        monkeypatch.setattr(L, "_iteration", lambda *a, **kw: "ok")
        from types import SimpleNamespace
        fake_settings = SimpleNamespace(
            model="x", base_url="y", loop_interval_seconds=0,
            loop_max_file_bytes=10_000, loop_push=False,
        )
        import sys as _sys
        config_mod = _sys.modules.get("qwen_coder_mcp.config")
        monkeypatch.setattr(
            config_mod, "load_settings", lambda: fake_settings, raising=False
        )

        class _StubClient:
            def __init__(self, *a, **kw): pass
            def close(self): pass
        monkeypatch.setattr(L, "QwenClient", _StubClient)
        monkeypatch.setattr(
            L.time, "sleep", lambda _s: (_ for _ in ()).throw(KeyboardInterrupt())
        )
        try:
            L.main()
        except KeyboardInterrupt:
            pass
        diag = [l for l in log_lines if "loop diagnostics" in l]
        assert len(diag) == 1
        assert "sigusr1_handler=unavailable" in diag[0]


class TestSigusr1DocumentedInDocstring:
    """Loop 88: keep module docstring synced with runtime introspection
    capability so the agent can rediscover SIGUSR1 from the source."""

    def test_module_docstring_mentions_sigusr1(self):
        from agent import loop as L
        assert L.__doc__ is not None
        assert "SIGUSR1" in L.__doc__

    def test_module_docstring_mentions_dump_logger_state(self):
        from agent import loop as L
        assert "_dump_logger_state" in L.__doc__

    def test_module_docstring_mentions_aggregate_cadence(self):
        from agent import loop as L
        assert "QWEN_AGGREGATE_SUMMARY_EVERY" in L.__doc__
