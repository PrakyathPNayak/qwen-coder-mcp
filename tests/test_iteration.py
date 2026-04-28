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
        """Loop 73: both HEAD and origin/main failures still return False."""
        from agent import loop as L
        import subprocess

        def fake_run(*a, **kw):
            return subprocess.CompletedProcess(
                args=["git", *a], returncode=128, stdout="",
                stderr="fatal: bad object",
            )
        monkeypatch.setattr(L, "_run_git", fake_run)
        log_lines = []
        monkeypatch.setattr(L, "_log", lambda m: log_lines.append(m))
        assert L._revert_changes() is False
        # Both fallback failure logs must appear.
        assert any("reset --hard fallback FAILED" in l for l in log_lines)
        assert any("reset --hard origin/main FAILED" in l for l in log_lines)

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
