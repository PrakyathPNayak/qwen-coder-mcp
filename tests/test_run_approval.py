"""Tests for the loop-250 ``/run`` approval gate.

Pre-loop-250 ``/run <cmd>`` shelled out immediately. The user asked
for Claude-Code-style approval flow: by default ``/run`` must require
explicit consent. Two consent paths:

  * Inline: ``/run --yes <cmd>`` (or ``-y``) — one-shot approve.
  * Session: ``/run_on`` toggles auto-approve until ``/run_off``.

Default state is *deny*, so a chat-injected user_text containing a
``/run`` slash command can't silently shell out.

These tests pin:
  * ``_parse_run_body`` argument-stripping
  * ``_render_run(confirm=...)`` happy path + denial path
  * ``_render_run`` swallows confirm-hook exceptions
  * Dispatcher routes ``/run`` through the gate and respects the
    session-level ``app.run_auto_approve`` flag
  * ``/run_on`` / ``/run_off`` mutate the session flag
  * ``/run_on`` / ``/run_off`` survive a missing app object
  * Help text + completion include the new commands
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from qwen_coder_mcp import fs_tools, shell_tools, tui


# ---------------------------------------------------------- parser
class TestParseRunBody:
    def test_no_flag(self):
        assert tui._parse_run_body("echo hi") == (False, "echo hi")

    def test_yes_flag(self):
        assert tui._parse_run_body("--yes echo hi") == (True, "echo hi")

    def test_short_y_flag(self):
        assert tui._parse_run_body("-y ls -la") == (True, "ls -la")

    def test_empty_body(self):
        assert tui._parse_run_body("") == (False, "")

    def test_yes_alone_is_no_command(self):
        assert tui._parse_run_body("--yes") == (True, "")

    def test_yes_in_middle_not_consumed(self):
        # --yes elsewhere in the cmd (e.g. as a sed flag) is NOT a
        # consent flag — only at start-of-body.
        assert tui._parse_run_body("sed --yes-please") == (False, "sed --yes-please")

    def test_leading_whitespace_stripped(self):
        assert tui._parse_run_body("   --yes echo hi") == (True, "echo hi")


# ---------------------------------------------------------- render
class TestRenderRunGate:
    def _cfg(self, tmp_path: Path) -> fs_tools.FsConfig:
        return fs_tools.FsConfig(root=tmp_path)

    def test_no_confirm_executes_for_back_compat(self, tmp_path):
        # When confirm=None, behavior is the legacy auto-execute path.
        out = tui._render_run(self._cfg(tmp_path), "echo hello-loop250")
        assert "hello-loop250" in out

    def test_confirm_true_executes(self, tmp_path):
        seen = []

        def yes(c):
            seen.append(c)
            return True

        out = tui._render_run(
            self._cfg(tmp_path), "echo go", confirm=yes
        )
        assert seen == ["echo go"]
        assert "go" in out

    def test_confirm_false_denies(self, tmp_path):
        out = tui._render_run(
            self._cfg(tmp_path), "echo nope", confirm=lambda _c: False
        )
        assert "denied" in out
        assert "echo nope" in out
        assert "--yes" in out  # hint mentions inline approve
        assert "/run_on" in out  # hint mentions session toggle

    def test_confirm_exception_denies(self, tmp_path):
        def boom(_c):
            raise RuntimeError("kaboom")

        out = tui._render_run(self._cfg(tmp_path), "echo x", confirm=boom)
        assert "denied" in out
        assert "RuntimeError" in out

    def test_denied_command_does_not_shell_out(self, tmp_path, monkeypatch):
        called = {"n": 0}

        def fake_run(*a, **k):
            called["n"] += 1
            return shell_tools.RunResult(  # type: ignore[attr-defined]
                cmd="x", returncode=0, stdout="", stderr="", duration_s=0.0
            )

        monkeypatch.setattr(shell_tools, "run_shell", fake_run)
        tui._render_run(
            self._cfg(tmp_path), "echo no", confirm=lambda _c: False
        )
        assert called["n"] == 0


# ---------------------------------------------------------- dispatcher
class TestDispatchRunApproval:
    def _cfg(self, tmp_path: Path) -> fs_tools.FsConfig:
        return fs_tools.FsConfig(root=tmp_path)

    def _client(self):
        return SimpleNamespace(settings=None)

    def test_default_deny_without_app(self, tmp_path):
        cmd = tui.parse_slash("/run echo hi")
        out, exit_flag = tui.dispatch_slash(
            cmd, client=self._client(), fs_cfg=self._cfg(tmp_path)
        )
        assert exit_flag is False
        assert "denied" in out

    def test_default_deny_with_app_flag_off(self, tmp_path):
        app = SimpleNamespace(run_auto_approve=False)
        cmd = tui.parse_slash("/run echo hi")
        out, _ = tui.dispatch_slash(
            cmd, client=self._client(), fs_cfg=self._cfg(tmp_path), app=app
        )
        assert "denied" in out

    def test_inline_yes_overrides_default_deny(self, tmp_path):
        cmd = tui.parse_slash("/run --yes echo loop250-inline")
        out, _ = tui.dispatch_slash(
            cmd, client=self._client(), fs_cfg=self._cfg(tmp_path)
        )
        assert "denied" not in out
        assert "loop250-inline" in out

    def test_short_y_overrides_default_deny(self, tmp_path):
        cmd = tui.parse_slash("/run -y echo loop250-short")
        out, _ = tui.dispatch_slash(
            cmd, client=self._client(), fs_cfg=self._cfg(tmp_path)
        )
        assert "loop250-short" in out

    def test_session_auto_approve_executes(self, tmp_path):
        app = SimpleNamespace(run_auto_approve=True)
        cmd = tui.parse_slash("/run echo loop250-session")
        out, _ = tui.dispatch_slash(
            cmd, client=self._client(), fs_cfg=self._cfg(tmp_path), app=app
        )
        assert "denied" not in out
        assert "loop250-session" in out

    def test_run_on_sets_session_flag(self, tmp_path):
        app = SimpleNamespace(run_auto_approve=False)
        cmd = tui.parse_slash("/run_on")
        out, _ = tui.dispatch_slash(
            cmd, client=self._client(), fs_cfg=self._cfg(tmp_path), app=app
        )
        assert app.run_auto_approve is True
        assert "ON" in out

    def test_run_off_clears_session_flag(self, tmp_path):
        app = SimpleNamespace(run_auto_approve=True)
        cmd = tui.parse_slash("/run_off")
        out, _ = tui.dispatch_slash(
            cmd, client=self._client(), fs_cfg=self._cfg(tmp_path), app=app
        )
        assert app.run_auto_approve is False
        assert "OFF" in out

    def test_run_on_without_app_does_not_crash(self, tmp_path):
        cmd = tui.parse_slash("/run_on")
        out, _ = tui.dispatch_slash(
            cmd, client=self._client(), fs_cfg=self._cfg(tmp_path)
        )
        assert "ON" in out  # status line still rendered

    def test_empty_run_returns_usage(self, tmp_path):
        cmd = tui.parse_slash("/run")
        out, _ = tui.dispatch_slash(
            cmd, client=self._client(), fs_cfg=self._cfg(tmp_path)
        )
        assert out.startswith("usage:")

    def test_yes_only_no_command_returns_usage(self, tmp_path):
        cmd = tui.parse_slash("/run --yes")
        out, _ = tui.dispatch_slash(
            cmd, client=self._client(), fs_cfg=self._cfg(tmp_path)
        )
        assert out.startswith("usage:")


# ---------------------------------------------------------- discoverability
class TestDiscoverability:
    def test_help_documents_yes_flag(self):
        assert "--yes" in tui.HELP_TEXT
        assert "/run_on" in tui.HELP_TEXT
        assert "/run_off" in tui.HELP_TEXT

    def test_completion_lists_run_on_off(self):
        comps = tui.slash_completions("/run")
        assert "/run" in comps
        assert "/run_on" in comps
        assert "/run_off" in comps
