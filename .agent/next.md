# Next Loop Candidates

1. (P6) `_apply_diff` — emit machine-parseable error category for log-aggregation (currently free-form prefix).
2. (P7) `_strip_fence` — handle nested triple-backticks within a fenced block.
3. (P5) `_iteration` — early-exit returns (no_candidate_files, skip) currently write no timing record; either OK (no rel) or add separate counter.
4. (P5) Audit `_commit_and_push` for empty-diff race when `_diff_in_scope` would have caught it.
5. (P6) `.loop/timing.log` rotation — file grows unboundedly across days.
