# Loop 177 candidates

1. **Time-to-first-token (TTFT) on model turns.** Track `time.monotonic()` of the first `chunk` event after each `tool_result` (or task start); emit a synthetic `AgentEvent(kind="ttft", latency_s=...)` so consumers can show "model started replying in 0.4s" alongside per-tool timing.
2. **`/checkpoints` slash command** — list available checkpoints with timestamps, supporting future multi-checkpoint history.
3. **Live vLLM smoke test of `<tool_call>` protocol** (opt-in, `pytest -m live`).
4. **Per-loop devil's advocate prompt** — `--devil` flag on `run_agent`.
5. **Aggregate latency summary at end of agent run** — sum per-tool latencies and emit a final `[agent] used 5 tools, total tool time 3.2s` line.

(5) is small + complementary to loops 175/176. (1) extends timing coverage but needs a new event kind. Pick (5) next loop for low-risk continuation.
