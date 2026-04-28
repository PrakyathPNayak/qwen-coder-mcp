# Next Loop Candidates

1. (P5) `main()` periodic summary every N iterations — print aggregate of `_LAST_SWALLOW_SUMMARY_COUNTS`.
2. (P5) Cache `iter_ts_monotonic` in `_iteration` and emit alongside `iter_ts` in timing.log.
3. (P6) `_dump_logger_state()` SIGUSR1 handler for runtime introspection.
4. (P5) Audit `_apply_diff` reject-path log calls.
5. (P7) `_strip_fence` nested triple-backticks (low risk).
6. (P6) `_RateLimitedSwallowLogger.last_log_message` — store the last emitted line for SIGUSR1 dump completeness.
