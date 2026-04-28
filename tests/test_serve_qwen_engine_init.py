"""Heavy end-to-end test: actually attempt vLLM engine initialization
with the exact argv ``serve_qwen.sh`` emits.

The user-reported regression chain went:
  loop 205: --swap-space removed in vLLM 0.11      (caught by --help=all)
  loop 211: OffloadingConnector incompatible       (NOT caught by --help=all)
            with default Hybrid KV Cache Manager;
            fails at engine init, not at argparse.

The --help=all validator in ``test_serve_qwen_help_validation.py``
only checks that flag *names* exist. It cannot catch
combination-incompatibilities that fire during engine bring-up.
This test does.

It is gated behind ``QWEN_SERVE_E2E_ENGINE=1`` because:
- it needs a real GPU
- it downloads a tiny model (default: facebook/opt-125m, ~250 MB)
- it spins up vLLM for ~30-60 seconds

CI/dev runs skip cleanly. Operators can opt in with:

    QWEN_SERVE_E2E_ENGINE=1 pytest tests/test_serve_qwen_engine_init.py -v

The test substitutes a tiny model and a low GPU-util cap into the
launcher's env, runs the script, polls /v1/models for up to 90s,
and FAILS THE TEST if the server log mentions a known engine-init
exception class. This is the lights-on smoke test that would have
caught loop 211's bug in advance.
"""
from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "serve_qwen.sh"
VLLM_VENV = REPO_ROOT / ".venv-serve" / "bin" / "vllm"


_GATE = os.environ.get("QWEN_SERVE_E2E_ENGINE", "").lower() in {"1", "true", "yes"}

pytestmark = pytest.mark.skipif(
    not _GATE,
    reason="set QWEN_SERVE_E2E_ENGINE=1 to run heavy engine-init E2E (needs GPU + model download + ~60s)",
)


def _has_gpu() -> bool:
    return shutil.which("nvidia-smi") is not None


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _looks_like_engine_init_failure(log: str) -> tuple[bool, str]:
    """Spot the known engine-init failure shapes. Returns (is_failure,
    excerpt). Operators see the excerpt verbatim if the test fails."""
    markers = [
        # Loop 211: HMA/OffloadingConnector incompatibility.
        "does not support HMA",
        # Generic vLLM engine-init disasters that should never reach
        # the API server.
        "Engine core initialization failed",
        "RuntimeError: Engine",
        "ValueError:",
        "TypeError:",
        # vLLM logs traceback markers that leak through stdout.
        "EngineCore failed to start",
    ]
    for m in markers:
        idx = log.find(m)
        if idx != -1:
            # Return ~400 chars of context for the failure message.
            start = max(0, idx - 100)
            end = min(len(log), idx + 300)
            return True, log[start:end]
    return False, ""


@pytest.fixture()
def tiny_model() -> str:
    # opt-125m is ~250 MB and lives in HF cache after the first run.
    # Operators can override.
    return os.environ.get("QWEN_SERVE_E2E_MODEL", "facebook/opt-125m")


def test_engine_initialises_with_dry_run_argv(tiny_model: str) -> None:
    if not VLLM_VENV.exists():
        pytest.skip("no .venv-serve/bin/vllm; cannot launch engine")
    if not _has_gpu():
        pytest.skip("no nvidia-smi found; engine init needs a GPU")

    port = _free_port()
    log_path = REPO_ROOT / ".loop" / "serve_e2e.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if log_path.exists():
        log_path.unlink()

    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("QWEN_SERVE_E2E_")
    }
    env.update(
        {
            "QWEN_SERVE_MODEL": tiny_model,
            "QWEN_SERVE_PORT": str(port),
            "QWEN_SERVE_MAX_LEN": "256",
            "QWEN_SERVE_MAX_SEQS": "1",
            "QWEN_SERVE_GPU_UTIL": "0.30",
            # opt-125m doesn't need fp8 KV; some tiny models reject it.
            "QWEN_SERVE_KV_DTYPE": "auto",
            # Default offloading should still work; this is the path
            # that caught loop 211.
            "QWEN_SERVE_KV_OFFLOAD_GIB": "1",
            "QWEN_SERVE_LOG": str(log_path),
            "QWEN_SERVE_DRY_RUN": "0",
            # opt-125m has no chat template; that's fine for engine-init
            # validation — we never send a chat request, only check
            # /v1/models.
            "QWEN_SERVE_LIMIT_MM": "",
        }
    )

    # The launcher backgrounds vllm via setsid + nohup. We get its
    # PID from the .loop/serve_qwen.pid file the script writes.
    pid_file = REPO_ROOT / ".loop" / "serve_qwen.pid"
    if pid_file.exists():
        pid_file.unlink()

    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert proc.returncode == 0, f"launcher script failed: {proc.stderr}"

    # Poll up to 90s for either /v1/models success or a fatal log entry.
    import urllib.error
    import urllib.request

    deadline = time.monotonic() + 90.0
    last_err: str | None = None
    succeeded = False
    failed_marker: str | None = None

    try:
        while time.monotonic() < deadline:
            # 1. Check the log for known fatal patterns.
            if log_path.exists():
                log = log_path.read_text(errors="replace")
                bad, excerpt = _looks_like_engine_init_failure(log)
                if bad:
                    failed_marker = excerpt
                    break
            # 2. Try the /v1/models endpoint.
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/v1/models",
                    headers={"Authorization": "Bearer EMPTY"},
                )
                with urllib.request.urlopen(req, timeout=2.0) as resp:
                    if resp.status == 200:
                        succeeded = True
                        break
            except (urllib.error.URLError, ConnectionRefusedError, OSError) as exc:
                last_err = str(exc)
            time.sleep(2.0)
    finally:
        # Always tear down the engine.
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                os.kill(pid, signal.SIGTERM)
                # Give it a moment to drain.
                time.sleep(2.0)
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            except (ValueError, ProcessLookupError):
                pass

    if failed_marker is not None:
        pytest.fail(
            "vLLM engine-init failure detected in serve log:\n"
            f"---\n{failed_marker}\n---"
        )
    assert succeeded, (
        f"engine did not become ready within 90s on port {port}. "
        f"last error: {last_err!r}\n"
        f"log tail: {log_path.read_text(errors='replace')[-2000:] if log_path.exists() else '(no log)'}"
    )
