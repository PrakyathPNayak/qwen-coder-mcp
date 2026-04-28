# Loop 196 candidates

1. **`format_turn_profile` honours TTY width** — wrap the tools table on narrow terminals.
2. **`/lat --format json`** — emit the ring buffer as JSON for downstream tooling.
3. **`/resume` prints a hint linking to `--since-resume`** — single line.
4. **Live vLLM smoke test** of `<tool_call>` protocol (opt-in, `pytest -m live`).
5. **Update `docs/AGENT_CHECKPOINTS.md`** to mention `diff` and `diff --since-resume`.

(5) is a small doc loop that captures the recovery family; (3) is a small UX nudge.
