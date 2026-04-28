# Loop 184 candidates

1. **`/agent --resume`** — start an agentic turn with the latest checkpoint pre-loaded.
2. **Configurable rotation `keep`** — env var `QWEN_AGENT_ROTATION_KEEP`; currently hardcoded to 5.
3. **`/lat n`** — show the n most recent turns (TurnProfile ring buffer).
4. **Atomic write of audit log** — same `.tmp + os.replace` treatment we gave checkpoints.
5. **Live vLLM smoke test** of `<tool_call>` protocol (opt-in, `pytest -m live`).

(2) is straightforward and removes the magic-number-5 from the TUI runner; users with long sessions will want larger rotation counts.
