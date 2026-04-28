"""Loop 200 — `/sysinfo --json` emits the snapshot as JSON."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from qwen_coder_mcp import fs_tools, tui
from qwen_coder_mcp.config import Settings
from qwen_coder_mcp.tui import ChatMessage


def _settings() -> Settings:
    return Settings(
        base_url="http://localhost:8000/v1",
        api_key="k",
        model="qwen-foo",
        timeout=5.0,
        max_tokens=10,
        server_max_len=2048,
        loop_interval_seconds=1,
        loop_max_file_bytes=1000,
        loop_push=False,
    )


class _OkClient:
    settings = _settings()

    def health_check(self):
        return {"ok": True, "models": ["qwen-foo", "qwen-bar"]}


class _DownClient:
    settings = _settings()

    def health_check(self):
        return {"ok": False, "error": "connection refused", "hint": "start vllm"}


class _RaisingClient:
    settings = _settings()

    def health_check(self):
        raise RuntimeError("boom")


@pytest.fixture()
def fs_cfg(tmp_path: Path) -> fs_tools.FsConfig:
    return fs_tools.FsConfig(root=tmp_path)


class TestSysinfoJson:
    def test_healthy_parses_as_json(self, fs_cfg: fs_tools.FsConfig) -> None:
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/sysinfo --json"),
            client=_OkClient(),
            fs_cfg=fs_cfg,
            history=[ChatMessage(role="user", content="hi there")],
        )
        data = json.loads(text)
        assert data["model"] == "qwen-foo"
        assert data["base_url"] == "http://localhost:8000/v1"
        assert data["fs_root"] == str(fs_cfg.root)
        assert data["history"]["messages"] == 1
        assert data["history"]["tokens_estimated"] > 0
        assert data["health"]["ok"] is True
        assert data["health"]["models"] == ["qwen-foo", "qwen-bar"]

    def test_format_json_alias(self, fs_cfg: fs_tools.FsConfig) -> None:
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/sysinfo --format=json"),
            client=_OkClient(),
            fs_cfg=fs_cfg,
            history=[],
        )
        json.loads(text)  # parses without raising

    def test_unhealthy_in_json(self, fs_cfg: fs_tools.FsConfig) -> None:
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/sysinfo --json"),
            client=_DownClient(),
            fs_cfg=fs_cfg,
            history=[],
        )
        data = json.loads(text)
        assert data["health"]["ok"] is False
        assert data["health"]["error"] == "connection refused"
        assert data["health"]["hint"] == "start vllm"

    def test_health_raises_caught(self, fs_cfg: fs_tools.FsConfig) -> None:
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/sysinfo --json"),
            client=_RaisingClient(),
            fs_cfg=fs_cfg,
            history=[],
        )
        data = json.loads(text)
        assert data["health"]["ok"] is False
        assert "boom" in data["health"]["error"]
        assert "RuntimeError" in data["health"]["error"]

    def test_no_json_flag_keeps_text(self, fs_cfg: fs_tools.FsConfig) -> None:
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/sysinfo"),
            client=_OkClient(),
            fs_cfg=fs_cfg,
            history=[],
        )
        with pytest.raises(json.JSONDecodeError):
            json.loads(text)
        # And the human-readable header is still there.
        assert "qwen-coder-tui sysinfo" in text

    def test_history_zero_when_none(self, fs_cfg: fs_tools.FsConfig) -> None:
        text, _ = tui.dispatch_slash(
            tui.parse_slash("/sysinfo --json"),
            client=_OkClient(),
            fs_cfg=fs_cfg,
            history=None,
        )
        data = json.loads(text)
        assert data["history"]["messages"] == 0
        assert data["history"]["tokens_estimated"] == 0
