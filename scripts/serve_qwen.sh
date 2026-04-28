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
#   QWEN_SERVE_MAX_LEN      max model context (default 2048)
#   QWEN_SERVE_GPU_UTIL     gpu memory utilization (default 0.92)
#   QWEN_SERVE_MAX_SEQS     max concurrent sequences (default 1)
#   QWEN_SERVE_KV_DTYPE     kv cache dtype (default fp8)
#   QWEN_SERVE_EAGER        enforce eager mode (default 1)
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
# (~14 GB) plus KV cache headroom. The OOM during warmup is almost
# always KV cache: vLLM reserves max_num_seqs * max_model_len tokens,
# and CUDA graph capture briefly doubles peak memory. We default to a
# small max_num_seqs (4) and enforce-eager so a fresh boot survives on
# a single 4090. Override with the env vars above for cards with more
# memory or multi-user setups.
MAX_LEN="${QWEN_SERVE_MAX_LEN:-2048}"
GPU_UTIL="${QWEN_SERVE_GPU_UTIL:-0.92}"
MAX_SEQS="${QWEN_SERVE_MAX_SEQS:-1}"
KV_DTYPE="${QWEN_SERVE_KV_DTYPE:-fp8}"
EAGER="${QWEN_SERVE_EAGER:-1}"
DTYPE="${QWEN_SERVE_DTYPE:-auto}"
API_KEY="${QWEN_SERVE_API_KEY:-EMPTY}"
EXTRA="${QWEN_SERVE_EXTRA:-}"
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

echo "[serve_qwen] model=$MODEL host=$HOST port=$PORT"
echo "[serve_qwen] max_len=$MAX_LEN gpu_util=$GPU_UTIL max_seqs=$MAX_SEQS kv_dtype=$KV_DTYPE eager=$EAGER limit_mm=$LIMIT_MM"
echo "[serve_qwen] PYTORCH_CUDA_ALLOC_CONF=$PYTORCH_CUDA_ALLOC_CONF"
echo "[serve_qwen] logs -> $LOG"

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
