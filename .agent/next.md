# Next Loop Candidates

1. (P6) `out_of_scope`, `validation_failed`, `commit_failed`, `revert_failed` outcomes — wrap in a single OUTER_OUTCOME_CATEGORIES frozenset mirroring APPLY_ERROR_CATEGORIES so the outer loop has the same machine-grepable taxonomy.
2. (P7) `_strip_fence` — handle nested triple-backticks within a fenced block (low-priority, current regex is safe vs typical diff context lines).
3. (P5) Audit `_commit_and_push` for empty-diff race.
4. (P6) Document `_abort_rebase_if_any` as canonical recovery contract in module docstring.
5. (P7) `_INNER_FENCE_RE` accepts only `[a-zA-Z0-9_+\-]` for lang tag — won't match `c++` (has `+`, fine) or `objective-c` (fine), but fails on lang tags with `.` or spaces. Low risk for diff payloads.
