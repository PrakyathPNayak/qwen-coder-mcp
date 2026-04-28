"""Tests for the loop-251 /run audit log + /runs viewer.

Every /run attempt — approved-and-executed, denied-by-gate, or even
confirm-hook-raises — appends one JSONL record to
``<workspace>/.agent/runs.log``. The /runs slash command tails it.

Pinned behaviors:
  * Audit append is best-effort (IO failure can't break the chat).
  * Records contain ts, cmd, approved, source; returncode when ran.
  * Denied attempts are recorded with approved=False (no returncode).
  * Confirm-hook exceptions count as denied + recorded.
  * /runs default tail length is 10; numeric arg overrides up to 1000.
  * /runs --json emits raw JSONL lines (no human formatting).
  * /runs on missing log file returns a friendly "no records" string.
  * audit_source=None (back-compat) writes nothing.
  * Discoverability: HELP_TEXT mentions /runs; completion lists it.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from qwen_coder_mcp import fs_tools, tui


def _read_audit(tmp_path: Path) -> list[dict]:
    p = tmp_path / ".agent" / "runs.log"
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]


class TestAuditAppend:
    def _cfg(self, tmp_path):
        return fs_tools.FsConfig(root=tmp_path)

    def test_executed_command_records_returncode(self, tmp_path):
        tui._render_run(
            self._cfg(tmp_path),
            "echo loop251-x",
            confirm=lambda _c: True,
            audit_source="slash",
        )
        recs = _read_audit(tmp_path)
        assert len(recs) == 1
        assert recs[0]["approved"] is True
        assert recs[0]["source"] == "slash"
        assert recs[0]["cmd"] == "echo loop251-x"
        assert "returncode" in recs[0]

    def test_denied_command_records_no_returncode(self, tmp_path):
        tui._render_run(
            self._cfg(tmp_path),
            "echo nope",
            confirm=lambda _c: False,
            audit_source="slash",
        )
        recs = _read_audit(tmp_path)
        assert len(recs) == 1
        assert recs[0]["approved"] is False
        assert "returncode" not in recs[0]

    def test_confirm_raises_recorded_as_denied(self, tmp_path):
        def boom(_c):
            raise RuntimeError("x")
        tui._render_run(
            self._cfg(tmp_path),
            "echo y",
            confirm=boom,
            audit_source="slash",
        )
        recs = _read_audit(tmp_path)
        assert len(recs) == 1
        assert recs[0]["approved"] is False

    def test_audit_source_none_writes_nothing(self, tmp_path):
        tui._render_run(self._cfg(tmp_path), "echo z")
        assert not (tmp_path / ".agent" / "runs.log").exists()

    def test_audit_io_failure_does_not_crash(self, tmp_path, monkeypatch):
        # Make .agent a regular file so .agent/runs.log can't be written.
        agent_path = tmp_path / ".agent"
        agent_path.write_text("not a dir")
        out = tui._render_run(
            self._cfg(tmp_path),
            "echo no-crash",
            confirm=lambda _c: True,
            audit_source="slash",
        )
        # Command still executed and rendered fine; audit silently failed.
        assert "no-crash" in out

    def test_multiple_appends_accumulate(self, tmp_path):
        cfg = self._cfg(tmp_path)
        for i in range(3):
            tui._render_run(
                cfg, f"echo a{i}", confirm=lambda _c: True, audit_source="slash"
            )
        assert len(_read_audit(tmp_path)) == 3


class TestRunsViewer:
    def _cfg(self, tmp_path):
        return fs_tools.FsConfig(root=tmp_path)

    def _seed(self, tmp_path, n: int) -> None:
        cfg = self._cfg(tmp_path)
        for i in range(n):
            tui._render_run(
                cfg, f"echo seed{i}", confirm=lambda _c: True, audit_source="slash"
            )

    def test_no_log_file(self, tmp_path):
        out = tui._render_runs_audit(self._cfg(tmp_path), [])
        assert "no /run audit" in out

    def test_default_tail_human_format(self, tmp_path):
        self._seed(tmp_path, 3)
        out = tui._render_runs_audit(self._cfg(tmp_path), [])
        for i in range(3):
            assert f"echo seed{i}" in out
        assert "OK" in out  # approved marker

    def test_numeric_arg_limits_output(self, tmp_path):
        self._seed(tmp_path, 15)
        out = tui._render_runs_audit(self._cfg(tmp_path), ["5"])
        assert "echo seed14" in out
        assert "echo seed10" in out
        assert "echo seed9" not in out

    def test_default_caps_at_ten(self, tmp_path):
        self._seed(tmp_path, 15)
        out = tui._render_runs_audit(self._cfg(tmp_path), [])
        assert "echo seed14" in out
        assert "echo seed4" not in out  # only 10 shown

    def test_json_mode_outputs_raw_jsonl(self, tmp_path):
        self._seed(tmp_path, 2)
        out = tui._render_runs_audit(self._cfg(tmp_path), ["--json"])
        for line in out.splitlines():
            obj = json.loads(line)
            assert "cmd" in obj
            assert "approved" in obj

    def test_denied_records_show_DEN_marker(self, tmp_path):
        cfg = self._cfg(tmp_path)
        tui._render_run(
            cfg, "echo denied-one", confirm=lambda _c: False, audit_source="slash"
        )
        out = tui._render_runs_audit(cfg, [])
        assert "DEN" in out
        assert "denied-one" in out


class TestDispatcherIntegration:
    def _cfg(self, tmp_path):
        return fs_tools.FsConfig(root=tmp_path)

    def test_slash_run_writes_audit_record(self, tmp_path):
        cmd = tui.parse_slash("/run --yes echo loop251-disp")
        tui.dispatch_slash(
            cmd,
            client=SimpleNamespace(settings=None),
            fs_cfg=self._cfg(tmp_path),
        )
        recs = _read_audit(tmp_path)
        assert len(recs) == 1
        assert recs[0]["source"] == "slash"
        assert recs[0]["approved"] is True

    def test_slash_run_denied_writes_record(self, tmp_path):
        cmd = tui.parse_slash("/run echo blocked-251")
        tui.dispatch_slash(
            cmd,
            client=SimpleNamespace(settings=None),
            fs_cfg=self._cfg(tmp_path),
        )
        recs = _read_audit(tmp_path)
        assert len(recs) == 1
        assert recs[0]["approved"] is False
        assert recs[0]["cmd"] == "echo blocked-251"

    def test_slash_runs_command_tails_audit(self, tmp_path):
        cfg = self._cfg(tmp_path)
        # seed via dispatcher so source="slash" naturally
        tui.dispatch_slash(
            tui.parse_slash("/run --yes echo seed-disp"),
            client=SimpleNamespace(settings=None),
            fs_cfg=cfg,
        )
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/runs"),
            client=SimpleNamespace(settings=None),
            fs_cfg=cfg,
        )
        assert "seed-disp" in text


class TestDiscoverability:
    def test_help_documents_runs(self):
        assert "/runs" in tui.HELP_TEXT

    def test_completion_lists_runs(self):
        comps = tui.slash_completions("/run")
        assert "/runs" in comps


class TestRotatedLogIncluded:
    """Loop 259: /runs viewer also reads from rotated runs.log.1 so
    history isn't lost the moment loop-257 rotation fires."""

    def _cfg(self, tmp_path):
        return fs_tools.FsConfig(root=tmp_path)

    def test_only_rotated_log_present(self, tmp_path):
        cfg = self._cfg(tmp_path)
        path = tui._audit_run_path(cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        rec = '{"ts": 1.0, "cmd": "echo from-rotated", "approved": true, "source": "slash"}'
        path.with_name("runs.log.1").write_text(rec + "\n", encoding="utf-8")
        out = tui._render_runs_audit(cfg, [])
        assert "from-rotated" in out

    def test_rotated_and_live_concatenated(self, tmp_path):
        cfg = self._cfg(tmp_path)
        path = tui._audit_run_path(cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        old = '{"ts": 1.0, "cmd": "echo OLD", "approved": true, "source": "slash"}'
        new = '{"ts": 2.0, "cmd": "echo NEW", "approved": true, "source": "slash"}'
        path.with_name("runs.log.1").write_text(old + "\n", encoding="utf-8")
        path.write_text(new + "\n", encoding="utf-8")
        out = tui._render_runs_audit(cfg, [])
        assert "OLD" in out and "NEW" in out
        # Rotated lines come before live lines (chronological).
        assert out.index("OLD") < out.index("NEW")

    def test_tail_n_spans_rotation_boundary(self, tmp_path):
        cfg = self._cfg(tmp_path)
        path = tui._audit_run_path(cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        rotated_recs = "\n".join(
            f'{{"ts": {i}, "cmd": "rot{i}", "approved": true, "source": "s"}}'
            for i in range(8)
        )
        live_recs = "\n".join(
            f'{{"ts": {10 + i}, "cmd": "live{i}", "approved": true, "source": "s"}}'
            for i in range(4)
        )
        path.with_name("runs.log.1").write_text(rotated_recs + "\n", encoding="utf-8")
        path.write_text(live_recs + "\n", encoding="utf-8")
        out = tui._render_runs_audit(cfg, ["10"])
        # 10 most recent of 12 = rot6,rot7,live0..live3 + 4 from rotated
        assert "live3" in out
        assert "rot6" in out
        assert "rot7" in out
        assert "rot1" not in out

