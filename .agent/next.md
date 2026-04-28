# Loop 203 candidates

1. **`format_history_diff` derives preview width from terminal** — the preview_chars parameter is hardcoded 60; should track terminal width via the same shutil pattern loop 202 just established.
2. **`/checkpoints export N <path> --gzip`** — compressed archives.
3. **`/tokens --json --top K`** — top-K heaviest messages for very long histories.
4. **Terminal-width awareness for `_format_checkpoint_listing`** — same UX gap, different rendering site.
5. **Live vLLM smoke test** — environment-dependent; defer.

(1) is the natural next step — directly mirrors loop 202's pattern in another renderer.
