# Next Loop Candidates

1. (P7) `_strip_fence` — handle nested triple-backticks within a fenced block.
2. (P5) Audit `_commit_and_push` for empty-diff race.
3. (P6) Add `revert_failed` and other outer-loop outcomes to a stable category constant set, mirroring `APPLY_ERROR_CATEGORIES`.
4. (P6) Document `_abort_rebase_if_any` as canonical recovery contract in module docstring.
5. (P5) `_run_git` has no timeout per-call long network ops (push, pull) could wedge an iteration past the budget. 
