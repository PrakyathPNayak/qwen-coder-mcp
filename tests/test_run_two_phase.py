"""Loop 266 -- two-phase /run preview.

``/run <cmd>`` (no --yes, no auto-approve) stages the command into
``app.pending_runs`` and returns a preview block with a stage_id.
The operator confirms with ``/yes <id>`` (executes) or cancels with
``/no <id>``. Backward-compatibility: when ``app`` lacks
``pending_runs`` (legacy stub apps in older tests) the dispatcher
falls back to the loop-250 immediate-deny path.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from qwen_coder_mcp import fs_tools, tui
from qwen_coder_mcp.tui import (
    RUN_STAGE_TTL_S,
    _StagedRun,
    _cancel_stage,
    _consume_stage,
    _format_run_preview,
    _stage_run_command,
)


def _cfg(tmp_path: Path) -> fs_tools.FsConfig:
    (tmp_path / ".agent").mkdir(exist_ok=True)
    return fs_tools.FsConfig(root=tmp_path)


def _client():
    return SimpleNamespace(settings=None)


# ----------------------------------------------------------- pure helpers

class TestStageHelpers:
    def test_stage_returns_id_and_preview(self):
        table: dict[str, _StagedRun] = {}
        sid, preview = _stage_run_command(table, "echo hi")
        assert sid in table
        assert table[sid].cmd == "echo hi"
        assert sid in preview
        assert "echo hi" in preview
        assert "/yes" in preview and "/no" in preview

    def test_consume_ok_removes_entry(self):
        table: dict[str, _StagedRun] = {}
        sid, _ = _stage_run_command(table, "ls")
        status, cmd = _consume_stage(table, sid)
        assert status == "ok"
        assert cmd == "ls"
        assert sid not in table

    def test_consume_missing_when_unknown(self):
        table: dict[str, _StagedRun] = {}
        status, cmd = _consume_stage(table, "deadbeef")
        # empty table -> "empty"; populated but unknown -> "missing"
        _stage_run_command(table, "x")
        status, cmd = _consume_stage(table, "deadbeef")
        assert status == "missing"
        assert cmd is None

    def test_consume_empty_table(self):
        status, cmd = _consume_stage({}, "anything")
        assert status == "empty"
        assert cmd is None

    def test_consume_expired_drops_entry(self):
        table: dict[str, _StagedRun] = {}
        now = 1000.0
        sid, _ = _stage_run_command(table, "ls", now=now)
        status, cmd = _consume_stage(
            table, sid, now=now + RUN_STAGE_TTL_S + 1
        )
        assert status == "expired"
        assert sid not in table

    def test_consume_no_id_picks_latest(self):
        table: dict[str, _StagedRun] = {}
        sid1, _ = _stage_run_command(table, "first", now=100.0)
        sid2, _ = _stage_run_command(table, "second", now=200.0)
        status, cmd = _consume_stage(table, None, now=200.5)
        assert status == "ok"
        assert cmd == "second"
        # First is still there.
        assert sid1 in table
        assert sid2 not in table

    def test_cancel_removes_entry(self):
        table: dict[str, _StagedRun] = {}
        sid, _ = _stage_run_command(table, "rm -rf /")
        status, cmd = _cancel_stage(table, sid)
        assert status == "ok"
        assert cmd == "rm -rf /"
        assert sid not in table

    def test_cap_evicts_oldest(self):
        table: dict[str, _StagedRun] = {}
        for i in range(20):
            _stage_run_command(table, f"cmd{i}", now=float(i), cap=4)
        assert len(table) <= 4
        # Oldest should have been evicted; newest survives.
        kept_cmds = {st.cmd for st in table.values()}
        assert "cmd19" in kept_cmds
        assert "cmd0" not in kept_cmds

    def test_id_is_hex_and_short(self):
        table: dict[str, _StagedRun] = {}
        sid, _ = _stage_run_command(table, "ls")
        assert len(sid) >= 6
        int(sid, 16)  # raises if not hex

    def test_format_preview_no_markup_chars(self):
        # Brackets in cmd must survive verbatim -- the preview block
        # is plain text rendered through _safe_log_write.
        out = _format_run_preview("abc123", "grep '[/▍]' file.txt", 600)
        assert "[/▍]" in out
        assert "abc123" in out
        assert "/yes abc123" in out


# ----------------------------------------------------------- dispatcher

class TestRunStaging:
    def test_run_with_pending_runs_stages_and_shows_preview(self, tmp_path):
        app = SimpleNamespace(run_auto_approve=False, pending_runs={})
        out, _ = tui.dispatch_slash(
            tui.parse_slash("/run echo loop266"),
            client=_client(), fs_cfg=_cfg(tmp_path), app=app,
        )
        assert "staged /run" in out
        assert "echo loop266" in out
        assert len(app.pending_runs) == 1

    def test_yes_executes_staged_run(self, tmp_path):
        app = SimpleNamespace(run_auto_approve=False, pending_runs={})
        tui.dispatch_slash(
            tui.parse_slash("/run echo loop266-yes"),
            client=_client(), fs_cfg=_cfg(tmp_path), app=app,
        )
        sid = next(iter(app.pending_runs))
        out, _ = tui.dispatch_slash(
            tui.parse_slash(f"/yes {sid}"),
            client=_client(), fs_cfg=_cfg(tmp_path), app=app,
        )
        assert "loop266-yes" in out
        assert "denied" not in out
        assert sid not in app.pending_runs

    def test_yes_with_no_arg_uses_latest(self, tmp_path):
        app = SimpleNamespace(run_auto_approve=False, pending_runs={})
        tui.dispatch_slash(
            tui.parse_slash("/run echo first266"),
            client=_client(), fs_cfg=_cfg(tmp_path), app=app,
        )
        time.sleep(0.001)
        tui.dispatch_slash(
            tui.parse_slash("/run echo second266"),
            client=_client(), fs_cfg=_cfg(tmp_path), app=app,
        )
        out, _ = tui.dispatch_slash(
            tui.parse_slash("/yes"),
            client=_client(), fs_cfg=_cfg(tmp_path), app=app,
        )
        assert "second266" in out
        assert "first266" not in out
        # First is still pending.
        assert len(app.pending_runs) == 1

    def test_no_cancels_staged_run(self, tmp_path):
        app = SimpleNamespace(run_auto_approve=False, pending_runs={})
        tui.dispatch_slash(
            tui.parse_slash("/run echo cancelme"),
            client=_client(), fs_cfg=_cfg(tmp_path), app=app,
        )
        sid = next(iter(app.pending_runs))
        out, _ = tui.dispatch_slash(
            tui.parse_slash(f"/no {sid}"),
            client=_client(), fs_cfg=_cfg(tmp_path), app=app,
        )
        assert "cancelled" in out
        assert "cancelme" in out
        assert sid not in app.pending_runs

    def test_yes_on_expired_says_so(self, tmp_path):
        app = SimpleNamespace(run_auto_approve=False, pending_runs={})
        # Manually backdate.
        app.pending_runs["abc"] = _StagedRun(
            stage_id="abc", cmd="echo old", created_at=time.time() - 99999
        )
        out, _ = tui.dispatch_slash(
            tui.parse_slash("/yes abc"),
            client=_client(), fs_cfg=_cfg(tmp_path), app=app,
        )
        assert "expired" in out
        assert "abc" not in app.pending_runs

    def test_yes_unknown_id_reports(self, tmp_path):
        app = SimpleNamespace(run_auto_approve=False, pending_runs={})
        # Need at least one entry for "missing" path (else "empty" returns).
        tui.dispatch_slash(
            tui.parse_slash("/run echo seed"),
            client=_client(), fs_cfg=_cfg(tmp_path), app=app,
        )
        out, _ = tui.dispatch_slash(
            tui.parse_slash("/yes deadbeef"),
            client=_client(), fs_cfg=_cfg(tmp_path), app=app,
        )
        assert "no staged run" in out
        assert "deadbeef" in out

    def test_yes_with_no_stages_says_so(self, tmp_path):
        app = SimpleNamespace(run_auto_approve=False, pending_runs={})
        out, _ = tui.dispatch_slash(
            tui.parse_slash("/yes"),
            client=_client(), fs_cfg=_cfg(tmp_path), app=app,
        )
        assert "no staged" in out

    def test_yes_without_pending_runs_attribute_says_unsupported(self, tmp_path):
        # Legacy app stub without pending_runs.
        app = SimpleNamespace(run_auto_approve=False)
        out, _ = tui.dispatch_slash(
            tui.parse_slash("/yes abc"),
            client=_client(), fs_cfg=_cfg(tmp_path), app=app,
        )
        assert "no /run staging" in out

    def test_inline_yes_still_bypasses_staging(self, tmp_path):
        app = SimpleNamespace(run_auto_approve=False, pending_runs={})
        out, _ = tui.dispatch_slash(
            tui.parse_slash("/run --yes echo loop266-bypass"),
            client=_client(), fs_cfg=_cfg(tmp_path), app=app,
        )
        assert "loop266-bypass" in out
        assert "staged" not in out
        assert app.pending_runs == {}

    def test_run_on_session_still_bypasses_staging(self, tmp_path):
        app = SimpleNamespace(run_auto_approve=True, pending_runs={})
        out, _ = tui.dispatch_slash(
            tui.parse_slash("/run echo loop266-session"),
            client=_client(), fs_cfg=_cfg(tmp_path), app=app,
        )
        assert "loop266-session" in out
        assert "staged" not in out
        assert app.pending_runs == {}

    def test_legacy_app_without_pending_runs_keeps_deny_path(self, tmp_path):
        # Backward-compat: existing test stubs use this exact shape.
        app = SimpleNamespace(run_auto_approve=False)
        out, _ = tui.dispatch_slash(
            tui.parse_slash("/run echo legacy"),
            client=_client(), fs_cfg=_cfg(tmp_path), app=app,
        )
        assert "denied" in out

    def test_bracket_heavy_cmd_safe_in_preview(self, tmp_path):
        app = SimpleNamespace(run_auto_approve=False, pending_runs={})
        out, _ = tui.dispatch_slash(
            tui.parse_slash("/run grep '[/▍]' file"),
            client=_client(), fs_cfg=_cfg(tmp_path), app=app,
        )
        assert "[/▍]" in out
        # Through Rich markup parsing the bracketed content must
        # survive the safe-write fallback.
        from rich.errors import MarkupError
        from rich.text import Text
        try:
            Text.from_markup(tui._safe_markup(out))
        except MarkupError:
            pytest.fail("preview text should round-trip through _safe_markup")


class TestSlashRegistration:
    def test_yes_no_registered(self):
        assert "/yes" in tui.SLASH_COMMANDS
        assert "/no" in tui.SLASH_COMMANDS

    def test_help_documents_two_phase(self):
        assert "two-phase" in tui.HELP_TEXT or "stage_id" in tui.HELP_TEXT
        assert "/yes" in tui.HELP_TEXT
        assert "/no" in tui.HELP_TEXT
