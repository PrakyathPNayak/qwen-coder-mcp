# Loop 202 candidates

1. **`format_turn_profile` honours TTY width** — wrap tools table on narrow terminals (carried, the meatier UX gap).
2. **`/checkpoints export N <path> --gzip`** — compressed archives (loop-199 follow-on).
3. **`format_history_diff` derives preview width from terminal**.
4. **`/tokens --json --top K`** — top-K heaviest messages instead of full list, for very long histories.
5. **Live vLLM smoke test** — environment-dependent; defer.

JSON-export trilogy is now complete (lat/sysinfo/tokens). Next move: (1) for a real UX fix or (2) for a natural follow-on.
