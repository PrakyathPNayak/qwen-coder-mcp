# Loop 220 candidates

1. **`QWEN_SERVE_FORCE_OFFLOAD=1` escape hatch** — let operators
   override the loop-216 hybrid guard for misnamed dense models.
2. **`/checkpoints export N <path> --gzip`** — long-deferred.
3. **TUI prefix-buffering for unwrapped streaming** — handle the
   loop-218-deferred unwrapped-`</think>` case in chat_stream by
   buffering the first N chars of any response. Trade initial
   latency for correctness on edge cases.
4. **Surface preflight probe in TUI startup banner** — loop 219
   added the probe to the headless loop; the TUI startup could
   show the same line so users know the backend is alive before
   sending the first message.
5. **`/sysinfo --probe --json`** symmetry audit — loop 215 added
   the probe to text mode; verify the JSON path still emits a
   parseable structure when probe returns `ok=False`.

**Recommended:** (4) — directly extends the loop-219 work and is
small, concrete, user-visible.
