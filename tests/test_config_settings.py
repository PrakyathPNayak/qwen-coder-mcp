"""Tests for ``qwen_coder_mcp.config.load_settings`` and the new
``server_max_len`` setting added in loop 158.

These lock the contract between the client and the serve script:
``QWEN_MAX_TOKENS`` must default to a value that fits inside
``QWEN_SERVER_MAX_LEN`` so a fresh install does not immediately hit
``VLLMValidationError: max_tokens=... cannot be greater than
max_model_len=...``.
"""

from __future__ import annotations

import os

import pytest

from qwen_coder_mcp.config import Settings, load_settings


@pytest.fixture(autouse=True)
def _wipe_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith(("QWEN_", "LOOP_")):
            monkeypatch.delenv(key, raising=False)


class TestDefaultSettings:
    def test_max_tokens_default_fits_inside_server_max_len(self) -> None:
        s = load_settings(env_file="/nonexistent/.env")
        assert s.max_tokens <= s.server_max_len, (
            f"QWEN_MAX_TOKENS default ({s.max_tokens}) must be <= "
            f"QWEN_SERVER_MAX_LEN default ({s.server_max_len}) or vLLM "
            "will reject every request with VLLMValidationError"
        )

    def test_default_server_max_len_matches_serve_script(self) -> None:
        s = load_settings(env_file="/nonexistent/.env")
        # The serve script's default --max-model-len was bumped to 65536
        # in loop 171 so the 4090 actually uses the long-context support
        # baked into Qwen3.6-27B. Adjust both together if you change one.
        assert s.server_max_len == 65536

    def test_default_max_tokens_is_safe(self) -> None:
        s = load_settings(env_file="/nonexistent/.env")
        # Loop 236: 16384 leaves ~49k tokens of prompt headroom under
        # the 65536-token serve default. Bumped from 8192 because
        # Qwen3-Next emits long <think>...</think> blocks that were
        # cutting answers off mid-reasoning at the prior budget.
        assert s.max_tokens == 16384


class TestSettingsOverrides:
    def test_qwen_server_max_len_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("QWEN_SERVER_MAX_LEN", "8192")
        s = load_settings(env_file="/nonexistent/.env")
        assert s.server_max_len == 8192

    def test_falls_back_to_qwen_serve_max_len(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Users who only configured the server-side variable should not
        # have to repeat it for the client.
        monkeypatch.delenv("QWEN_SERVER_MAX_LEN", raising=False)
        monkeypatch.setenv("QWEN_SERVE_MAX_LEN", "4096")
        s = load_settings(env_file="/nonexistent/.env")
        assert s.server_max_len == 4096

    def test_explicit_overrides_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("QWEN_SERVER_MAX_LEN", "1024")
        monkeypatch.setenv("QWEN_SERVE_MAX_LEN", "8192")
        s = load_settings(env_file="/nonexistent/.env")
        assert s.server_max_len == 1024

    def test_max_tokens_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("QWEN_MAX_TOKENS", "512")
        s = load_settings(env_file="/nonexistent/.env")
        assert s.max_tokens == 512

    def test_settings_is_frozen(self) -> None:
        s = Settings(
            base_url="http://x/v1",
            api_key="k",
            model="m",
            timeout=1.0,
            max_tokens=10,
            server_max_len=2048,
            loop_interval_seconds=1,
            loop_max_file_bytes=1,
            loop_push=False,
        )
        with pytest.raises(Exception):
            s.max_tokens = 999  # type: ignore[misc]

    def test_api_key_hidden_from_repr(self) -> None:
        """Regression: ``Settings`` is frozen but its repr used to echo
        the API key, which leaked into local logs and uncaught
        traceback dumps. ``api_key`` is now ``repr=False`` so the
        secret stays accessible by attribute but is omitted from any
        accidental ``print(settings)``.
        """
        s = Settings(
            base_url="http://x/v1",
            api_key="super-secret-key",
            model="m",
            timeout=1.0,
            max_tokens=10,
            server_max_len=2048,
            loop_interval_seconds=1,
            loop_max_file_bytes=1,
            loop_push=False,
        )
        assert "super-secret-key" not in repr(s)
        # But the value is still readable for code that needs it.
        assert s.api_key == "super-secret-key"
