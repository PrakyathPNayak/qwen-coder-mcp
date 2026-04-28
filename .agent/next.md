# Next Loop Candidates

1. (P6) `_iteration` — log time spent per phase to `.loop/timing.log` for budget tuning.
2. (P5) `_has_dir_path_conflict` — also reject diffs *deleting* a path that is currently a directory.
3. (`_strip_fence`) P7 handle nested triple-backticks within a fenced block. 
4. (P6) `_apply_diff` — emit machine-parseable error category for log-aggregation.
5. (P5) Audit `_commit_and_push` for empty-diff race when `_diff_in_scope` has been overridden in tests.
