"""Loop 257: size-based rotation for ``.agent/runs.log``.

Without rotation, long-lived agent loops would grow the audit log
unbounded. Loop 257 adds an env-tunable size cap that, when exceeded,
moves the live file to ``runs.log.1`` (single-generation rotation,
overwriting any prior backup) before the next append.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from qwen_coder_mcp import fs_tools, tui


@pytest.fixture
def cfg(tmp_path: Path) -> fs_tools.FsConfig:
    return fs_tools.FsConfig(root=tmp_path)


def _records(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln]


def test_rotation_disabled_when_cap_zero(monkeypatch, cfg, tmp_path):
    monkeypatch.setenv("QWEN_RUNS_LOG_MAX_BYTES", "0")
    for i in range(5):
        tui._audit_run(cfg, cmd=f"echo {i}", approved=True, source="slash")
    p = tui._audit_run_path(cfg)
    assert p.exists()
    assert not p.with_name(p.name + ".1").exists()
    assert len(_records(p)) == 5


def test_rotation_fires_when_cap_exceeded(monkeypatch, cfg):
    # Tiny cap forces rotation after the first record.
    monkeypatch.setenv("QWEN_RUNS_LOG_MAX_BYTES", "10")
    tui._audit_run(cfg, cmd="echo first", approved=True, source="slash")
    tui._audit_run(cfg, cmd="echo second", approved=True, source="slash")
    p = tui._audit_run_path(cfg)
    backup = p.with_name(p.name + ".1")
    assert backup.exists()
    # The backup holds the first record; the live log holds the second.
    assert any("first" in r["cmd"] for r in _records(backup))
    assert any("second" in r["cmd"] for r in _records(p))


def test_rotation_overwrites_prior_backup(monkeypatch, cfg):
    monkeypatch.setenv("QWEN_RUNS_LOG_MAX_BYTES", "10")
    tui._audit_run(cfg, cmd="gen-1", approved=True, source="slash")
    tui._audit_run(cfg, cmd="gen-2", approved=True, source="slash")
    tui._audit_run(cfg, cmd="gen-3", approved=True, source="slash")
    p = tui._audit_run_path(cfg)
    backup = p.with_name(p.name + ".1")
    # Single-generation: the backup now holds gen-2, live holds gen-3.
    assert any("gen-2" in r["cmd"] for r in _records(backup))
    assert not any("gen-1" in r["cmd"] for r in _records(backup))
    assert any("gen-3" in r["cmd"] for r in _records(p))


def test_invalid_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("QWEN_RUNS_LOG_MAX_BYTES", "not-a-number")
    assert tui._audit_run_max_bytes() == 1024 * 1024


def test_negative_env_clamps_to_zero(monkeypatch):
    monkeypatch.setenv("QWEN_RUNS_LOG_MAX_BYTES", "-50")
    assert tui._audit_run_max_bytes() == 0


def test_default_cap_is_one_mib(monkeypatch):
    monkeypatch.delenv("QWEN_RUNS_LOG_MAX_BYTES", raising=False)
    assert tui._audit_run_max_bytes() == 1024 * 1024


def test_rotation_helper_no_op_if_log_missing(tmp_path):
    p = tmp_path / "missing.log"
    tui._maybe_rotate_runs_log(p, cap=10)
    assert not p.exists()
    assert not p.with_name(p.name + ".1").exists()


def test_rotation_helper_no_op_under_cap(tmp_path):
    p = tmp_path / "small.log"
    p.write_text("a" * 5)
    tui._maybe_rotate_runs_log(p, cap=100)
    assert p.exists()
    assert not p.with_name(p.name + ".1").exists()


def test_audit_failure_swallowed_after_rotation(monkeypatch, cfg, tmp_path):
    """If rotation succeeds but the subsequent open fails, the chat
    session must not crash."""
    monkeypatch.setenv("QWEN_RUNS_LOG_MAX_BYTES", "10")
    tui._audit_run(cfg, cmd="hello", approved=True, source="slash")

    real_open = Path.open

    def boom(self, *a, **k):
        if self.name == "runs.log":
            raise OSError("disk full")
        return real_open(self, *a, **k)

    monkeypatch.setattr(Path, "open", boom)
    # Should not raise.
    tui._audit_run(cfg, cmd="bye", approved=True, source="slash")
