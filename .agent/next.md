# Loop 198 candidates

1. **`format_turn_profile` honours TTY width** — wrap the tools table on narrow terminals.
2. **`/lat --format json`** — emit the ring buffer as JSON for downstream tooling.
3. **`format_history_diff` derives preview width from terminal** — currently hardcoded 60.
4. **Live vLLM smoke test** of `<tool_call>` protocol (opt-in, `pytest -m live`).
5. **`/resume --preview --inline`** — let preview accept --inline like the diff command does.
