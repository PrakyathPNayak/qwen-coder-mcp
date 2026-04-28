# Loop 214 candidates

1. **Generalise the chat-turn integration fixture** — the `_make_client(handler)` helper now lives in three test files (test_chat_stream, test_qwen_client, test_tui_chat_turn_e2e). Move to `tests/conftest.py` as a shared fixture.
2. `/checkpoints export N <path> --gzip`
3. `/sysinfo --json --probe` — active vLLM `/health` probe.
4. **Audit other places where error-message substring matching is case-sensitive** — the loop 213 bug suggests there may be siblings.
5. Live vLLM smoke test of `<tool_call>` syntax (env-dependent, carried since 164).

**Recommended:** (4) — same-class regression sweep while the lesson is fresh.
