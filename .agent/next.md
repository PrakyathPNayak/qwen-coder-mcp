# Loop 183 candidates

1. **`/agent --resume`** — start an agentic turn with the latest checkpoint pre-loaded.
2. **Configurable rotation `keep`** — env var `QWEN_AGENT_ROTATION_KEEP`; currently hardcoded to 5.
3. **Auto-load latest checkpoint on TUI boot** — currently the user has to type `/resume` after a crash.
4. **`/lat n`** — show the n most recent turns, not just the last one (TurnProfile history ring buffer).
5. **Live vLLM smoke test** of `<tool_call>` protocol (opt-in, `pytest -m live`).

(3) is the natural follow-up to loop 181's fallback helper — the helper exists, just isn't wired into boot.
