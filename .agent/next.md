# Loop 192 candidates

1. **`/checkpoints diff N`** — symmetric diff between current chat history and snapshot N (added/dropped messages by role + first chars).
2. **`format_turn_profile` honours TTY width** — wrap the tools table on narrow terminals.
3. **`/help` lists user-facing commands only by default; `--all` reveals admin** — once the table is bigger.
4. **Live vLLM smoke test** of `<tool_call>` protocol (opt-in, `pytest -m live`).
5. **Audit `apply_patch`** — does it have the same atomic-write story as direct fs_write?

(1) is the obvious next step in the checkpoint family.
