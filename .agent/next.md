# Loop 204 candidates

1. **Wire dispatcher to use `preview_chars=None`** — loop 203 added auto mode but `/checkpoints diff` still relies on the 60-default. Switching it makes the auto-mode actually visible to users.
2. **Terminal-width awareness for `_format_checkpoint_listing`** — same UX gap, different render site.
3. **`/checkpoints export N <path> --gzip`** — compressed archives.
4. **`/tokens --json --top K`** — top-K heaviest messages.
5. **Live vLLM smoke test** — environment-dependent.

(1) is the natural follow-on — without it loop 203's work is dead code from the user's perspective.
