"""Shared test helpers for QwenClient + httpx.MockTransport tests.

Single source of truth for the QwenClient/Settings/MockTransport wiring
recipe. Drift between this fixture and the real client constructor is
itself a bug class — centralising is what keeps the surface honest.
"""
from __future__ import annotations

from typing import Any, Callable

import httpx

from qwen_coder_mcp.config import Settings
from qwen_coder_mcp.qwen_client import QwenClient


_DEFAULT_SETTINGS: dict[str, Any] = dict(
    base_url="http://x/v1",
    api_key="k",
    model="qwen",
    timeout=10.0,
    max_tokens=128,
    server_max_len=2048,
    loop_interval_seconds=60,
    loop_max_file_bytes=200_000,
    loop_push=False,
)


def make_mock_qwen_client(
    handler: Callable[[httpx.Request], httpx.Response],
    **settings_overrides: Any,
) -> QwenClient:
    """Return a QwenClient whose underlying httpx.Client uses MockTransport.

    Optional ``settings_overrides`` override individual ``Settings``
    fields. The Authorization and Content-Type headers are set so
    request handlers can assert on them.
    """
    cfg = dict(_DEFAULT_SETTINGS)
    cfg.update(settings_overrides)
    settings = Settings(**cfg)
    client = QwenClient(settings)
    client._client.close()
    client._client = httpx.Client(
        base_url=settings.base_url,
        transport=httpx.MockTransport(handler),
        timeout=settings.timeout,
        headers={
            "Authorization": f"Bearer {settings.api_key}",
            "Content-Type": "application/json",
        },
    )
    return client
