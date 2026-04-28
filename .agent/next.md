# Loop 194 candidates

1. **`format_turn_profile` honours TTY width** — wrap the tools table on narrow terminals.
2. **`/checkpoints diff --since-resume`** — auto-pick the snapshot that `/resume` would load.
3. **`apply_patch` atomicity audit** — does it stage writes via .tmp like fs_write?
4. **Live vLLM smoke test** of `<tool_call>` protocol (opt-in, `pytest -m live`).
5. **`/lat --format json`** — emit the ring buffer as JSON for downstream tooling.

(2) is small, finishes the diff family. (3) is a real-integrity pick.
