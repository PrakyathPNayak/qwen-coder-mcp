# Loop 205 candidates

1. **Terminal-width awareness for `_format_checkpoint_listing`** — same UX gap as loops 202/203/204 but at the listing render site. Long snapshot names + ISO-8601 + size column overflow on narrow terminals.
2. **`/checkpoints export N <path> --gzip`** — compressed archives.
3. **`/tokens --json --top K`** — top-K heaviest messages.
4. **Live vLLM smoke test** — environment-dependent; defer.
5. **`/help <term> --regex`** — escape hatch for searching for `/c.*ts`-style patterns.

(1) is the natural finisher to the terminal-width arc. After that the obvious-pool starts to thin and (2)/(3) are reasonable next moves.
