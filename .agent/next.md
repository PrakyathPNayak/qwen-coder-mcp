# Next Loop Candidates

1. (P5) `_revert_changes` callers ignore the bool return; if a revert truly fails the next iteration may corrupt — propagate to outer loop as a hard skip.
2. (P5) `STATE_MAX_BYTES` — hardcoded 256K; expose env override.
3. (P7) `_strip_fence` — handle nested triple-backticks within a fenced block.
4. (P5) Audit `_commit_and_push` for empty-diff race.
5. (P6) `_iteration` — when `_revert_changes` returns False, log a structured outcome category (`revert_failed:{rel}`).
