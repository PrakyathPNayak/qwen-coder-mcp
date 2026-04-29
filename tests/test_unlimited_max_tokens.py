"""Loop 276 -- QWEN_NO_TOKEN_LIMIT uncaps max_tokens to context room."""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from qwen_coder_mcp.config import Settings, load_settings


def _settings(**kw):
    base = dict(
        base_url="http://x",
        api_key="EMPTY",
        model="m",
        timeout=10.0,
        max_tokens=4096,
        server_max_len=65536,
        loop_interval_seconds=45,
        loop_max_file_bytes=60000,
        loop_push=False,
    )
    base.update(kw)
    return Settings(**base)


class TestSettingsField:
    def test_default_off(self):
        s = _settings()
        assert s.unlimited_max_tokens is False

    def test_explicit_on(self):
        s = _settings(unlimited_max_tokens=True)
        assert s.unlimited_max_tokens is True


class TestEnvParse:
    def test_env_unset(self, monkeypatch, tmp_path):
        monkeypatch.delenv("QWEN_NO_TOKEN_LIMIT", raising=False)
        s = load_settings(env_file=tmp_path / "nonexistent.env")
        assert s.unlimited_max_tokens is False

    @pytest.mark.parametrize("val", ["1", "true", "True", "yes"])
    def test_env_truthy(self, monkeypatch, tmp_path, val):
        monkeypatch.setenv("QWEN_NO_TOKEN_LIMIT", val)
        s = load_settings(env_file=tmp_path / "nonexistent.env")
        assert s.unlimited_max_tokens is True

    @pytest.mark.parametrize("val", ["0", "false", "no", ""])
    def test_env_falsy(self, monkeypatch, tmp_path, val):
        monkeypatch.setenv("QWEN_NO_TOKEN_LIMIT", val)
        s = load_settings(env_file=tmp_path / "nonexistent.env")
        assert s.unlimited_max_tokens is False


class TestResolveMaxTokens:
    def _make_client(self, **kw):
        from qwen_coder_mcp.qwen_client import QwenClient

        return QwenClient(_settings(**kw))

    def test_clamped_when_off(self):
        c = self._make_client(unlimited_max_tokens=False, max_tokens=4096)
        # Tiny prompt, big room. Without unlimited, returned == budget.
        out = c._resolve_max_tokens([{"role": "user", "content": "hi"}], None)
        assert out == 4096

    def test_uncapped_when_on(self):
        c = self._make_client(unlimited_max_tokens=True, max_tokens=4096)
        out = c._resolve_max_tokens([{"role": "user", "content": "hi"}], None)
        # Should use ~all room (server_max_len 65536 - tiny prompt - reserve).
        assert out > 4096
        assert out <= 65536

    def test_explicit_request_still_honored(self):
        c = self._make_client(unlimited_max_tokens=True, max_tokens=4096)
        # Caller pinning budget=2048 should still get clamped to 2048,
        # not blown out to room. Unlimited only lifts the *default*.
        out = c._resolve_max_tokens([{"role": "user", "content": "hi"}], 2048)
        assert out == 2048
