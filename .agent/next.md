# Loop 179 candidates

1. **`/checkpoints` slash command** — list available `.agent/agent_state*.json` checkpoints with file mtimes; supports future multi-checkpoint history.
2. **Live vLLM smoke test of `<tool_call>` protocol** (opt-in, `pytest -m live`).
3. **Per-loop devil's advocate prompt** — `--devil` flag on `run_agent` that injects a critic turn before the final answer.
4. **Rotating checkpoints** — keep last N (configurable) under `.agent/checkpoints/<timestamp>.json` so a buggy run doesn't trash the previous state.
5. **`/lat` slash command** — print last-turn timing breakdown (TTFT + per-tool + summary) in tabular form.

(4) is the natural follow-up to loops 173/174 — single-file checkpointing has the obvious fragility that the latest crash overwrites the last good state. (5) surfaces the timings we just added into a queryable command for after-the-fact debugging.
