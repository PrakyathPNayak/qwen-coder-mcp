"""Tests for ``scripts/serve_qwen.sh``.

The script normally launches vLLM in the background, so it is hard to
test directly. We use ``QWEN_SERVE_DRY_RUN=1`` which prints the argv it
*would* exec (one arg per line) and exits 0, then assert on the captured
arguments. This locks the OOM-mitigation defaults (max_num_seqs=1,
kv-cache-dtype=fp8, --enforce-eager, --limit-mm-per-prompt) in place so
future edits cannot silently regress them.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "serve_qwen.sh"


def _run(env_overrides: dict[str, str] | None = None) -> list[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("QWEN_SERVE_")}
    env["QWEN_SERVE_DRY_RUN"] = "1"
    env["PATH"] = os.environ.get("PATH", "")
    if env_overrides:
        env.update(env_overrides)
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


def _argv_after_marker(lines: list[str]) -> list[str]:
    for i, line in enumerate(lines):
        if line == "vllm":
            return lines[i:]
    raise AssertionError(f"no vllm marker in dry-run output: {lines!r}")


def _flag_value(argv: list[str], flag: str) -> str:
    assert flag in argv, f"flag {flag} missing from argv: {argv}"
    return argv[argv.index(flag) + 1]


class TestServeScriptDefaults:
    def test_script_exists_and_is_bash(self) -> None:
        assert SCRIPT.exists()
        assert SCRIPT.read_text().startswith("#!/usr/bin/env bash")

    def test_script_is_syntactically_valid(self) -> None:
        subprocess.run(["bash", "-n", str(SCRIPT)], check=True)

    def test_default_invocation_uses_int4_27b_model(self) -> None:
        argv = _argv_after_marker(_run())
        assert argv[0] == "vllm"
        assert argv[1] == "serve"
        assert argv[2] == "Lorbus/Qwen3.6-27B-int4-AutoRound"

    def test_default_oom_safe_kv_settings(self) -> None:
        argv = _argv_after_marker(_run())
        # Loop 171 raised the long-context defaults: 64k context, fp8
        # KV, 0.95 GPU util, single sequence, 16 GiB CPU offload.
        # Loop 205 migrated --swap-space to --kv-offloading-size when
        # vLLM 0.11 removed the legacy flag (see test_serve_qwen_help_validation.py).
        assert _flag_value(argv, "--max-model-len") == "65536"
        assert _flag_value(argv, "--max-num-seqs") == "1"
        assert _flag_value(argv, "--kv-cache-dtype") == "fp8"
        assert _flag_value(argv, "--gpu-memory-utilization") == "0.95"
        assert _flag_value(argv, "--kv-offloading-size") == "16"
        assert _flag_value(argv, "--kv-offloading-backend") == "native"

    def test_default_enables_chunked_prefill(self) -> None:
        argv = _argv_after_marker(_run())
        assert "--enable-chunked-prefill" in argv
        assert _flag_value(argv, "--max-num-batched-tokens") == "4096"

    def test_default_includes_enforce_eager(self) -> None:
        argv = _argv_after_marker(_run())
        assert "--enforce-eager" in argv

    def test_default_disables_multimodal_encoder_cache(self) -> None:
        argv = _argv_after_marker(_run())
        assert _flag_value(argv, "--limit-mm-per-prompt") == '{"image":0,"video":0}'

    def test_default_trust_remote_code(self) -> None:
        argv = _argv_after_marker(_run())
        assert "--trust-remote-code" in argv

    def test_default_served_model_alias(self) -> None:
        argv = _argv_after_marker(_run())
        assert _flag_value(argv, "--served-model-name") == "qwen3.6-27b"

    def test_default_bind_host_and_port(self) -> None:
        argv = _argv_after_marker(_run())
        assert _flag_value(argv, "--host") == "127.0.0.1"
        assert _flag_value(argv, "--port") == "8000"

    def test_pytorch_allocator_env_advertised(self) -> None:
        lines = _run()
        assert any(
            "PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True" in line
            for line in lines
        )


class TestServeScriptOverrides:
    def test_max_seqs_override(self) -> None:
        argv = _argv_after_marker(_run({"QWEN_SERVE_MAX_SEQS": "8"}))
        assert _flag_value(argv, "--max-num-seqs") == "8"

    def test_max_len_override(self) -> None:
        argv = _argv_after_marker(_run({"QWEN_SERVE_MAX_LEN": "8192"}))
        assert _flag_value(argv, "--max-model-len") == "8192"

    def test_gpu_util_override(self) -> None:
        argv = _argv_after_marker(_run({"QWEN_SERVE_GPU_UTIL": "0.75"}))
        assert _flag_value(argv, "--gpu-memory-utilization") == "0.75"

    def test_kv_dtype_override(self) -> None:
        argv = _argv_after_marker(_run({"QWEN_SERVE_KV_DTYPE": "auto"}))
        assert _flag_value(argv, "--kv-cache-dtype") == "auto"

    def test_eager_zero_drops_flag(self) -> None:
        argv = _argv_after_marker(_run({"QWEN_SERVE_EAGER": "0"}))
        assert "--enforce-eager" not in argv

    def test_port_override(self) -> None:
        argv = _argv_after_marker(_run({"QWEN_SERVE_PORT": "9001"}))
        assert _flag_value(argv, "--port") == "9001"

    def test_model_override(self) -> None:
        argv = _argv_after_marker(_run({"QWEN_SERVE_MODEL": "Qwen/Qwen3.6-27B-FP8"}))
        assert argv[2] == "Qwen/Qwen3.6-27B-FP8"

    def test_api_key_override(self) -> None:
        argv = _argv_after_marker(_run({"QWEN_SERVE_API_KEY": "secret-token"}))
        assert _flag_value(argv, "--api-key") == "secret-token"

    def test_limit_mm_empty_drops_flag(self) -> None:
        argv = _argv_after_marker(_run({"QWEN_SERVE_LIMIT_MM": ""}))
        assert "--limit-mm-per-prompt" not in argv

    def test_limit_mm_custom_json(self) -> None:
        argv = _argv_after_marker(
            _run({"QWEN_SERVE_LIMIT_MM": '{"image":1,"video":0}'})
        )
        assert _flag_value(argv, "--limit-mm-per-prompt") == '{"image":1,"video":0}'

    def test_extra_args_appended(self) -> None:
        argv = _argv_after_marker(
            _run({"QWEN_SERVE_EXTRA": "--kv-offloading-size 4"})
        )
        assert "--kv-offloading-size" in argv
        assert "4" in argv

    def test_kv_offload_override(self) -> None:
        argv = _argv_after_marker(_run({"QWEN_SERVE_KV_OFFLOAD_GIB": "64"}))
        assert _flag_value(argv, "--kv-offloading-size") == "64"
        assert _flag_value(argv, "--kv-offloading-backend") == "native"

    def test_kv_offload_zero_drops_flag(self) -> None:
        # Operators on RAM-constrained hosts can opt out of CPU offload.
        argv = _argv_after_marker(_run({"QWEN_SERVE_KV_OFFLOAD_GIB": "0"}))
        assert "--kv-offloading-size" not in argv
        assert "--kv-offloading-backend" not in argv

    def test_swap_space_alias_still_honoured(self) -> None:
        # Backwards compat for operators with QWEN_SERVE_SWAP_SPACE
        # already in their environment files. Maps to the new flag.
        argv = _argv_after_marker(_run({"QWEN_SERVE_SWAP_SPACE": "32"}))
        assert _flag_value(argv, "--kv-offloading-size") == "32"
        # And the obsolete --swap-space flag is no longer in argv.
        assert "--swap-space" not in argv

    def test_kv_offload_takes_precedence_over_swap_space_alias(self) -> None:
        argv = _argv_after_marker(
            _run(
                {
                    "QWEN_SERVE_SWAP_SPACE": "32",
                    "QWEN_SERVE_KV_OFFLOAD_GIB": "8",
                }
            )
        )
        assert _flag_value(argv, "--kv-offloading-size") == "8"

    def test_chunked_prefill_disable(self) -> None:
        argv = _argv_after_marker(_run({"QWEN_SERVE_CHUNKED_PREFILL": "0"}))
        assert "--enable-chunked-prefill" not in argv
        assert "--max-num-batched-tokens" not in argv

    def test_max_batched_override(self) -> None:
        argv = _argv_after_marker(_run({"QWEN_SERVE_MAX_BATCHED": "2048"}))
        assert _flag_value(argv, "--max-num-batched-tokens") == "2048"

    def test_long_context_override(self) -> None:
        # Push to 128k for users with extra VRAM headroom (e.g. fp8
        # weights instead of int4). Just make sure the flag pipeline
        # actually forwards the value.
        argv = _argv_after_marker(_run({"QWEN_SERVE_MAX_LEN": "131072"}))
        assert _flag_value(argv, "--max-model-len") == "131072"
