"""Tests for the console-script entry points (``qwen-coder-tui`` and
``qwen-coder-mcp``).

These guard the user-facing CLI surface: ``--help`` must print and exit
zero without booting Textual or asyncio, ``--version`` must print the
package version, and unknown flags must error rather than be silently
swallowed by the underlying app.
"""

from __future__ import annotations

import sys

import pytest

from qwen_coder_mcp import __version__, server, tui


# ---------------------------------------------------------------- TUI


class TestTuiCli:
    def test_help_exits_zero_without_running_app(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as exc:
            tui.main(["--help"])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "qwen-coder-tui" in out
        assert "--version" in out

    def test_version_prints_package_version(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as exc:
            tui.main(["--version"])
        assert exc.value.code == 0
        printed = capsys.readouterr()
        text = printed.out + printed.err
        assert __version__ in text

    def test_unknown_flag_errors(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as exc:
            tui.main(["--definitely-not-a-flag"])
        assert exc.value.code != 0
        err = capsys.readouterr().err
        assert "unrecognized arguments" in err or "error" in err

    def test_help_does_not_import_textual_app(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called = {"build": False}

        def boom() -> None:
            called["build"] = True
            raise AssertionError("--help should not call _build_app")

        monkeypatch.setattr(tui, "_build_app", boom)
        with pytest.raises(SystemExit):
            tui.main(["--help"])
        assert called["build"] is False


# ---------------------------------------------------------------- server


class TestServerCli:
    def test_help_exits_zero_without_running_asyncio(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        called = {"run": False}

        def boom(_coro: object) -> None:
            called["run"] = True
            raise AssertionError("--help should not call asyncio.run")

        monkeypatch.setattr(server.asyncio, "run", boom)
        with pytest.raises(SystemExit) as exc:
            server.main(["--help"])
        assert exc.value.code == 0
        assert called["run"] is False
        out = capsys.readouterr().out
        assert "qwen-coder-mcp" in out

    def test_version_prints_package_version(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as exc:
            server.main(["--version"])
        assert exc.value.code == 0
        printed = capsys.readouterr()
        text = printed.out + printed.err
        assert __version__ in text

    def test_unknown_flag_errors(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as exc:
            server.main(["--no-such-flag"])
        assert exc.value.code != 0
