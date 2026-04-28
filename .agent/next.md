# Loop 185 candidates

1. **`/agent --resume`** — start an agentic turn with the latest checkpoint pre-loaded.
2. **`/lat n`** — show the n most recent turns (TurnProfile ring buffer).
3. **Atomic write of audit log** — same `.tmp + os.replace` treatment we gave checkpoints.
4. **Document QWEN_AGENT_ROTATION_KEEP** in docs/LOCAL_SERVE.md or a config section.
5. **Live vLLM smoke test** of `<tool_call>` protocol (opt-in, `pytest -m live`).

(4) closes the doc loop on (this loop)'s env var so users can actually find it. Small but obviously coupled.
