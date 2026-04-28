"""Tests for ``scripts/run_loop.sh``.

Final entry in the scripts/ coverage arc started by loop 205
(serve_qwen.sh) and continued by loops 222 (stop_qwen.sh) and 223
(wait_ready.sh).

Strategy: copy the script into a sandbox so its cwd-relative
``.loop/loop.pid`` lands inside ``tmp_path`` rather than the real
repo's ``.loop/``. Prepend a tempdir to PATH containing a fake
``python`` that does NOT execute ``agent.loop`` -- it just sleeps
forever in the background so we can observe the script's PID
bookkeeping without booting the autonomous loop itself.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_SCRIPT = REPO_ROOT / "scripts" / "run_loop.sh"


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    """Copy ``run_loop.sh`` into a tmp_path/scripts/ layout so its
    ``cd "$(dirname "$0")/.."`` lands in tmp_path."""
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    dst = scripts_dir / "run_loop.sh"
    shutil.copy(REAL_SCRIPT, dst)
    dst.chmod(0o755)
    return dst


@pytest.fixture
def fake_python_overlay(tmp_path: Path) -> tuple[Path, Path]:
    """Provide a fake ``python`` on PATH that writes its argv to a
    capture file and sleeps. Returns (overlay_dir, capture_file).
    """
    overlay = tmp_path / "bin"
    overlay.mkdir()
    capture = tmp_path / "python.calls"
    fake = overlay / "python"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        # Record the args so the test can prove the script invoked
        # ``python -m agent.loop`` (and not, say, ``python3`` or
        # something fork-and-pray).
        f'echo "$@" >> {capture}\n'
        # Sleep just long enough for the script to capture our PID
        # and for the test to inspect it. 60s is well past the
        # test's verification window but still bounded.
        "exec sleep 60\n"
    )
    fake.chmod(0o755)
    return overlay, capture


def _spawn_script(
    sandbox: Path,
    overlay: Path,
    *,
    expect_zero: bool = True,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items()}
    env["PATH"] = f"{overlay}:{env['PATH']}"
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        ["bash", str(sandbox)],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
        check=False,
    )
    if expect_zero:
        assert proc.returncode == 0, (
            f"script exited {proc.returncode}\n"
            f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
        )
    return proc


def _read_pid(sandbox_root: Path) -> int:
    pidfile = sandbox_root / ".loop" / "loop.pid"
    assert pidfile.exists(), f"pidfile missing: {pidfile}"
    return int(pidfile.read_text().strip())


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _reap(pid: int) -> None:
    """Best-effort kill of a sandbox-spawned child to keep the test
    runner clean. Each test calls this on every PID it spawned."""
    if not _is_alive(pid):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    for _ in range(20):
        if not _is_alive(pid):
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return


class TestRunLoopStaticInvariants:
    def test_real_script_exists_and_is_bash(self) -> None:
        assert REAL_SCRIPT.exists()
        assert REAL_SCRIPT.read_text().startswith("#!/usr/bin/env bash")

    def test_real_script_is_syntactically_valid(self) -> None:
        subprocess.run(["bash", "-n", str(REAL_SCRIPT)], check=True)

    def test_real_script_uses_strict_mode(self) -> None:
        text = REAL_SCRIPT.read_text()
        assert "set -euo pipefail" in text

    def test_real_script_invokes_agent_loop_module(self) -> None:
        # The contract is "python -m agent.loop". Pin both halves
        # so a refactor to a console-script entry point cannot
        # silently change semantics without updating this test.
        text = REAL_SCRIPT.read_text()
        assert "python -m agent.loop" in text

    def test_real_script_uses_nohup_for_detachment(self) -> None:
        # The whole point of run_loop.sh is "detached background
        # process that survives this shell." Without nohup the
        # autonomous loop dies when the operator's SSH session
        # closes -- a regression we'd notice exactly once, in a
        # very expensive way.
        text = REAL_SCRIPT.read_text()
        assert "nohup" in text


class TestRunLoopFreshStart:
    def test_writes_pidfile_and_starts_loop(
        self,
        sandbox: Path,
        fake_python_overlay: tuple[Path, Path],
        tmp_path: Path,
    ) -> None:
        overlay, capture = fake_python_overlay
        proc = _spawn_script(sandbox, overlay)
        try:
            assert "loop started" in proc.stdout
            pid = _read_pid(tmp_path)
            assert pid > 0
            # Give nohup a moment to actually fork/exec the child.
            time.sleep(0.3)
            assert _is_alive(pid)
            # The child is whatever the fake-python's exec'd sleep
            # ended up as -- on Linux exec replaces in-place so
            # the recorded PID IS the sleep process.
            # And the script invoked python -m agent.loop:
            calls = capture.read_text()
            assert "-m agent.loop" in calls
        finally:
            _reap(pid)


class TestRunLoopAlreadyRunning:
    def test_refuses_to_start_when_pid_alive(
        self, sandbox: Path, fake_python_overlay: tuple[Path, Path], tmp_path: Path
    ) -> None:
        overlay, _ = fake_python_overlay
        # Plant a pidfile pointing at a known-live process. The
        # current pytest process is convenient -- it's definitely
        # alive for the duration of this test.
        loop_dir = tmp_path / ".loop"
        loop_dir.mkdir(exist_ok=True)
        live_pid = os.getpid()
        (loop_dir / "loop.pid").write_text(str(live_pid))

        proc = _spawn_script(sandbox, overlay, expect_zero=False)
        assert proc.returncode == 1
        assert "already running" in proc.stderr.lower()
        # And the pidfile was not overwritten -- we need to keep
        # pointing at the original loop.
        assert (loop_dir / "loop.pid").read_text().strip() == str(live_pid)


class TestRunLoopStalePidfile:
    def test_starts_new_loop_when_pid_is_dead(
        self,
        sandbox: Path,
        fake_python_overlay: tuple[Path, Path],
        tmp_path: Path,
    ) -> None:
        overlay, _ = fake_python_overlay
        # Plant a pidfile that points at a freshly-exited PID.
        # Spawn a Python that prints its PID and exits -- by the
        # time we read the result, the PID is free.
        ghost = subprocess.run(
            [sys.executable, "-c", "import os; print(os.getpid())"],
            capture_output=True,
            text=True,
            check=True,
        )
        stale_pid = int(ghost.stdout.strip())
        if _is_alive(stale_pid):
            stale_pid = 999_999  # defensively-high synthetic value

        loop_dir = tmp_path / ".loop"
        loop_dir.mkdir(exist_ok=True)
        (loop_dir / "loop.pid").write_text(str(stale_pid))

        proc = _spawn_script(sandbox, overlay)
        try:
            assert "loop started" in proc.stdout
            new_pid = _read_pid(tmp_path)
            assert new_pid != stale_pid
            time.sleep(0.3)
            assert _is_alive(new_pid)
        finally:
            new_pid = _read_pid(tmp_path)
            _reap(new_pid)
