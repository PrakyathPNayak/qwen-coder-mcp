# Loop 218 candidates

1. **Streaming-mode `<think>` stripping** — `QwenClient.chat_stream`
   yields raw chunks and currently leaks think tokens. Need a
   stateful filter that tracks the open/close-tag state across chunk
   boundaries (a `</think>` may split across two chunks). High value
   for the TUI which streams.
2. **`QWEN_SERVE_FORCE_OFFLOAD=1` escape hatch** for false-positive
   hybrid-name matches in `serve_qwen.sh`.
3. **`/checkpoints export N <path> --gzip`** — long-deferred.
4. **agent_loop pre-flight `/health` probe** — same probe as
   `/sysinfo --probe`, but in the autonomous loop's startup so it
   self-detects engine readiness instead of timing out chat.
5. **Audit `agent_loop` for any *other* spot that consumes
   `qwen_client` output without going through `_extract_text`** —
   ensure the strip applies everywhere the chain-of-thought could
   leak into a parser.

**Recommended:** (1) — same bug class as loop 217 but on the
streaming path. The TUI uses streaming, so this directly affects
the user-facing experience.
