# Loop 221 candidates

1. **`QWEN_SERVE_FORCE_OFFLOAD=1` escape hatch** — let operators
   override the loop-216 hybrid guard for misnamed dense models.
2. **`/checkpoints export N <path> --gzip`** — long-deferred.
3. **TUI prefix-buffering for unwrapped streaming** — handle the
   loop-218-deferred unwrapped-`</think>` case in chat_stream by
   buffering the first N chars of any response.
4. **`/sysinfo --probe --json` symmetry audit** — verify the JSON
   path emits a parseable structure on probe failure.
5. **Real-model E2E adds `<tool_call>` invocation** — the loop-217
   real-model test asserts PINGOK reply but doesn't drive a real
   tool-call round trip (model emits `<tool_call>`, agent_loop
   parses it, fs_tool fires, result fed back). High-leverage live
   integration test.

**Recommended:** (5) — the loop-217 live run already proved the
model emits parseable `<tool_call>` JSON; promoting that into a
gated regression test would catch any future drift in the prompt
or model behaviour.
