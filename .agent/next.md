# Loop 222 candidates

1. **Real-model E2E with `<tool_call>` round trip** — promote the
   loop-217 live observation into a regression test: drive a
   prompt → model emits `<tool_call>` JSON → agent_loop parses →
   fs_tool fires → result back to model.
2. **`/checkpoints export N <path> --gzip`** — long-deferred
   variant of the existing export; trivial atomic-write extension.
3. **TUI prefix-buffering for unwrapped streaming `</think>`** —
   the loop-218-deferred unwrapped case; buffer first ~9 chars of
   any streamed response so we can still suppress a leading bare
   `</think>`.
4. **`/sysinfo --probe --json` failure-shape audit** — verify the
   JSON path emits a parseable structure when the engine probe
   fails (no None.get crashes, schema stable).
5. **`scripts/stop_qwen.sh` test** — currently no pytest pin on
   stop semantics; symmetry with the `serve_qwen.sh` dry-run test.

**Recommended:** (5) — small, isolated, fills the obvious gap
left by the loop-205 dry-run testing pattern. Then (1) for the
heavyweight live integration.
