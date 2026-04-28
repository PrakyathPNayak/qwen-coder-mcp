"""End-to-end validation that ``serve_qwen.sh`` emits an argv vLLM
will actually accept.

The dry-run argv tests in ``test_serve_qwen_sh.py`` only assert
*string-equality* on flag names — they passed all the way through
the vLLM-0.11 release that removed ``--swap-space``, because the
script and the tests both hardcoded the same wrong string. A user
running the actual launcher hit:

    vllm: error: unrecognized arguments: --swap-space 16

To prevent that class of regression we shell out to the real
``vllm serve --help=all`` (when a vLLM venv is reachable) and
assert that *every* long flag the script emits is recognised.
Skipped cleanly when no vLLM install is available so CI on a
machine without the GPU venv doesn't fail.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "serve_qwen.sh"
VLLM_VENV = REPO_ROOT / ".venv-serve" / "bin" / "vllm"


def _vllm_executable() -> str | None:
    """Locate a vllm CLI we can call. Prefer the project's local
    .venv-serve install; fall back to whatever is on PATH; return None
    if neither is available."""
    if VLLM_VENV.exists() and os.access(VLLM_VENV, os.X_OK):
        return str(VLLM_VENV)
    from shutil import which

    return which("vllm")


def _vllm_help_all(vllm_exe: str) -> str:
    # --help=all dumps every flag across every config group; that's
    # the validator surface we want.
    proc = subprocess.run(
        [vllm_exe, "serve", "--help=all"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc.stdout + proc.stderr


def _flags_in_help(help_text: str) -> set[str]:
    # vllm renders flags one-per-line indented like ``  --max-model-len``
    # or ``  --enable-foo, --no-enable-foo``. Pick up every long flag.
    return set(re.findall(r"--[a-zA-Z][a-zA-Z0-9-]*", help_text))


def _dry_run_argv() -> list[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("QWEN_SERVE_")}
    env["QWEN_SERVE_DRY_RUN"] = "1"
    env["PATH"] = os.environ.get("PATH", "")
    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return [line for line in proc.stdout.splitlines() if line]


@pytest.fixture(scope="module")
def vllm_help() -> str:
    exe = _vllm_executable()
    if exe is None:
        pytest.skip("no vllm executable found; cannot validate against real CLI")
    return _vllm_help_all(exe)


@pytest.fixture(scope="module")
def help_flags(vllm_help: str) -> set[str]:
    flags = _flags_in_help(vllm_help)
    if not flags:
        pytest.skip("vllm --help=all returned no flags; CLI shape changed")
    return flags


class TestServeArgvAcceptedByVllm:
    def test_default_argv_only_uses_recognised_flags(
        self, help_flags: set[str]
    ) -> None:
        argv = _dry_run_argv()
        offending: list[str] = []
        for tok in argv:
            if not tok.startswith("--"):
                continue
            # Strip a trailing =value form just in case.
            base = tok.split("=", 1)[0]
            if base not in help_flags:
                offending.append(base)
        assert offending == [], (
            f"serve_qwen.sh emits flags vLLM does not recognise: {offending}\n"
            f"This is exactly the failure mode that took --swap-space to "
            f"production. Either rename the flag in the script or add the "
            f"missing flag to vLLM."
        )

    def test_legacy_swap_space_no_longer_emitted(
        self, help_flags: set[str]
    ) -> None:
        # Regression pin: --swap-space was removed in vLLM 0.11. If
        # this assertion ever fires it means someone re-added the
        # legacy flag; it will crash the serve launcher.
        argv = _dry_run_argv()
        assert "--swap-space" not in argv, (
            "--swap-space was removed in vLLM 0.11; use --kv-offloading-size"
        )

    def test_kv_offloading_flag_recognised(self, help_flags: set[str]) -> None:
        # Sanity: the *replacement* flag we now emit really exists.
        assert "--kv-offloading-size" in help_flags
        assert "--kv-offloading-backend" in help_flags

    def test_disable_hybrid_kv_cache_manager_recognised(
        self, help_flags: set[str]
    ) -> None:
        # Loop 211: the native OffloadingConnector is incompatible with
        # the Hybrid KV Cache Manager (HMA), which is enabled by
        # default. vLLM raises at engine-init time:
        #   ValueError: Connector OffloadingConnector does not support
        #   HMA but HMA is enabled. Please set
        #   `--disable-hybrid-kv-cache-manager`.
        # If a future vLLM ever renames or removes this flag, this
        # assertion fires before the launcher crashes in production.
        assert "--disable-hybrid-kv-cache-manager" in help_flags

    def test_offloading_paired_with_disable_hybrid_in_argv(
        self, help_flags: set[str]
    ) -> None:
        # Pure-argv invariant (no vLLM dependency): when the dry-run
        # argv contains --kv-offloading-size, it must also contain
        # --disable-hybrid-kv-cache-manager. This is the exact engine-
        # init failure the user hit; pinning the pairing here catches
        # a future split that drops only one of the two flags.
        argv = _dry_run_argv()
        if "--kv-offloading-size" in argv:
            assert "--disable-hybrid-kv-cache-manager" in argv, (
                "--kv-offloading-size requires --disable-hybrid-kv-cache-manager; "
                "engine init will raise OffloadingConnector-vs-HMA at startup."
            )

    def test_chunked_prefill_flag_recognised(self, help_flags: set[str]) -> None:
        assert "--enable-chunked-prefill" in help_flags
        assert "--max-num-batched-tokens" in help_flags

    def test_core_flags_recognised(self, help_flags: set[str]) -> None:
        # Belt-and-braces: pin the small handful of flags the launcher
        # absolutely cannot ship without. Catches a future vLLM rename.
        for flag in (
            "--host",
            "--port",
            "--dtype",
            "--max-model-len",
            "--max-num-seqs",
            "--kv-cache-dtype",
            "--gpu-memory-utilization",
            "--api-key",
            "--served-model-name",
            "--trust-remote-code",
            "--enforce-eager",
            "--limit-mm-per-prompt",
        ):
            assert flag in help_flags, (
                f"{flag} no longer recognised by vllm serve --help=all"
            )

    def test_argv_with_kv_offload_zero_still_clean(
        self, help_flags: set[str]
    ) -> None:
        # User can opt out of offloading; remaining argv must still be
        # entirely valid.
        env = {k: v for k, v in os.environ.items() if not k.startswith("QWEN_SERVE_")}
        env["QWEN_SERVE_DRY_RUN"] = "1"
        env["QWEN_SERVE_KV_OFFLOAD_GIB"] = "0"
        env["PATH"] = os.environ.get("PATH", "")
        proc = subprocess.run(
            ["bash", str(SCRIPT)],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        argv = [line for line in proc.stdout.splitlines() if line]
        offending = [
            t.split("=", 1)[0]
            for t in argv
            if t.startswith("--") and t.split("=", 1)[0] not in help_flags
        ]
        assert offending == []
        # And the offloading flags really were dropped.
        assert "--kv-offloading-size" not in argv
        assert "--kv-offloading-backend" not in argv
