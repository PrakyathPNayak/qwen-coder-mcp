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
        # KV, 0.88 GPU util (loop 227, lowered from 0.95 after the
        # GDN/mamba forward-path OOM), single sequence.
        # Loop 205 migrated --swap-space to --kv-offloading-size when
        # vLLM 0.11 removed the legacy flag (see test_serve_qwen_help_validation.py).
        # Loop 215' (this loop): the default Qwen3.6 model is hybrid
        # (mamba+attention). Hybrid models REQUIRE the Hybrid KV Cache
        # Manager which the native OffloadingConnector forbids -- so
        # offloading is structurally unavailable for the default model.
        # Pin: HMA stays enabled, no offloading flags emitted.
        assert _flag_value(argv, "--max-model-len") == "65536"
        assert _flag_value(argv, "--max-num-seqs") == "1"
        assert _flag_value(argv, "--kv-cache-dtype") == "fp8"
        assert _flag_value(argv, "--gpu-memory-utilization") == "0.88"
        assert "--kv-offloading-size" not in argv
        assert "--kv-offloading-backend" not in argv
        assert "--disable-hybrid-kv-cache-manager" not in argv

    def test_default_enables_chunked_prefill(self) -> None:
        argv = _argv_after_marker(_run())
        assert "--enable-chunked-prefill" in argv
        assert _flag_value(argv, "--max-num-batched-tokens") == "2048"

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

    def test_gpu_util_override_upward_still_forwards(self) -> None:
        # Loop 227: lowered the default from 0.95 to 0.88 to leave
        # transient headroom for the GDN/mamba forward-path scratch
        # bulge. Users with a 48GB+ card who want the old aggressive
        # KV budget can still set QWEN_SERVE_GPU_UTIL=0.95 and the
        # script forwards it verbatim with no clamp.
        argv = _argv_after_marker(_run({"QWEN_SERVE_GPU_UTIL": "0.95"}))
        assert _flag_value(argv, "--gpu-memory-utilization") == "0.95"

    def test_max_batched_override_upward_still_forwards(self) -> None:
        # Loop 227 mirror: default lowered 4096 -> 2048 for the same
        # GDN-scratch reason. Override path stays unclamped.
        argv = _argv_after_marker(_run({"QWEN_SERVE_MAX_BATCHED": "4096"}))
        assert _flag_value(argv, "--max-num-batched-tokens") == "4096"

    def test_loop_227_oom_safe_defaults_combined(self) -> None:
        # Loop 227 regression pin: a stock invocation must produce
        # BOTH lowered defaults (0.88 gpu_util AND 2048 max_batched)
        # in the same argv. If a future loop tunes one without the
        # other, this fires immediately.
        argv = _argv_after_marker(_run())
        assert _flag_value(argv, "--gpu-memory-utilization") == "0.88"
        assert _flag_value(argv, "--max-num-batched-tokens") == "2048"

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
        # Use a non-hybrid model name so the hybrid-detection guard
        # doesn't force offloading off.
        argv = _argv_after_marker(
            _run(
                {
                    "QWEN_SERVE_KV_OFFLOAD_GIB": "64",
                    "QWEN_SERVE_MODEL": "Qwen/Qwen2.5-7B-Instruct",
                }
            )
        )
        assert _flag_value(argv, "--kv-offloading-size") == "64"
        assert _flag_value(argv, "--kv-offloading-backend") == "native"

    def test_kv_offload_zero_drops_flag(self) -> None:
        # Operators on RAM-constrained hosts can opt out of CPU offload.
        # Use a non-hybrid model to isolate this check from the hybrid guard.
        argv = _argv_after_marker(
            _run(
                {
                    "QWEN_SERVE_KV_OFFLOAD_GIB": "0",
                    "QWEN_SERVE_MODEL": "Qwen/Qwen2.5-7B-Instruct",
                }
            )
        )
        assert "--kv-offloading-size" not in argv
        assert "--kv-offloading-backend" not in argv
        # The HMA-disable flag is paired *with* offloading, so it should
        # also disappear when offloading is off.
        assert "--disable-hybrid-kv-cache-manager" not in argv

    def test_kv_offload_pairs_with_disable_hybrid_kv_manager(self) -> None:
        # Loop 211: the native OffloadingConnector raises at engine init
        # if HMA is enabled. Whenever we emit --kv-offloading-size we
        # must also emit --disable-hybrid-kv-cache-manager. Use a
        # non-hybrid model so the hybrid guard does not pre-empt this.
        argv = _argv_after_marker(
            _run({"QWEN_SERVE_MODEL": "Qwen/Qwen2.5-7B-Instruct"})
        )
        assert "--kv-offloading-size" in argv
        assert "--disable-hybrid-kv-cache-manager" in argv
        argv2 = _argv_after_marker(
            _run(
                {
                    "QWEN_SERVE_KV_OFFLOAD_GIB": "32",
                    "QWEN_SERVE_MODEL": "Qwen/Qwen2.5-7B-Instruct",
                }
            )
        )
        assert "--kv-offloading-size" in argv2
        assert "--disable-hybrid-kv-cache-manager" in argv2

    def test_hybrid_model_forces_offloading_off_even_when_requested(
        self,
    ) -> None:
        # Loop 215' (this loop): hybrid models (Qwen3-Next, Qwen3.6,
        # Jamba, mamba) REQUIRE the Hybrid KV Cache Manager. The native
        # OffloadingConnector forbids HMA. Mutual exclusion ->
        # offloading must be force-disabled for hybrid models, even if
        # the operator explicitly sets KV_OFFLOAD_GIB=64. Pin every
        # known hybrid family.
        for model in (
            "Lorbus/Qwen3.6-27B-int4-AutoRound",       # default
            "Qwen/Qwen3-Next-80B-A3B-Instruct",
            "ai21labs/Jamba-v0.1",
            "state-spaces/mamba-130m",
            "nvidia/NemotronH-8B-Instruct",
            "MiniMaxAI/MiniMax-Text-01",
        ):
            argv = _argv_after_marker(
                _run(
                    {
                        "QWEN_SERVE_MODEL": model,
                        "QWEN_SERVE_KV_OFFLOAD_GIB": "64",
                    }
                )
            )
            assert "--kv-offloading-size" not in argv, (
                f"hybrid model {model} must NOT receive offloading flags "
                "(HMA conflict; vLLM will fail engine init)"
            )
            assert "--kv-offloading-backend" not in argv
            assert "--disable-hybrid-kv-cache-manager" not in argv

    def test_non_hybrid_model_keeps_offloading(self) -> None:
        # The guard must be selective: dense models like Qwen2.5 are
        # NOT hybrid and benefit from offloading. Pin both directions.
        argv = _argv_after_marker(
            _run({"QWEN_SERVE_MODEL": "Qwen/Qwen2.5-7B-Instruct"})
        )
        assert "--kv-offloading-size" in argv
        assert "--disable-hybrid-kv-cache-manager" in argv

    def test_swap_space_alias_still_honoured(self) -> None:
        # Backwards compat for operators with QWEN_SERVE_SWAP_SPACE
        # already in their environment files. Maps to the new flag.
        # Use a non-hybrid model to bypass the hybrid offloading guard.
        argv = _argv_after_marker(
            _run(
                {
                    "QWEN_SERVE_SWAP_SPACE": "32",
                    "QWEN_SERVE_MODEL": "Qwen/Qwen2.5-7B-Instruct",
                }
            )
        )
        assert _flag_value(argv, "--kv-offloading-size") == "32"
        # And the obsolete --swap-space flag is no longer in argv.
        assert "--swap-space" not in argv

    def test_kv_offload_takes_precedence_over_swap_space_alias(self) -> None:
        argv = _argv_after_marker(
            _run(
                {
                    "QWEN_SERVE_SWAP_SPACE": "32",
                    "QWEN_SERVE_KV_OFFLOAD_GIB": "8",
                    "QWEN_SERVE_MODEL": "Qwen/Qwen2.5-7B-Instruct",
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


class TestForceOffloadEscapeHatch:
    """Loop 221: ``QWEN_SERVE_FORCE_OFFLOAD=1`` overrides the
    hybrid-model guard from loop 216.

    Why it exists: the guard matches by model-name substring (no vLLM
    CLI to ask "is this model hybrid?" pre-launch). False positives
    are possible for forks/mirrors whose HF id happens to contain a
    matching substring (e.g. a dense model named
    ``acme/dense-mamba-distilled-7b``). The escape hatch lets the
    operator override the guard at their own risk -- if the model
    really is hybrid, vLLM's engine init will raise the loop-216
    ValueError. The user has explicitly opted in.
    """

    def test_force_offload_keeps_offloading_for_qwen3_6(self) -> None:
        # The default hybrid model with the escape hatch on. Operator
        # is asserting "I know what I'm doing; emit the flags anyway."
        argv = _argv_after_marker(
            _run(
                {
                    "QWEN_SERVE_FORCE_OFFLOAD": "1",
                    "QWEN_SERVE_KV_OFFLOAD_GIB": "16",
                }
            )
        )
        assert "--kv-offloading-size" in argv, (
            "QWEN_SERVE_FORCE_OFFLOAD=1 must bypass the hybrid guard"
        )
        assert _flag_value(argv, "--kv-offloading-size") == "16"
        # And HMA must be disabled, since OffloadingConnector requires
        # that (loop 211 invariant): whenever offloading flags are
        # emitted, --disable-hybrid-kv-cache-manager goes with them.
        assert "--disable-hybrid-kv-cache-manager" in argv

    def test_force_offload_keeps_offloading_for_jamba(self) -> None:
        # Pin a different hybrid family to make sure the escape hatch
        # is not accidentally Qwen-specific.
        argv = _argv_after_marker(
            _run(
                {
                    "QWEN_SERVE_FORCE_OFFLOAD": "1",
                    "QWEN_SERVE_MODEL": "ai21labs/Jamba-v0.1",
                    "QWEN_SERVE_KV_OFFLOAD_GIB": "8",
                }
            )
        )
        assert "--kv-offloading-size" in argv
        assert _flag_value(argv, "--kv-offloading-size") == "8"

    def test_force_offload_true_also_works(self) -> None:
        # Mirrors the QWEN_SERVE_EAGER convention: accept '1' or 'true'.
        argv = _argv_after_marker(
            _run(
                {
                    "QWEN_SERVE_FORCE_OFFLOAD": "true",
                    "QWEN_SERVE_KV_OFFLOAD_GIB": "16",
                }
            )
        )
        assert "--kv-offloading-size" in argv

    def test_force_offload_zero_does_not_re_enable_offloading(self) -> None:
        # If the operator explicitly set KV_OFFLOAD_GIB=0 AND turned
        # the escape hatch on, we must NOT re-add offloading flags --
        # the user's explicit 0 wins. The escape hatch only bypasses
        # the *guard*; it does not invent offloading.
        argv = _argv_after_marker(
            _run(
                {
                    "QWEN_SERVE_FORCE_OFFLOAD": "1",
                    "QWEN_SERVE_KV_OFFLOAD_GIB": "0",
                }
            )
        )
        assert "--kv-offloading-size" not in argv
        assert "--disable-hybrid-kv-cache-manager" not in argv

    def test_force_offload_unset_keeps_guard_active(self) -> None:
        # Default behaviour (no env var) is the loop-216 guard --
        # offloading off for hybrid models. Make sure the escape hatch
        # is opt-in, not opt-out.
        argv = _argv_after_marker(_run({"QWEN_SERVE_KV_OFFLOAD_GIB": "16"}))
        assert "--kv-offloading-size" not in argv

    def test_force_offload_zero_string_keeps_guard_active(self) -> None:
        # Symmetry with QWEN_SERVE_EAGER: '0' must NOT enable the
        # escape hatch (would be a footgun for shells that leave the
        # var defined as 0 from a previous run).
        argv = _argv_after_marker(
            _run(
                {
                    "QWEN_SERVE_FORCE_OFFLOAD": "0",
                    "QWEN_SERVE_KV_OFFLOAD_GIB": "16",
                }
            )
        )
        assert "--kv-offloading-size" not in argv

    def test_force_offload_emits_warning_to_stderr(self) -> None:
        # Operators need a loud-enough breadcrumb in case the engine
        # then fails init -- they should be able to grep `serve.log`
        # for why offloading flags appeared on a hybrid model. Pin
        # the warning so a future refactor cannot silently drop it.
        env = {k: v for k, v in os.environ.items() if not k.startswith("QWEN_SERVE_")}
        env["QWEN_SERVE_DRY_RUN"] = "1"
        env["PATH"] = os.environ.get("PATH", "")
        env["QWEN_SERVE_FORCE_OFFLOAD"] = "1"
        env["QWEN_SERVE_KV_OFFLOAD_GIB"] = "16"
        proc = subprocess.run(
            ["bash", str(SCRIPT)],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        assert "QWEN_SERVE_FORCE_OFFLOAD" in proc.stderr
        assert "skipping" in proc.stderr.lower()

