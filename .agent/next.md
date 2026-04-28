# Loop 174 candidates

1. **`/resume` slash command.** Read `.agent/agent_state.json` and rehydrate `self.history`, with a confirm modal showing message count + last assistant snippet.
2. **Spinner + per-step latency in agent transcript.** Each `tool_call` event gets a `(2.4s)` suffix once the matching `tool_result` lands.
3. **`/diff` slash command.** One-shot `git diff HEAD` / `git diff --cached` rendered with syntax highlighting, sandboxed via shell_tools.
4. **Live vLLM smoke test of `<tool_call>` protocol** (opt-in, `pytest -m live`).
5. **Per-loop devil's advocate prompt** — `--devil` flag on `run_agent`.

(1) is the natural follow-up to loop 173 — the checkpoints are useless without a way to load them back.
