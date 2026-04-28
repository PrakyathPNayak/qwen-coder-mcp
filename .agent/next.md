# Loop 175 candidates

1. **Per-step latency in agent transcript.** Each `tool_call` event gets a `(2.4s)` suffix once the matching `tool_result` lands. Time-to-first-token + time-to-last-tool tracked.
2. **`/diff` slash command.** One-shot `git diff [HEAD|--cached|<ref>]` rendered with syntax highlighting; sandboxed via `shell_tools`.
3. **Live vLLM smoke test of `<tool_call>` protocol** (opt-in, `pytest -m live`).
4. **Per-loop devil's advocate prompt** — `--devil` flag on `run_agent` injecting a critic turn before the final answer.
5. **`/checkpoints` slash command** — list available checkpoints with timestamps; prep work for multi-checkpoint history.

(1) is the highest-impact: it surfaces tool latency that's otherwise invisible, which is the single most common debugging question users ask about agent runs ("why did that take so long?").
