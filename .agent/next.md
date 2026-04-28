# Loop 215 candidates

1. `/checkpoints export N <path> --gzip` — long-deferred utility.
2. `/sysinfo --json --probe` — active vLLM `/health` probe (would have caught loops 205 & 211 lock-step bugs in production sooner).
3. **Dedupe the test_qwen_client.py inline MockTransport assemblies** — three sites at lines 42/572/645 still construct httpx clients by hand. Check if they fit the loop-214 helper or genuinely need bespoke shapes.
4. **Add a TUI-level integration test for streaming + checkpoint/save flow** — currently chat_turn_stream + checkpoint save are tested in isolation.
5. Live vLLM smoke test of `<tool_call>` syntax (env-dependent, carried since 164).

**Recommended:** (2) — it's been on the candidate list for many loops; closes the loop-205/211 reactive-detection gap with proactive runtime probe.
