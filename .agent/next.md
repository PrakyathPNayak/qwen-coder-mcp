# Loop 193 candidates

1. **Per-message line diff in `/checkpoints diff`** — when role matches and content differs, show a unified-diff fragment instead of just a `~` marker.
2. **`format_turn_profile` honours TTY width** — wrap the tools table on narrow terminals.
3. **`/checkpoints diff --since-resume`** — auto-pick the snapshot that `/resume` would load.
4. **`apply_patch` atomicity audit** — does it stage writes via .tmp like fs_write?
5. **Live vLLM smoke test** of `<tool_call>` protocol (opt-in, `pytest -m live`).

(1) builds directly on this loop's renderer; (3) is a small ergonomics win.
