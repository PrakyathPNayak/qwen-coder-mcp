# Next Loop Candidates

1. (`STATE_MAX_BYTES`) P5 hardcoded 256K; expose env override (`QWEN_STATE_MAX_BYTES`). 
2. (P7) `_strip_fence` — handle nested triple-backticks within a fenced block.
3. (P5) Audit `_commit_and_push` for empty-diff race.
4. (P5) `_revert_changes` — fire-and-forget; no rc check.
5. (P6) `_iteration_log_max_files` — count of history files in any prune-call helper now relies on `iterdir()`; for very large dirs (>10k) this is slow.
