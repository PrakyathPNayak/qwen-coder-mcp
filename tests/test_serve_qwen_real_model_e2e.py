"""Heavy real-model E2E: launch the actual Qwen3.6-27B (or whatever
``QWEN_SERVE_MODEL`` defaults to) through ``serve_qwen.sh``, verify
the engine reaches readiness, send a real chat completion, and pin
that the live response shape matches what the agent loop expects.

This is the lights-on validator that loops 205, 211, and 216 each
needed in advance. The opt-125m E2E in ``test_serve_qwen_engine_init``
covers the dense-model offloading code path; this test covers the
hybrid-model path that the production default model lives on.

Gated behind ``QWEN_SERVE_E2E_REAL_MODEL=1`` (separate from the opt
gate) because:

- Needs ~17 GiB of free VRAM
- First run downloads ~13 GiB of int4 weights
- Engine init is ~45-90s even with weights cached
- Server runs for the duration of the test (~1-2 minutes)

CI/dev runs skip cleanly. Operators opt in with::

    QWEN_SERVE_E2E_REAL_MODEL=1 \\
      pytest tests/test_serve_qwen_real_model_e2e.py -v -s

If both ``QWEN_SERVE_REUSE_RUNNING=1`` is set AND the launcher is
already running on the configured port, we reuse it instead of
starting a new server. This lets operators keep a long-lived server
and rerun the test cheaply.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "serve_qwen.sh"
VLLM_VENV = REPO_ROOT / ".venv-serve" / "bin" / "vllm"
PID_FILE = REPO_ROOT / ".loop" / "serve_qwen.pid"


_GATE = os.environ.get("QWEN_SERVE_E2E_REAL_MODEL", "").lower() in {
    "1",
    "true",
    "yes",
}

pytestmark = pytest.mark.skipif(
    not _GATE,
    reason=(
        "set QWEN_SERVE_E2E_REAL_MODEL=1 to run heavy real-model E2E "
        "(needs ~17GiB VRAM, weights download, ~90s startup)"
    ),
)


def _has_gpu() -> bool:
    return shutil.which("nvidia-smi") is not None


def _port_open(port: int) -> bool:
    s = socket.socket()
    try:
        s.settimeout(0.5)
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _http_get_json(url: str, *, key: str = "EMPTY", timeout: float = 10.0) -> dict:
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {key}"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        assert resp.status == 200, f"{url} -> {resp.status}"
        return json.loads(resp.read().decode("utf-8"))


def _http_post_json(
    url: str,
    payload: dict,
    *,
    key: str = "EMPTY",
    timeout: float = 120.0,
) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        assert resp.status == 200, f"{url} -> {resp.status}"
        return json.loads(resp.read().decode("utf-8"))


@pytest.fixture(scope="module")
def live_server() -> dict:
    if not _has_gpu():
        pytest.skip("no nvidia-smi found")
    if not VLLM_VENV.exists():
        pytest.skip("no .venv-serve/bin/vllm")

    port = int(os.environ.get("QWEN_SERVE_PORT", "8000"))
    base_url = f"http://127.0.0.1:{port}"
    api_key = os.environ.get("QWEN_SERVE_API_KEY", "EMPTY")

    started_here = False
    if _port_open(port) and os.environ.get(
        "QWEN_SERVE_REUSE_RUNNING", ""
    ).lower() in {"1", "true", "yes"}:
        # Already running; just verify health.
        pass
    else:
        if _port_open(port):
            pytest.skip(
                f"port {port} already in use and QWEN_SERVE_REUSE_RUNNING "
                "not set; refusing to clobber"
            )
        # Launch via the production script with its real defaults.
        log_path = REPO_ROOT / ".loop" / "serve_e2e_real.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if log_path.exists():
            log_path.unlink()
        env = dict(os.environ)
        env["QWEN_SERVE_LOG"] = str(log_path)
        env["QWEN_SERVE_DRY_RUN"] = "0"
        proc = subprocess.run(
            ["bash", str(SCRIPT)],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, f"launcher failed: {proc.stderr}"
        started_here = True

    # Poll /health (server root) for up to 240s -- first-time download
    # plus engine-init can be slow.
    deadline = time.monotonic() + 240.0
    last_err: str | None = None
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(
                f"{base_url}/health",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                if resp.status == 200:
                    break
        except (urllib.error.URLError, OSError) as exc:
            last_err = str(exc)
        time.sleep(3.0)
    else:
        if started_here and PID_FILE.exists():
            try:
                os.kill(int(PID_FILE.read_text().strip()), signal.SIGTERM)
            except (ValueError, ProcessLookupError):
                pass
        pytest.fail(f"engine never became healthy: {last_err}")

    yield {"base_url": base_url, "api_key": api_key}

    if started_here and PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            time.sleep(2.0)
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        except (ValueError, ProcessLookupError):
            pass


def test_v1_models_endpoint_returns_loaded_model(live_server) -> None:
    data = _http_get_json(
        f"{live_server['base_url']}/v1/models",
        key=live_server["api_key"],
    )
    ids = [m["id"] for m in data["data"]]
    assert ids, f"/v1/models returned empty list: {data}"


def test_health_endpoint_at_server_root(live_server) -> None:
    # Verifies the loop-215 assumption: /health lives at server root,
    # not under /v1.
    req = urllib.request.Request(
        f"{live_server['base_url']}/health",
        headers={"Authorization": f"Bearer {live_server['api_key']}"},
    )
    with urllib.request.urlopen(req, timeout=5.0) as resp:
        assert resp.status == 200


def test_chat_completion_returns_text(live_server) -> None:
    models = _http_get_json(
        f"{live_server['base_url']}/v1/models",
        key=live_server["api_key"],
    )
    model_id = models["data"][0]["id"]
    out = _http_post_json(
        f"{live_server['base_url']}/v1/chat/completions",
        {
            "model": model_id,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Reply with only the literal token PINGOK and "
                        "nothing else."
                    ),
                }
            ],
            "max_tokens": 96,
            "temperature": 0,
        },
        key=live_server["api_key"],
    )
    content = out["choices"][0]["message"].get("content") or ""
    assert content, f"empty content: {out}"
    # Even with thinking traces, PINGOK must appear in the text the
    # client sees somewhere (raw or post-strip).
    assert "PINGOK" in content


def test_qwen_client_chat_strips_think_block_live(live_server) -> None:
    # End-to-end through QwenClient -- this is the path the agent
    # loop uses. Validates that _strip_think_blocks + _extract_text
    # produces a clean answer (loop 217 fix) against the actual model.
    import dataclasses

    from qwen_coder_mcp.config import load_settings
    from qwen_coder_mcp.qwen_client import QwenClient

    base = load_settings()
    s = dataclasses.replace(
        base,
        base_url=f"{live_server['base_url']}/v1",
        api_key=live_server["api_key"],
    )
    qc = QwenClient(s)
    out = qc.chat(
        [
            {
                "role": "user",
                "content": (
                    "Reply with only the literal token PINGOK and "
                    "nothing else."
                ),
            }
        ],
        max_tokens=96,
        temperature=0,
    )
    assert "PINGOK" in out
    # Crucially: no leaked thinking trace.
    assert "<think>" not in out
    assert "</think>" not in out
