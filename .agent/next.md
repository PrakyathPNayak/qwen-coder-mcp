# Next Loop Candidates

1. (P5) `main()` periodic summary every N iterations — print aggregate of `_LAST_SWALLOW_SUMMARY_COUNTS`.
2. (P5) Cache `iter_ts_monotonic` in `_iteration` and emit alongside `iter_ts` in timing.log.
3. (P6) Audit `_run_git` timeout (line 287) — same spam class but timeout is rarer.
4. (P7) `_strip_fence` nested triple-backticks (low risk).
5. (P6) Consider `_RateLimitedSwallowLogger.report` returning bool.
6. (P6) `_dump_logger_state()` handler for SIGUSR1 — runtime introspection of all `_swallow_loggers()`.
7. (P5) Audit `_apply_diff` reject-path log calls.
