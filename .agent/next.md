# Loop 199 candidates

1. **`format_turn_profile` honours TTY width** — wrap the tools table on narrow terminals.
2. **`format_history_diff` derives preview width from terminal** — currently hardcoded 60.
3. **Live vLLM smoke test** of `<tool_call>` protocol (opt-in, `pytest -m live`).
4. **`/checkpoints export N <path>`** — write the rotation N to an arbitrary file (lets users archive snapshots before pruning).
5. **`/sysinfo --json`** — same JSON treatment for /sysinfo.

(4) is the natural next checkpoint feature; (1) is a real cosmetics gap.
