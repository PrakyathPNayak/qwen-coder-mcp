# Next Loop Candidates

1. (P6) `.loop/history/*.md` — old rejected/applied history accumulates; add retention policy (keep last N or last X bytes total).
2. (P6) `_apply_diff` — emit machine-parseable error category for log-aggregation.
3. (P7) `_strip_fence` — handle nested triple-backticks within a fenced block.
4. (P5) Audit `_commit_and_push` for empty-diff race.
5. (P6) Extract a single env-int parser used by `_runtime_log_max_bytes`, `_timing_max_bytes`, and `_env_timeout_seconds` (3 near-duplicate helpers).
