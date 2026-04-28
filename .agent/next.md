# Loop 197 candidates

1. **`format_turn_profile` honours TTY width** — wrap the tools table on narrow terminals.
2. **`/lat --format json`** — emit the ring buffer as JSON for downstream tooling.
3. **`/resume` prints a hint linking to `--since-resume`** — single line.
4. **Live vLLM smoke test** of `<tool_call>` protocol (opt-in, `pytest -m live`).
5. **`format_history_diff` honours preview width** — currently hardcoded 60 chars; could derive from terminal width.

(3) is small (~20 LoC + 3 tests); (1) is meatier.
