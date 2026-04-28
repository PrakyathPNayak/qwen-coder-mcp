"""Tests for ``resolve_rotation_keep`` (loop 184) — env-var-driven
override of the agent-checkpoint rotation cap. Pure helper, so the
tests pass an isolated dict instead of touching ``os.environ``."""
from __future__ import annotations

from qwen_coder_mcp.tui import (
    DEFAULT_ROTATION_KEEP,
    resolve_rotation_keep,
)


class TestResolveRotationKeep:
    def test_unset_returns_default(self) -> None:
        assert resolve_rotation_keep({}) == DEFAULT_ROTATION_KEEP

    def test_default_is_five(self) -> None:
        # Pin the documented default so changing it without intent
        # trips a test.
        assert DEFAULT_ROTATION_KEEP == 5

    def test_empty_string_returns_default(self) -> None:
        assert resolve_rotation_keep({"QWEN_AGENT_ROTATION_KEEP": ""}) == DEFAULT_ROTATION_KEEP

    def test_whitespace_only_returns_default(self) -> None:
        assert resolve_rotation_keep({"QWEN_AGENT_ROTATION_KEEP": "   "}) == DEFAULT_ROTATION_KEEP

    def test_valid_positive_int(self) -> None:
        assert resolve_rotation_keep({"QWEN_AGENT_ROTATION_KEEP": "20"}) == 20

    def test_zero_means_retain_all(self) -> None:
        # 0 is a meaningful sentinel for rotate_agent_checkpoints —
        # "keep everything". Don't fall back to the default for it.
        assert resolve_rotation_keep({"QWEN_AGENT_ROTATION_KEEP": "0"}) == 0

    def test_negative_clamped_to_zero(self) -> None:
        assert resolve_rotation_keep({"QWEN_AGENT_ROTATION_KEEP": "-3"}) == 0

    def test_unparseable_returns_default(self) -> None:
        assert resolve_rotation_keep({"QWEN_AGENT_ROTATION_KEEP": "many"}) == DEFAULT_ROTATION_KEEP

    def test_float_unparseable_returns_default(self) -> None:
        # int('5.5') raises -> default.
        assert resolve_rotation_keep({"QWEN_AGENT_ROTATION_KEEP": "5.5"}) == DEFAULT_ROTATION_KEEP

    def test_reads_from_os_environ_when_no_arg(
        self, monkeypatch: object
    ) -> None:
        # Default arg path: env=None reads os.environ directly.
        import os
        old = os.environ.pop("QWEN_AGENT_ROTATION_KEEP", None)
        try:
            os.environ["QWEN_AGENT_ROTATION_KEEP"] = "12"
            assert resolve_rotation_keep() == 12
        finally:
            os.environ.pop("QWEN_AGENT_ROTATION_KEEP", None)
            if old is not None:
                os.environ["QWEN_AGENT_ROTATION_KEEP"] = old
