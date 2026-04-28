"""Tests for ``scripts/stop_qwen.sh``.

Symmetry with the ``serve_qwen.sh`` dry-run pinning in
``test_serve_qwen_sh.py``: pin the four observable behaviours of
the stop script so a future refactor cannot silently break them.

Behaviours:
1. No pidfile -> error to stderr, exit 1, do not crash.
2. Stale pidfile (process gone) -> message to stdout, pidfile
   removed, exit 0.
3. Live process -> SIGTERM is sent, pidfile removed, exit 0.
4. Live process that ignores SIGTERM -> escalation to SIGKILL.

The script does ``cd "$(dirname "$0")/.."`` so its pidfile path
is always relative to the parent of its own directory. To test
without polluting the real ``.loop/serve.pid`` of the repo, each
test materialises a sandbox layout::

    <tmp>/scripts/stop_qwen.sh   # copy of the real script
    <tmp>/.loop/serve.pid        # fixture-controlled

and invokes the copy. The script depends on nothing else under
the repo.
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
REAL_SCRIPT = REPO_ROOT / "scripts" / "stop_qwen.sh"


@pytest.fixture
def sandboxed_script(tmp_path: Path) -> Path:
    """Copy ``stop_qwen.sh`` into a sandbox so its cwd-relative
    pidfile path lands inside ``tmp_path`` rather than the real
    repo's ``.loop/`` dir."""
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    dst = scripts_dir / "stop_qwen.sh"
    shutil.copy(REAL_SCRIPT, dst)
    dst.chmod(0o755)
    (tmp_path / ".loop").mkdir()
    return dst


def _run(script: Path, *, expect_zero: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if expect_zero:
        assert proc.returncode == 0, (
            f"script exited {proc.returncode}\n"
            f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
        )
    return proc


class TestStopScriptStaticInvariants:
    def test_real_script_exists_and_is_bash(self) -> None:
        assert REAL_SCRIPT.exists()
        assert REAL_SCRIPT.read_text().startswith("#!/usr/bin/env bash")

    def test_real_script_is_syntactically_valid(self) -> None:
        subprocess.run(["bash", "-n", str(REAL_SCRIPT)], check=True)

    def test_real_script_uses_strict_mode(self) -> None:
        # set -euo pipefail is the load-bearing safety harness; an
        # accidental rewrite that drops it would let a stale
        # variable expansion or a mis-typed kill silently succeed.
        text = REAL_SCRIPT.read_text()
        assert "set -euo pipefail" in text


class TestStopScriptNoPidfile:
    def test_missing_pidfile_exits_nonzero(self, sandboxed_script: Path) -> None:
        # No .loop/serve.pid in the sandbox: the script must NOT
        # crash with a bash error and must NOT swallow this.
        # Operator semantics: "you didn't start me; cannot stop."
        proc = _run(sandboxed_script, expect_zero=False)
        assert proc.returncode == 1
        assert "no pid file" in proc.stderr.lower()


class TestStopScriptStalePidfile:
    def test_stale_pidfile_is_cleaned_up(
        self, sandboxed_script: Path, tmp_path: Path
    ) -> None:
        # Write a PID we know is not running. PID 999999 is well
        # above the default Linux pid_max (4194304 cap, but in
        # practice processes never reach this on a CI box).
        # Use a one-shot subshell that exits to also obtain a
        # guaranteed-stale pid for portability.
        ghost = subprocess.run(
            [sys.executable, "-c", "import os; print(os.getpid())"],
            capture_output=True,
            text=True,
            check=True,
        )
        stale_pid = int(ghost.stdout.strip())
        # By the time the previous Python exited, that PID is free
        # again on this system. Defensive: if it has already been
        # reassigned to some unrelated process, retry with a high
        # synthetic value.
        if _is_process_alive(stale_pid):
            stale_pid = 999_999
        pidfile = tmp_path / ".loop" / "serve.pid"
        pidfile.write_text(str(stale_pid))

        proc = _run(sandboxed_script)
        assert "not running" in proc.stdout.lower()
        # Pidfile must be removed so the next serve_qwen.sh start
        # does not refuse with "already running".
        assert not pidfile.exists()


class TestStopScriptLiveProcess:
    def test_sigterm_stops_live_process_and_removes_pidfile(
        self, sandboxed_script: Path, tmp_path: Path
    ) -> None:
        # Spawn a benign long-lived process whose only job is to
        # exit cleanly on SIGTERM. ``sleep 30`` is in coreutils on
        # any Linux runner.
        child = subprocess.Popen(
            ["sleep", "30"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            pidfile = tmp_path / ".loop" / "serve.pid"
            pidfile.write_text(str(child.pid))

            proc = _run(sandboxed_script)
            assert "stopped pid" in proc.stdout
            assert str(child.pid) in proc.stdout
            # Process must actually be reaped by SIGTERM.
            child.wait(timeout=10)
            assert child.returncode is not None
            # And the pidfile is gone -- otherwise the next start
            # refuses to launch with "already running".
            assert not pidfile.exists()
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)


class TestStopScriptSigkillEscalation:
    def test_process_ignoring_sigterm_is_sigkilled(
        self, sandboxed_script: Path, tmp_path: Path
    ) -> None:
        # The script SIGTERMs, polls for up to 30s, then SIGKILLs.
        # Build a child that traps SIGTERM (ignores it) so we can
        # observe the escalation. Run the child via a tiny Python
        # snippet rather than shelling so signal handling is
        # explicit and portable.
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                # Ignore SIGTERM, sleep forever in 1s ticks.
                "import signal,time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "[time.sleep(1) for _ in iter(int,1)]",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            # Give the child a moment to actually install its
            # SIGTERM handler before the script tries to stop it.
            time.sleep(0.5)
            pidfile = tmp_path / ".loop" / "serve.pid"
            pidfile.write_text(str(child.pid))

            proc = _run(sandboxed_script)
            assert "stopped pid" in proc.stdout
            # The script's escalation loop is up to 30 SIGTERM
            # poll-attempts at 1s each before it falls through to
            # SIGKILL. SIGKILL cannot be ignored, so the child is
            # gone by the time the script returns.
            child.wait(timeout=5)
            # SIGKILL exit codes are -9 in subprocess; SIGTERM-trapped
            # process never exits via TERM, so this proves SIGKILL
            # actually fired.
            assert child.returncode == -signal.SIGKILL
            assert not pidfile.exists()
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)


def _is_process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
