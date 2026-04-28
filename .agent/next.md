# Next Loop Candidates

1. (P5) `STATE_MAX_BYTES` — hardcoded 256K; expose env override.
2. (P7) `_strip_fence` — handle nested triple-backticks within a fenced block.
3. (P5) Audit `_commit_and_push` for empty-diff race — `status --porcelain` is checked, but verify race with concurrent test fixture.
4. (P6) Add `revert_failed` to a stable outcome-category constant set, mirroring `APPLY_ERROR_CATEGORIES`.
5. (P6) `_abort_rebase_if_any` does a hard reset on any dirty tree at iteration start; document this in module docstring as the canonical recovery contract.
