"""Loop 258 tests: /allow_all, /safe_mode, and /loop start|stop|status|tail."""
from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from qwen_coder_mcp import fs_tools, tui


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def chat(self, history, **kw) -> str:  # pragma: no cover - unused
        return ""


def _dispatch(name: str, *args: str, root: Path):
    cfg = fs_tools.FsConfig(root=root)
    cmd = tui.SlashCommand(name=name, args=tuple(args), rest=" ".join(args))
    return tui.dispatch_slash(cmd, client=_FakeClient(), fs_cfg=cfg)


# ---- mega-toggle sentinels --------------------------------------------------

class TestMegaToggleSentinels:
    def test_allow_all_returns_sentinel(self, tmp_path: Path) -> None:
        text, q = _dispatch("allow_all", root=tmp_path)
        assert q is False
        assert text == tui._AGENT_TOGGLE_SENTINEL + "allow_all"

    def test_safe_mode_returns_sentinel(self, tmp_path: Path) -> None:
        text, q = _dispatch("safe_mode", root=tmp_path)
        assert q is False
        assert text == tui._AGENT_TOGGLE_SENTINEL + "safe_mode"

    def test_completions_include_megatoggles(self) -> None:
        assert "/allow_all" in tui.SLASH_COMMANDS
        assert "/safe_mode" in tui.SLASH_COMMANDS
        assert "/loop" in tui.SLASH_COMMANDS

    def test_help_text_mentions_megatoggles_and_loop(self) -> None:
        assert "/allow_all" in tui.HELP_TEXT
        assert "/safe_mode" in tui.HELP_TEXT
        assert "/loop" in tui.HELP_TEXT


# ---- /loop helpers ----------------------------------------------------------

class TestLoopPidHelpers:
    def test_pid_path_under_agent_dir(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        assert tui._loop_pid_path(cfg) == tmp_path / ".agent" / "loop.pid"

    def test_runtime_log_path_under_loop_dir(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        assert (
            tui._loop_runtime_log_path(cfg) == tmp_path / ".loop" / "runtime.log"
        )

    def test_read_pid_missing_returns_none(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        assert tui._loop_read_pid(cfg) is None

    def test_read_pid_invalid_text(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        p = tui._loop_pid_path(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("not-an-int", encoding="utf-8")
        assert tui._loop_read_pid(cfg) is None

    def test_write_then_read_pid(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        tui._loop_write_pid(cfg, 4242)
        assert tui._loop_read_pid(cfg) == 4242

    def test_clear_pid_idempotent(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        # No file: should not raise.
        tui._loop_clear_pid(cfg)
        tui._loop_write_pid(cfg, 1)
        tui._loop_clear_pid(cfg)
        assert not tui._loop_pid_path(cfg).exists()

    def test_pid_alive_self(self, tmp_path: Path) -> None:
        # Our own pid is alive by definition.
        assert tui._loop_pid_alive(os.getpid()) is True

    def test_pid_alive_zero_is_false(self) -> None:
        assert tui._loop_pid_alive(0) is False

    def test_pid_alive_negative_is_false(self) -> None:
        assert tui._loop_pid_alive(-1) is False

    def test_pid_alive_dead_pid(self, tmp_path: Path) -> None:
        # Pick a pid that almost certainly doesn't exist.
        assert tui._loop_pid_alive(2**31 - 1) is False


# ---- /loop status -----------------------------------------------------------

class TestLoopStatus:
    def test_status_no_pid_file(self, tmp_path: Path) -> None:
        text, _ = _dispatch("loop", "status", root=tmp_path)
        assert "stopped" in text
        assert "no pid file" in text

    def test_status_default_arg_is_status(self, tmp_path: Path) -> None:
        text, _ = _dispatch("loop", root=tmp_path)
        assert "stopped" in text  # Same as `/loop status`.

    def test_status_alive_pid(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        tui._loop_write_pid(cfg, os.getpid())
        text, _ = _dispatch("loop", "status", root=tmp_path)
        assert "running" in text
        assert str(os.getpid()) in text

    def test_status_stale_pid(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        tui._loop_write_pid(cfg, 2**31 - 1)
        text, _ = _dispatch("loop", "status", root=tmp_path)
        assert "stopped" in text
        assert "stale" in text

    def test_status_includes_runtime_log_size(self, tmp_path: Path) -> None:
        (tmp_path / ".loop").mkdir()
        (tmp_path / ".loop" / "runtime.log").write_text("x" * 17, encoding="utf-8")
        text, _ = _dispatch("loop", "status", root=tmp_path)
        assert "17 bytes" in text


# ---- /loop start ------------------------------------------------------------

class TestLoopStart:
    def test_start_spawns_subprocess_and_writes_pid(self, tmp_path: Path) -> None:
        fake_proc = MagicMock(pid=12345)
        with patch("subprocess.Popen", return_value=fake_proc) as popen:
            text, _ = _dispatch("loop", "start", root=tmp_path)
        assert "12345" in text
        assert "started" in text
        # Subprocess actually invoked with `python -m agent.loop`.
        argv = popen.call_args[0][0]
        assert argv[0] == sys.executable
        assert argv[1:] == ["-m", "agent.loop"]
        # PID file persisted.
        cfg = fs_tools.FsConfig(root=tmp_path)
        assert tui._loop_read_pid(cfg) == 12345

    def test_start_refuses_when_already_running(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        tui._loop_write_pid(cfg, os.getpid())  # Real alive pid.
        with patch("subprocess.Popen") as popen:
            text, _ = _dispatch("loop", "start", root=tmp_path)
        assert "already running" in text
        popen.assert_not_called()

    def test_start_clears_stale_pid_then_spawns(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        tui._loop_write_pid(cfg, 2**31 - 1)  # Dead pid.
        fake_proc = MagicMock(pid=999)
        with patch("subprocess.Popen", return_value=fake_proc):
            text, _ = _dispatch("loop", "start", root=tmp_path)
        assert "999" in text
        assert tui._loop_read_pid(cfg) == 999

    def test_start_oserror_is_reported(self, tmp_path: Path) -> None:
        with patch("subprocess.Popen", side_effect=OSError("no python")):
            text, _ = _dispatch("loop", "start", root=tmp_path)
        assert "failed" in text
        cfg = fs_tools.FsConfig(root=tmp_path)
        assert tui._loop_read_pid(cfg) is None


# ---- /loop stop, /loop kill -------------------------------------------------

class TestLoopStop:
    def test_stop_no_pid_file(self, tmp_path: Path) -> None:
        text, _ = _dispatch("loop", "stop", root=tmp_path)
        assert "not running" in text

    def test_stop_stale_pid_clears_file(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        tui._loop_write_pid(cfg, 2**31 - 1)
        text, _ = _dispatch("loop", "stop", root=tmp_path)
        assert "not alive" in text or "stale" in text
        assert tui._loop_read_pid(cfg) is None

    def test_stop_sends_sigterm(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        tui._loop_write_pid(cfg, 7777)
        with patch("qwen_coder_mcp.tui._loop_pid_alive", return_value=True), \
             patch("os.kill") as ok:
            text, _ = _dispatch("loop", "stop", root=tmp_path)
        ok.assert_called_once_with(7777, signal.SIGTERM)
        assert "SIGTERM" in text

    def test_kill_sends_sigkill_and_clears(self, tmp_path: Path) -> None:
        cfg = fs_tools.FsConfig(root=tmp_path)
        tui._loop_write_pid(cfg, 8888)
        with patch("qwen_coder_mcp.tui._loop_pid_alive", return_value=True), \
             patch("os.kill") as ok:
            text, _ = _dispatch("loop", "kill", root=tmp_path)
        ok.assert_called_once_with(8888, signal.SIGKILL)
        assert "SIGKILL" in text
        assert tui._loop_read_pid(cfg) is None


# ---- /loop tail -------------------------------------------------------------

class TestLoopTail:
    def test_tail_missing_log(self, tmp_path: Path) -> None:
        text, _ = _dispatch("loop", "tail", root=tmp_path)
        assert "not found" in text

    def test_tail_default_30_lines(self, tmp_path: Path) -> None:
        d = tmp_path / ".loop"
        d.mkdir()
        lines = [f"line-{i}" for i in range(100)]
        (d / "runtime.log").write_text("\n".join(lines), encoding="utf-8")
        text, _ = _dispatch("loop", "tail", root=tmp_path)
        out_lines = text.splitlines()
        assert len(out_lines) == 30
        assert out_lines[-1] == "line-99"
        assert out_lines[0] == "line-70"

    def test_tail_explicit_n(self, tmp_path: Path) -> None:
        d = tmp_path / ".loop"
        d.mkdir()
        (d / "runtime.log").write_text("a\nb\nc\nd\n", encoding="utf-8")
        text, _ = _dispatch("loop", "tail", "2", root=tmp_path)
        assert text.splitlines() == ["c", "d"]

    def test_tail_invalid_n(self, tmp_path: Path) -> None:
        text, _ = _dispatch("loop", "tail", "abc", root=tmp_path)
        assert "usage" in text

    def test_tail_empty_log(self, tmp_path: Path) -> None:
        d = tmp_path / ".loop"
        d.mkdir()
        (d / "runtime.log").write_text("", encoding="utf-8")
        text, _ = _dispatch("loop", "tail", root=tmp_path)
        assert "empty" in text


class TestLoopUsage:
    def test_unknown_subcommand(self, tmp_path: Path) -> None:
        text, _ = _dispatch("loop", "frobnicate", root=tmp_path)
        assert "usage" in text
