"""Make the repo root importable so `import agent.loop` works in tests."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@pytest.fixture(autouse=True)
def _reset_swallow_loggers():
    """Reset every registered rate-limited swallow logger before each test.

    Without this, a test that triggers a logger (e.g., `_GIT_LOCAL_SWALLOW_LOG`
    via `_commit_and_push` failure) leaves `count > 0` behind, which contaminates
    later tests that assert exact counts. Loop 94 centralises the scattered
    per-test `try/finally` reset pattern.

    Imported lazily so importing conftest never fails if `agent.loop` has a
    transient import-time issue (e.g., during a partial commit).
    """
    try:
        from agent import loop as L
        for lg in L._swallow_loggers():
            lg.reset()
        L._LAST_SWALLOW_SUMMARY_COUNTS.clear()
    except Exception:
        pass
    yield
    try:
        from agent import loop as L
        for lg in L._swallow_loggers():
            lg.reset()
        L._LAST_SWALLOW_SUMMARY_COUNTS.clear()
    except Exception:
        pass


@pytest.fixture
def make_qwen_client():
    """Factory fixture wrapping `tests._helpers.make_mock_qwen_client`.

    Provided as a fixture so new tests can request `make_qwen_client`
    by name; existing tests import the helper directly.
    """
    from tests._helpers import make_mock_qwen_client
    return make_mock_qwen_client
