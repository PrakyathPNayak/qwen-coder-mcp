"""Tests for ``scripts/wait_ready.sh``.

Completes the scripts/ coverage arc started by the loop-205
serve_qwen.sh dry-run pattern and continued by loop-222
stop_qwen.sh sandbox tests.

The script polls ``/v1/models`` once per second up to 600s. To
test the success and timeout branches deterministically (and
quickly) we prepend a tempdir to PATH that shadows ``curl`` and
``seq`` with deterministic stand-ins:

* fake ``seq`` -> emits ``1`` only, so the loop runs at most one
  iteration -> the timeout test takes ~1s instead of 10 minutes.
* fake ``curl`` -> exits 0 (success branch) or exits 22 (HTTP
  failure branch). It also writes the URL + Authorization header
  it received to a side-channel file so tests can pin the
  outgoing request shape.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "wait_ready.sh"


def _write_fake(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    path.chmod(0o755)


def _make_path_overlay(tmp_path: Path, *, curl_body: str, seq_body: str) -> Path:
    """Return a directory that, when prepended to PATH, shadows
    curl and seq with the provided body. Returns the overlay
    directory."""
    overlay = tmp_path / "bin"
    overlay.mkdir()
    _write_fake(overlay / "curl", curl_body)
    _write_fake(overlay / "seq", seq_body)
    return overlay


def _run(
    overlay: Path,
    *,
    env_overrides: dict[str, str] | None = None,
    expect_zero: bool = True,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items()}
    env["PATH"] = f"{overlay}:{env['PATH']}"
    if env_overrides:
        env.update(env_overrides)
    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
        check=False,
    )
    if expect_zero:
        assert proc.returncode == 0, (
            f"script exited {proc.returncode}\n"
            f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
        )
    return proc


class TestWaitReadyStaticInvariants:
    def test_script_exists_and_is_bash(self) -> None:
        assert SCRIPT.exists()
        assert SCRIPT.read_text().startswith("#!/usr/bin/env bash")

    def test_script_is_syntactically_valid(self) -> None:
        subprocess.run(["bash", "-n", str(SCRIPT)], check=True)

    def test_script_uses_strict_mode(self) -> None:
        # Same load-bearing invariant as the other shell scripts.
        text = SCRIPT.read_text()
        assert "set -euo pipefail" in text

    def test_script_polls_v1_models_endpoint(self) -> None:
        # The choice of /v1/models (and not, say, /health) is
        # deliberate and load-bearing: in vLLM 0.11 /health lives
        # at the server root, but /v1/models is the OpenAI-compat
        # endpoint that proves auth + model registry are live.
        # Loop 215 added a separate /health probe in the Python
        # client; this script intentionally stays on /v1/models so
        # that 'wait_ready' reports the API is consumable, not
        # just that the engine process is up.
        text = SCRIPT.read_text()
        assert "/v1/models" in text

    def test_script_uses_bearer_auth(self) -> None:
        # Operators sometimes set QWEN_SERVE_API_KEY; the script
        # must forward it as a Bearer header or it would 401
        # against a non-EMPTY-keyed server.
        text = SCRIPT.read_text()
        assert "Authorization: Bearer" in text


class TestWaitReadyHappyPath:
    @pytest.fixture
    def capture_path(self, tmp_path: Path) -> Path:
        return tmp_path / "curl.calls"

    def _success_overlay(
        self, tmp_path: Path, capture_path: Path
    ) -> Path:
        return _make_path_overlay(
            tmp_path,
            curl_body=f"""
                # Record the last invocation so the test can pin
                # the URL / Authorization header. Emit a stub
                # /v1/models response body and exit 0.
                echo "ARGS: $*" >> {capture_path}
                if [[ "$*" != *"-fsS"* ]]; then
                  echo "fake curl: missing -fsS in $*" >&2
                  exit 99
                fi
                # The script calls curl twice on success: once to
                # poll, once to print the body. Echo a plausible
                # stub on stdout for the second call; the first
                # call has its stdout redirected to /dev/null by
                # the script.
                echo '{{"data":[{{"id":"qwen3.6"}}]}}'
                exit 0
            """,
            seq_body="""
                # Doesn't matter -- success on the very first
                # iteration -- but emit 1 to be safe.
                echo 1
            """,
        )

    def test_ready_immediately(
        self, tmp_path: Path, capture_path: Path
    ) -> None:
        overlay = self._success_overlay(tmp_path, capture_path)
        proc = _run(overlay)
        assert "polling http://" in proc.stdout
        assert "ready after" in proc.stdout
        # And the response body is echoed verbatim to stdout so an
        # operator running this in CI can see what model came up.
        assert "qwen3.6" in proc.stdout

    def test_curl_invoked_with_correct_url_and_auth(
        self, tmp_path: Path, capture_path: Path
    ) -> None:
        overlay = self._success_overlay(tmp_path, capture_path)
        _run(
            overlay,
            env_overrides={
                "QWEN_SERVE_HOST": "10.0.0.5",
                "QWEN_SERVE_PORT": "9009",
                "QWEN_SERVE_API_KEY": "secret-token",
            },
        )
        calls = capture_path.read_text()
        # URL must reflect host/port env vars.
        assert "http://10.0.0.5:9009/v1/models" in calls
        # And the bearer token must be forwarded.
        assert "Authorization: Bearer secret-token" in calls

    def test_default_host_port_when_unset(
        self, tmp_path: Path, capture_path: Path
    ) -> None:
        overlay = self._success_overlay(tmp_path, capture_path)
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("QWEN_SERVE_")
        }
        env["PATH"] = f"{overlay}:{env['PATH']}"
        proc = subprocess.run(
            ["bash", str(SCRIPT)],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
            check=True,
        )
        assert "127.0.0.1:8000" in proc.stdout
        # Default API_KEY value is the literal "EMPTY"; verify we
        # actually forward that rather than crashing on an unset
        # variable (the script uses parameter-expansion defaults).
        calls = capture_path.read_text()
        assert "Authorization: Bearer EMPTY" in calls


class TestWaitReadyTimeout:
    def test_timeout_branch_exits_one_with_stderr_message(
        self, tmp_path: Path
    ) -> None:
        # Fake curl always fails -> the script falls through the
        # entire seq loop and hits the timeout branch. Fake seq
        # truncates the loop to one iteration so the test takes
        # ~1s rather than the script's real 600s ceiling.
        overlay = _make_path_overlay(
            tmp_path,
            curl_body="""
                # Always fail -- HTTP 22 is curl's "server returned
                # >= 400" exit status, so the script's -fsS will
                # propagate non-zero up to the if-branch.
                exit 22
            """,
            seq_body="""
                # Truncate the 1..600 loop to a single iteration.
                echo 1
            """,
        )
        proc = _run(overlay, expect_zero=False, timeout=20)
        assert proc.returncode == 1
        assert "timed out" in proc.stderr.lower()

    def test_timeout_message_mentions_seconds(self, tmp_path: Path) -> None:
        # The contract is "timed out after 600s" -- a future edit
        # that drops the duration would lose the operator's
        # ability to grok at-a-glance how long they waited.
        overlay = _make_path_overlay(
            tmp_path,
            curl_body="exit 22\n",
            seq_body="echo 1\n",
        )
        proc = _run(overlay, expect_zero=False, timeout=20)
        assert "600s" in proc.stderr
