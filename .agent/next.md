# Loop 180 candidates

1. **`/checkpoints` slash command** — list rotated snapshots with file mtimes; `/checkpoints load <n>` rehydrates a specific one.
2. **`/lat` slash command** — print last-turn timing breakdown (TTFT + per-tool list + summary) in tabular form using the events emitted in loops 175-178.
3. **Live vLLM smoke test of `<tool_call>` protocol** (opt-in, `pytest -m live`).
4. **Per-loop devil's advocate prompt** — `--devil` flag on `run_agent` injecting a critic turn before the final answer.
5. **Agent transcript pretty-print to stdout** — when running `/agent` outside the TUI (e.g. from the MCP server), produce a stable text rendering.

(1) is the obvious follow-up — the rotation infrastructure landed in this loop has no UI surface yet.
