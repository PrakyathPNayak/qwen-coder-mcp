# Loop 176 candidates

1. **`/diff` slash command.** One-shot `git diff [HEAD|--cached|<ref>]` rendered with syntax highlighting; sandboxed via shell_tools.
2. **Live vLLM smoke test of `<tool_call>` protocol** (opt-in, `pytest -m live`).
3. **Time-to-first-token tracking** alongside the per-tool latency we just added; shows model-side latency separately from tool-side.
4. **Per-loop devil's advocate prompt** — `--devil` flag on `run_agent`.
5. **`/checkpoints` slash command** — list available checkpoints with timestamps.

(1) is the natural next visible UX win; tool-call latency was a similar visibility fix.
