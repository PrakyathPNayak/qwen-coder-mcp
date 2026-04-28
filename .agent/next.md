# Next Loop Candidates

1. (P6) `_apply_diff` — emit machine-parseable error category code (e.g. `unsafe_path`, `dir_conflict`) as stable field.
2. (P5) `_state.md` (state tracking) — also unbounded; `STATE_ARCHIVE_DIR` rotation exists, verify it's actually called.
3. (P7) `_strip_fence` — handle nested triple-backticks within a fenced block.
4. (P5) Audit `_commit_and_push` for empty-diff race when scope check passes but tree was already clean.
5. (P6) `_prune_history` is O(n) on every write; for large dirs consider a low-water-mark to amortize.
