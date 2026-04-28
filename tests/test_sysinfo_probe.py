"""Tests for /sysinfo --probe and QwenClient.vllm_health_probe.

Loop 215: closes the loops 205 / 211 reactive-detection gap. Argv-level
validation only proves "vLLM accepted the flags". --help validation only
proves "the flag names exist". The active engine-readiness signal lives
at vLLM's /health endpoint (server root, not /v1). The TUI now exposes
this via /sysinfo --probe so operators can distinguish "args OK" from
"engine actually ready".
"""
from __future__ import annotations

import json

import httpx
import pytest

from qwen_coder_mcp import fs_tools, tui

from tests._helpers import make_mock_qwen_client


class TestVllmHealthProbe:
    def test_health_url_strips_v1_suffix(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, content=b"")

        client = make_mock_qwen_client(handler, base_url="http://x:8000/v1")
        # The mock transport intercepts traffic at the *client* level; for
        # vllm_health_probe we use a top-level httpx.get against the
        # reconstructed root URL. Patch httpx.get directly to capture.
        import qwen_coder_mcp.qwen_client as qc

        calls: list[str] = []

        def fake_get(url, **kw):
            calls.append(url)
            return httpx.Response(200, content=b"")

        old = qc.httpx.get
        qc.httpx.get = fake_get
        try:
            result = client.vllm_health_probe()
        finally:
            qc.httpx.get = old
        assert calls == ["http://x:8000/health"]
        assert result == {"ok": True, "status": 200}

    def test_health_url_when_no_v1_suffix(self) -> None:
        client = make_mock_qwen_client(
            lambda r: httpx.Response(200), base_url="http://x:8000"
        )
        import qwen_coder_mcp.qwen_client as qc

        calls: list[str] = []

        def fake_get(url, **kw):
            calls.append(url)
            return httpx.Response(200, content=b"")

        old = qc.httpx.get
        qc.httpx.get = fake_get
        try:
            client.vllm_health_probe()
        finally:
            qc.httpx.get = old
        assert calls == ["http://x:8000/health"]

    def test_503_returns_warming_up_hint(self) -> None:
        client = make_mock_qwen_client(lambda r: httpx.Response(200))
        import qwen_coder_mcp.qwen_client as qc

        old = qc.httpx.get
        qc.httpx.get = lambda url, **kw: httpx.Response(503, content=b"")
        try:
            result = client.vllm_health_probe()
        finally:
            qc.httpx.get = old
        assert result["ok"] is False
        assert "engine not ready" in result["error"].lower()
        assert "still initialising" in (result.get("hint") or "")

    def test_connect_error_returns_serve_hint(self) -> None:
        client = make_mock_qwen_client(lambda r: httpx.Response(200))
        import qwen_coder_mcp.qwen_client as qc

        def boom(url, **kw):
            raise httpx.ConnectError("nope")

        old = qc.httpx.get
        qc.httpx.get = boom
        try:
            result = client.vllm_health_probe()
        finally:
            qc.httpx.get = old
        assert result["ok"] is False
        assert "serve_qwen.sh" in (result.get("hint") or "")

    def test_timeout_returns_warming_hint(self) -> None:
        client = make_mock_qwen_client(lambda r: httpx.Response(200))
        import qwen_coder_mcp.qwen_client as qc

        def slow(url, **kw):
            raise httpx.ReadTimeout("slow")

        old = qc.httpx.get
        qc.httpx.get = slow
        try:
            result = client.vllm_health_probe()
        finally:
            qc.httpx.get = old
        assert result["ok"] is False
        assert "model load" in (result.get("hint") or "")

    def test_authorization_header_sent(self) -> None:
        client = make_mock_qwen_client(
            lambda r: httpx.Response(200), api_key="secret-token"
        )
        import qwen_coder_mcp.qwen_client as qc

        captured: dict = {}

        def fake_get(url, **kw):
            captured["headers"] = kw.get("headers") or {}
            return httpx.Response(200, content=b"")

        old = qc.httpx.get
        qc.httpx.get = fake_get
        try:
            client.vllm_health_probe()
        finally:
            qc.httpx.get = old
        assert captured["headers"].get("Authorization") == "Bearer secret-token"


class TestSysinfoProbe:
    def _stub_probe(self, monkeypatch, payload):
        import qwen_coder_mcp.qwen_client as qc

        monkeypatch.setattr(
            qc.QwenClient, "vllm_health_probe", lambda self, timeout=2.0: payload
        )

    def test_text_sysinfo_without_probe_omits_engine_line(
        self, tmp_path, monkeypatch
    ) -> None:
        client = make_mock_qwen_client(
            lambda r: httpx.Response(
                200, content=json.dumps({"data": [{"id": "qwen"}]}).encode()
            )
        )
        cfg = fs_tools.FsConfig(root=tmp_path)
        out = tui._render_sysinfo(client, cfg, history=[])
        assert "engine:" not in out
        assert "backend ok" in out

    def test_text_sysinfo_with_probe_adds_engine_ready_line(
        self, tmp_path, monkeypatch
    ) -> None:
        client = make_mock_qwen_client(
            lambda r: httpx.Response(
                200, content=json.dumps({"data": [{"id": "qwen"}]}).encode()
            )
        )
        self._stub_probe(monkeypatch, {"ok": True, "status": 200})
        cfg = fs_tools.FsConfig(root=tmp_path)
        out = tui._render_sysinfo(client, cfg, history=[], probe=True)
        assert "engine ready" in out
        assert "vLLM /health 200" in out

    def test_text_sysinfo_with_probe_503_surfaces_hint(
        self, tmp_path, monkeypatch
    ) -> None:
        client = make_mock_qwen_client(
            lambda r: httpx.Response(
                200, content=json.dumps({"data": [{"id": "qwen"}]}).encode()
            )
        )
        self._stub_probe(
            monkeypatch,
            {
                "ok": False,
                "error": "engine not ready (503) at http://x/health",
                "hint": "still initialising; tail .loop/serve.log",
            },
        )
        cfg = fs_tools.FsConfig(root=tmp_path)
        out = tui._render_sysinfo(client, cfg, history=[], probe=True)
        assert "engine not ready" in out
        assert "still initialising" in out

    def test_json_sysinfo_with_probe_includes_engine_health(
        self, tmp_path, monkeypatch
    ) -> None:
        client = make_mock_qwen_client(
            lambda r: httpx.Response(
                200, content=json.dumps({"data": [{"id": "qwen"}]}).encode()
            )
        )
        self._stub_probe(monkeypatch, {"ok": True, "status": 200})
        cfg = fs_tools.FsConfig(root=tmp_path)
        out = tui._render_sysinfo_json(client, cfg, history=[], probe=True)
        payload = json.loads(out)
        assert payload["engine_health"] == {"ok": True, "status": 200}
        assert payload["health"]["ok"] is True

    def test_json_sysinfo_without_probe_has_no_engine_health(
        self, tmp_path
    ) -> None:
        client = make_mock_qwen_client(
            lambda r: httpx.Response(
                200, content=json.dumps({"data": [{"id": "qwen"}]}).encode()
            )
        )
        cfg = fs_tools.FsConfig(root=tmp_path)
        out = tui._render_sysinfo_json(client, cfg, history=[])
        payload = json.loads(out)
        assert "engine_health" not in payload


class TestSysinfoSlashDispatch:
    def test_dispatcher_routes_probe_flag(self, tmp_path, monkeypatch) -> None:
        client = make_mock_qwen_client(
            lambda r: httpx.Response(
                200, content=json.dumps({"data": [{"id": "qwen"}]}).encode()
            )
        )
        import qwen_coder_mcp.qwen_client as qc

        probe_calls: list[bool] = []

        def fake_probe(self, timeout=2.0):
            probe_calls.append(True)
            return {"ok": True, "status": 200}

        monkeypatch.setattr(qc.QwenClient, "vllm_health_probe", fake_probe)
        cfg = fs_tools.FsConfig(root=tmp_path)
        out, was_chat = tui.dispatch_slash(
            tui.parse_slash("/sysinfo --probe"),
            client=client,
            fs_cfg=cfg,
            history=[],
        )
        assert was_chat is False
        assert probe_calls == [True]
        assert "engine ready" in out

    def test_dispatcher_routes_json_probe_combo(
        self, tmp_path, monkeypatch
    ) -> None:
        client = make_mock_qwen_client(
            lambda r: httpx.Response(
                200, content=json.dumps({"data": [{"id": "qwen"}]}).encode()
            )
        )
        import qwen_coder_mcp.qwen_client as qc

        monkeypatch.setattr(
            qc.QwenClient,
            "vllm_health_probe",
            lambda self, timeout=2.0: {"ok": True, "status": 200},
        )
        cfg = fs_tools.FsConfig(root=tmp_path)
        out, _ = tui.dispatch_slash(
            tui.parse_slash("/sysinfo --json --probe"),
            client=client,
            fs_cfg=cfg,
            history=[],
        )
        payload = json.loads(out)
        assert payload["engine_health"]["ok"] is True

    def test_dispatcher_no_probe_does_not_call_probe(
        self, tmp_path, monkeypatch
    ) -> None:
        client = make_mock_qwen_client(
            lambda r: httpx.Response(
                200, content=json.dumps({"data": [{"id": "qwen"}]}).encode()
            )
        )
        import qwen_coder_mcp.qwen_client as qc

        called: list[bool] = []
        monkeypatch.setattr(
            qc.QwenClient,
            "vllm_health_probe",
            lambda self, timeout=2.0: (called.append(True), {"ok": True})[1],
        )
        cfg = fs_tools.FsConfig(root=tmp_path)
        tui.dispatch_slash(
            tui.parse_slash("/sysinfo"),
            client=client,
            fs_cfg=cfg,
            history=[],
        )
        assert called == [], "probe must not run unless --probe is passed"
