# Loop 173 candidates

1. **Auto-checkpoint agent state.** Persist transcript + tool history to `.agent/agent_state.json` every N agent turns (configurable via `/agent_checkpoint N`) so an interrupt mid-multi-step run isn't fatal.
2. **Live vLLM smoke test of `<tool_call>` protocol.** Opt-in integration test (`pytest -m live`) that hits a running server with TOOL_PROTOCOL_DOC and asserts at least one `<tool_call>` block.
3. **Per-loop devil's advocate prompt** — `--devil` flag on `run_agent` injecting a critic turn before the final answer.
4. **`/diff` slash command** — one-shot `git diff HEAD` or `git diff --cached` rendered with syntax highlighting.
5. **Spinner + per-step latency in agent transcript.** Each tool_call gets a "(2.4s)" suffix once the result lands.

Pick highest impact next loop. (1) is probably the winner — it makes the agent recoverable, which compounds.
