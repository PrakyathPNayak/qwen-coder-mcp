# Next Loop Candidates

1. (P5) `main()` periodic summary every N iterations — print aggregate of `_LAST_SWALLOW_SUMMARY_COUNTS`.
2. (P5) Cache `iter_ts_monotonic` in `_iteration` and emit alongside `iter_ts` in timing.log.
3. (P7) `_strip_fence` nested triple-backticks (low risk).
4. (P6) `_RateLimitedSwallowLogger.report` returns bool whether it logged.
5. (P6) `_dump_logger_state()` SIGUSR1 handler for runtime introspection.
6. (P5) Audit `_apply_diff` reject-path log calls.
7. (P5) Cache `_swallow_loggers()` tuple — currently rebuilt per call. Tiny win but clean.
