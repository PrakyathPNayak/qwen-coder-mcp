"""Loop 261: pytest must not pollute the real `.loop/runtime.log`.

The autouse `_isolate_loop_runtime_log` fixture in conftest.py points
`agent.loop.LOG_FILE` at a per-test tmp path so calls to `_log` made
during fixture setup or test bodies don't leak into the operator's
view of `/loop tail`.
"""
from __future__ import annotations

from pathlib import Path

import pytest


def test_log_file_redirected_to_tmp(tmp_path):
    from agent import loop as L
    # The autouse fixture redirected LOG_FILE before the test body ran.
    assert "looplog" in str(L.LOG_FILE)
    # And it lives under pytest's tmp tree, not the repo's `.loop/`.
    assert ".loop/runtime.log" not in str(L.LOG_FILE).replace("\\", "/")


def test_log_writes_go_to_tmp_only(tmp_path):
    from agent import loop as L
    repo_log = Path(__file__).resolve().parents[1] / ".loop" / "runtime.log"
    before = repo_log.exists()
    L._log("loop-261-isolation-canary")
    after = repo_log.exists()
    # If repo_log didn't exist before, it must not exist after the call.
    if not before:
        assert not after, (
            f"agent.loop._log leaked into repo .loop/runtime.log "
            f"despite the autouse isolation fixture: {repo_log}"
        )
    # And the tmp redirect target must contain the canary.
    text = L.LOG_FILE.read_text(encoding="utf-8")
    assert "loop-261-isolation-canary" in text


def test_timing_file_also_redirected():
    from agent import loop as L
    assert "looplog" in str(L.TIMING_FILE)
