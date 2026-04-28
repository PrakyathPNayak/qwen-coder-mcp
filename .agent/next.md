# Loop 191 candidates

1. **`/checkpoints diff N`** — diff between current chat history and snapshot N. Show added/dropped messages by role + first chars.
2. **`format_turn_profile` honours TTY width** — wrap the tools table on narrow terminals.
3. **Audit `save_agent_state` (the mid-flight per-tool-call checkpoint)** — verify it's atomic, harden if not.
4. **`/help` honours `--search <term>`** — substring filter over the help table.
5. **Live vLLM smoke test** of `<tool_call>` protocol (opt-in, `pytest -m live`).

(1) is the meatiest and gives users a real recovery decision tool. Lean (1) next.
