# Next Loop Candidates

1. (P6) Document `_abort_rebase_if_any` + `_revert_changes` cascade as canonical recovery contract in module docstring (still pending across loops).
2. (P5) `main()` periodic summary every N iterations — print aggregate of `_LAST_SWALLOW_SUMMARY_COUNTS` for long runs.
3. (P5) Cache `iter_ts_monotonic` in `_iteration` and emit alongside `iter_ts` in timing.log.
4. (P6) Audit `_run_git` timeout (line 287) — same spam class but timeout is rarer.
5. (P7) `_strip_fence` nested triple-backticks (low risk).
6. (P6) Consider `_RateLimitedSwallowLogger.report` returning bool (whether it logged).
7. (P5) Audit `_abort_rebase_if_any` `_log` calls if any.
