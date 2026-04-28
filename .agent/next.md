# Next Loop Candidates

1. (P7) `_strip_fence` — handle nested triple-backticks within a fenced block.
2. (P5) Audit `_commit_and_push` for empty-diff race when scope passes but tree was clean.
3. (P6) Generalize `_prune_state_archive` + `_prune_history` (identical shape) into one helper.
4. (P6) `state.md` (root, not archive) — `STATE_MAX_BYTES` is hardcoded 256K; expose env override.
5. (P5) `_revert_changes` — check return code; today it's fire-and-forget.
