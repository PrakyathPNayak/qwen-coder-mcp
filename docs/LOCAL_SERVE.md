# Local-serving Qwen3.6-27B on a single RTX 4090 (24 GB)

`Qwen3.6-27B` is a hybrid Gated-DeltaNet + Gated-Attention causal LM with a
vision encoder. In native FP16/BF16 it needs ~54 GB of weights, so it does
not fit on a 24 GB consumer card. We solve this with **4-bit AutoRound**,
served by **vLLM** (which speaks the OpenAI Chat Completions wire protocol
that the MCP server already targets).

## Default configuration

| Knob | Default | Notes |
| --- | --- | --- |
| Model | `Lorbus/Qwen3.6-27B-int4-AutoRound` | int4 weights, fp16 activations. ~14 GB on disk + GPU. |
| Backend | vLLM `>=0.7.2` | First-class support for hybrid arch + AutoRound. |
| Port | `8000` (HTTP, OpenAI-compatible) | `/v1/chat/completions`, `/v1/models`. |
| Context | `32768` (default), up to `262144` | Costs KV-cache memory; 32k is comfortable on 24 GB. |
| API key | `EMPTY` (any value accepted client-side) | Override with `QWEN_SERVE_API_KEY`. |
| Served name | `qwen3.6-27b` | What MCP server uses as `QWEN_MODEL`. |

## One-shot launch

```bash
# 1. start the local server (auto-installs vLLM into .venv-serve on first run)
./scripts/serve_qwen.sh
./scripts/wait_ready.sh             # blocks until /v1/models responds

# 2. launch the MCP server / agent loop pointing at the local endpoint
cp .env.example .env                 # already correct: localhost:8000, EMPTY key
./scripts/run_loop.sh                # detached agentic loop
tail -f .loop/runtime.log
```

To stop everything:

```bash
./scripts/stop_qwen.sh
kill "$(cat .loop/loop.pid)"
```

## VRAM budget on a 4090

| Model variant | Approx. weights | Fits 24 GB? | Notes |
| --- | --- | --- | --- |
| `Qwen/Qwen3.6-27B` (BF16) | ~54 GB | ❌ | requires multi-GPU |
| `Qwen/Qwen3.6-27B-FP8` | ~27 GB | ❌ (KV cache pushes it over) | works on 32 GB cards |
| `Lorbus/Qwen3.6-27B-int4-AutoRound` | ~14 GB | ✅ | recommended default here |
| `unsloth/Qwen3.6-27B-GGUF` Q4_K_M | ~16 GB | ✅ via llama.cpp | see fallback below |

## Fallback: llama.cpp

If you cannot install vLLM (e.g. PyTorch CUDA mismatch), use llama.cpp's
built-in OpenAI-compatible server with the unsloth GGUFs:

```bash
# install once
pipx install llama-cpp-python[server]    # or build llama.cpp from source

# pull a Q4_K_M GGUF
huggingface-cli download unsloth/Qwen3.6-27B-GGUF \
  Qwen3.6-27B-Q4_K_M.gguf --local-dir ./models

# serve OpenAI-compatible on :8000
python -m llama_cpp.server \
  --model ./models/Qwen3.6-27B-Q4_K_M.gguf \
  --n_gpu_layers -1 \
  --n_ctx 32768 \
  --host 127.0.0.1 --port 8000
```

The MCP server / agent loop don't care which backend serves them — they only
require `/v1/chat/completions`.

## Troubleshooting

- **OOM at startup (CUDA out of memory during warmup / KV cache allocation)**:
  the defaults already enforce eager mode, fp8 KV cache, and `max_num_seqs=4`
  on a 24 GB 4090. If it still OOMs, drop further:
  - `QWEN_SERVE_MAX_LEN=2048` (less KV cache per slot)
  - `QWEN_SERVE_MAX_SEQS=1` (single-slot KV cache)
  - `QWEN_SERVE_GPU_UTIL=0.75`
  - `QWEN_SERVE_KV_DTYPE=fp8` is already the default; keep it.
  - `QWEN_SERVE_EAGER=1` is already the default; keep it (skips CUDA graph
    capture which briefly doubles peak memory).
  The script also exports `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`
  which the OOM error itself recommends; it reduces fragmentation.
- **`unknown architecture qwen3_5`**: upgrade vLLM (`pip install -U vllm`)
  inside `.venv-serve`. Hybrid Gated-DeltaNet support landed in 0.7.x.
- **Slow first token**: vLLM compiles CUDA graphs on the first request; the
  agent loop's first iteration may take 30-60 s. With `--enforce-eager` the
  first token is faster but throughput is slightly lower.
- **Port already in use**: `QWEN_SERVE_PORT=8001 ./scripts/serve_qwen.sh`
  and set `QWEN_BASE_URL=http://127.0.0.1:8001/v1` in `.env`.
