# Loop 200 candidates

1. **`format_turn_profile` honours TTY width** — wrap tools table on narrow terminals.
2. **`/sysinfo --json`** — same JSON treatment for sysinfo output.
3. **`/checkpoints export N <path> --gzip`** — compressed archives.
4. **`format_history_diff` derives preview width from terminal**.
5. **Live vLLM smoke test** — environment-dependent; deferring.

(2) is the natural follow-on to loop 198's --json work and small. (1) is the meatier UX fix.
