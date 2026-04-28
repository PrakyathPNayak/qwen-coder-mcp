# Loop 178 candidates

1. **Time-to-first-token (TTFT) on model turns.** Track time from prompt-flush to first chunk; emit `AgentEvent(kind="ttft", latency_s=...)` right after the first chunk of each model turn.
2. **`/checkpoints` slash command** — list available checkpoints with timestamps under `.agent/`, prepping for multi-checkpoint history.
3. **Live vLLM smoke test of `<tool_call>` protocol** (opt-in, `pytest -m live`).
4. **Per-loop devil's advocate prompt** — `--devil` flag on `run_agent` injecting a critic turn before final answer.
5. **Surface summary in MCP server response** — the JSON-RPC streaming endpoint can include the summary line in its terminal frame so non-TUI clients render it identically.

(5) is the natural next step in the observability arc — propagate the new event kind through the existing MCP wire format.
