#!/usr/bin/env bash
# Serve Qwen3.6-27B locally on this RTX 4090 via vLLM (OpenAI-compatible).
#
# Default model is the 4-bit AutoRound quantization that fits in ~14 GB VRAM,
# leaving headroom on a 24 GB 4090 for KV cache at long context.
#
# Usage:
#   ./scripts/serve_qwen.sh                        # default: int4 AutoRound
#   QWEN_SERVE_MODEL=Qwen/Qwen3.6-27B-FP8 ./scripts/serve_qwen.sh   # FP8 (needs ~27 GB)
#   QWEN_SERVE_PORT=8001 ./scripts/serve_qwen.sh
#
# Env knobs:
#   QWEN_SERVE_MODEL        HF model id served by vLLM
#   QWEN_SERVE_PORT         port (default 8000)
#   QWEN_SERVE_HOST         bind host (default 127.0.0.1)
#   QWEN_SERVE_MAX_LEN      max model context (default 65536)
#   QWEN_SERVE_GPU_UTIL     gpu memory utilization (default 0.95)
#   QWEN_SERVE_MAX_SEQS     max concurrent sequences (default 1)
#   QWEN_SERVE_KV_DTYPE     kv cache dtype (default fp8)
#   QWEN_SERVE_EAGER        enforce eager mode (default 1)
#   QWEN_SERVE_KV_OFFLOAD_GIB  CPU RAM (GiB) for KV cache offloading via
#                           --kv-offloading-size (default 16). Replaces the
#                           removed-in-vLLM-0.11 --swap-space flag.
#                           QWEN_SERVE_SWAP_SPACE accepted as deprecated alias.
#   QWEN_SERVE_CHUNKED_PREFILL  chunked prefill on long prompts (default 1)
#   QWEN_SERVE_MAX_BATCHED  per-step batched-token cap (default 4096)
#   QWEN_SERVE_LIMIT_MM     --limit-mm-per-prompt JSON; default disables
#                           image+video to free 1-2 GiB of encoder cache.
#                           Export an empty string to re-enable multimodal.
#   QWEN_SERVE_DRY_RUN      print the vllm serve argv (one per line) and
#                           exit 0 instead of launching; for tests.
#   QWEN_SERVE_DTYPE        weight dtype override (default auto)
#   QWEN_SERVE_API_KEY      bearer token clients must send (default EMPTY)
#   QWEN_SERVE_EXTRA        extra args appended to vllm serve
#
# The script auto-installs vLLM into a local virtualenv at .venv-serve if
# not already present.

set -euo pipefail
cd "$(dirname "$0")/.."

MODEL="${QWEN_SERVE_MODEL:-Lorbus/Qwen3.6-27B-int4-AutoRound}"
PORT="${QWEN_SERVE_PORT:-8000}"
HOST="${QWEN_SERVE_HOST:-127.0.0.1}"
# Defaults tuned for a 24 GB RTX 4090 holding the int4 27B weights
# (~14 GB) plus KV cache headroom. With fp8 KV cache the per-token
# footprint is around 128 KiB, so 65k tokens of context fits in ~8 GiB
# of remaining VRAM. Long prompts require chunked prefill to avoid OOM
# during the initial pass; vLLM also gets a generous CPU swap pool so
# preempted KV blocks (e.g. during a long prefill) can spill to host
# RAM instead of crashing the engine. Override every value below via
# its env var if your card has more memory or you need a different
# context budget.
MAX_LEN="${QWEN_SERVE_MAX_LEN:-65536}"
GPU_UTIL="${QWEN_SERVE_GPU_UTIL:-0.95}"
MAX_SEQS="${QWEN_SERVE_MAX_SEQS:-1}"
KV_DTYPE="${QWEN_SERVE_KV_DTYPE:-fp8}"
EAGER="${QWEN_SERVE_EAGER:-1}"
DTYPE="${QWEN_SERVE_DTYPE:-auto}"
API_KEY="${QWEN_SERVE_API_KEY:-EMPTY}"
EXTRA="${QWEN_SERVE_EXTRA:-}"
# CPU RAM (in GiB) reserved for KV cache offloading. vLLM 0.11+
# replaced the legacy `--swap-space` flag with `--kv-offloading-size`
# (used together with --kv-offloading-backend native). Same semantic
# role: KV blocks under GPU pressure spill to host RAM rather than
# crashing the engine. Set to 0 to disable offloading entirely.
# QWEN_SERVE_SWAP_SPACE is honoured as a deprecated alias so existing
# operator scripts keep working.
KV_OFFLOAD_GIB="${QWEN_SERVE_KV_OFFLOAD_GIB:-${QWEN_SERVE_SWAP_SPACE:-16}}"
# Chunked prefill processes long prompts in slices instead of one
# 65k-token forward pass that would OOM. Default on; set to 0 only
# if you know prefill fits in VRAM.
CHUNKED_PREFILL="${QWEN_SERVE_CHUNKED_PREFILL:-1}"
# Per-step batched-token budget when chunked prefill is on. 4096 keeps
# peak prefill memory bounded even for a 200k-token prompt.
MAX_BATCHED="${QWEN_SERVE_MAX_BATCHED:-4096}"
# Multimodal encoder cache eats 1-2 GiB on a vision-capable Qwen build
# even when the user only sends text. Disable image/video by default so
# the int4 27B + KV cache fits on a single 24 GiB 4090. Override by
# exporting QWEN_SERVE_LIMIT_MM='' (empty) when actually serving images.
if [ "${QWEN_SERVE_LIMIT_MM+set}" = "set" ]; then
  LIMIT_MM="$QWEN_SERVE_LIMIT_MM"
else
  LIMIT_MM='{"image":0,"video":0}'
fi
DRY_RUN="${QWEN_SERVE_DRY_RUN:-0}"

# Reduce CUDA allocator fragmentation -- the OOM error itself recommends
# this (208 MiB reserved-but-unallocated when warmup tried 1.53 GiB).
export PYTORCH_CUDA_ALLOC_CONF="${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True}"

VENV=".venv-serve"
if [ "$DRY_RUN" != "1" ]; then
  if [ ! -d "$VENV" ]; then
    echo "[serve_qwen] creating $VENV and installing vLLM (this can take a while)..."
    python -m venv "$VENV"
    # shellcheck disable=SC1091
    source "$VENV/bin/activate"
    pip install -q --upgrade pip
    pip install -q "vllm>=0.7.2" "auto-round>=0.4.0" "transformers>=4.46"
  else
    # shellcheck disable=SC1091
    source "$VENV/bin/activate"
  fi

  mkdir -p .loop
fi
LOG=".loop/serve.log"
PIDFILE=".loop/serve.pid"

if [ "$DRY_RUN" != "1" ] && [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "[serve_qwen] already running (pid $(cat "$PIDFILE"))" >&2
  exit 1
fi

EAGER_ARG=()
if [ "$EAGER" = "1" ] || [ "$EAGER" = "true" ]; then
  EAGER_ARG=(--enforce-eager)
fi

LIMIT_MM_ARG=()
if [ -n "$LIMIT_MM" ]; then
  LIMIT_MM_ARG=(--limit-mm-per-prompt "$LIMIT_MM")
fi

CHUNKED_ARG=()
if [ "$CHUNKED_PREFILL" = "1" ] || [ "$CHUNKED_PREFILL" = "true" ]; then
  CHUNKED_ARG=(--enable-chunked-prefill --max-num-batched-tokens "$MAX_BATCHED")
fi

echo "[serve_qwen] model=$MODEL host=$HOST port=$PORT"
echo "[serve_qwen] max_len=$MAX_LEN gpu_util=$GPU_UTIL max_seqs=$MAX_SEQS kv_dtype=$KV_DTYPE eager=$EAGER limit_mm=$LIMIT_MM"
echo "[serve_qwen] kv_offload=${KV_OFFLOAD_GIB}GiB chunked_prefill=$CHUNKED_PREFILL max_batched=$MAX_BATCHED"
echo "[serve_qwen] PYTORCH_CUDA_ALLOC_CONF=$PYTORCH_CUDA_ALLOC_CONF"
echo "[serve_qwen] logs -> $LOG"

KV_OFFLOAD_ARG=()
if [ "$KV_OFFLOAD_GIB" != "0" ]; then
  # vLLM 0.11+ flag pair. backend=native uses the in-tree CPU
  # offloader (the lmcache backend has extra runtime deps).
  KV_OFFLOAD_ARG=(--kv-offloading-size "$KV_OFFLOAD_GIB" --kv-offloading-backend native)
fi

CMD=(vllm serve "$MODEL"
  --host "$HOST"
  --port "$PORT"
  --dtype "$DTYPE"
  --max-model-len "$MAX_LEN"
  --max-num-seqs "$MAX_SEQS"
  --kv-cache-dtype "$KV_DTYPE"
  --gpu-memory-utilization "$GPU_UTIL"
  --api-key "$API_KEY"
  --served-model-name "qwen3.6-27b" "$MODEL"
  --trust-remote-code
  "${EAGER_ARG[@]}"
  "${LIMIT_MM_ARG[@]}"
  "${CHUNKED_ARG[@]}"
  "${KV_OFFLOAD_ARG[@]}"
)

if [ "$DRY_RUN" = "1" ]; then
  for arg in "${CMD[@]}"; do
    printf '%s\n' "$arg"
  done
  if [ -n "$EXTRA" ]; then
    # shellcheck disable=SC2086
    printf '%s\n' $EXTRA
  fi
  exit 0
fi

# shellcheck disable=SC2086
nohup "${CMD[@]}" $EXTRA \
  >> "$LOG" 2>&1 &

echo $! > "$PIDFILE"
echo "[serve_qwen] started (pid $(cat "$PIDFILE"))"
echo "[serve_qwen] tail -f $LOG"
echo "[serve_qwen] OpenAI base_url -> http://$HOST:$PORT/v1"
