# Next Loop Candidates

1. (P6) Document `_abort_rebase_if_any` + `_revert_changes` cascade as canonical recovery contract in module docstring.
2. (P7) `_strip_fence` nested triple-backticks (low risk).
3. (P5) Cache `iter_ts_monotonic` in `_iteration` and emit alongside `iter_ts` in timing.log for sub-second analytics.
4. (P5) `main()` periodic summary — every N iterations print aggregate of `_LAST_SWALLOW_SUMMARY_COUNTS` so runs with no per-iteration growth still show historical totals.
5. (P5) Audit `_run_git` failure log calls — rare but if origin/network breaks, push/pull spam every iteration. Convert to rate-limited.
6. (P6) Consider `_RateLimitedSwallowLogger.report` returning `bool` (whether it logged) for callers that want to do additional "first-time" work.
