# Loop 195 candidates

1. **`format_turn_profile` honours TTY width** — wrap the tools table on narrow terminals.
2. **`/checkpoints diff --since-resume`** — auto-pick the snapshot that `/resume` would load.
3. **`/lat --format json`** — emit the ring buffer as JSON for downstream tooling.
4. **Live vLLM smoke test** of `<tool_call>` protocol (opt-in, `pytest -m live`).
5. **`save_history_jsonl` parent fsync** — strict durability of the rename itself (carry from loop 194's devil step).
