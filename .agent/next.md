# Next Loop Candidates

1. (P5) Cache `iter_ts_monotonic` in `_iteration` and emit alongside `iter_ts` in timing.log.
2. (P6) `_dump_logger_state()` SIGUSR1 handler for runtime introspection.
3. (P5) Audit `_apply_diff` reject-path log calls.
4. (P7) `_strip_fence` nested triple-backticks.
5. (P6) `_RateLimitedSwallowLogger.last_log_message` — store last emitted line for SIGUSR1 dump.
6. (P5) Test `main()` calls `_log_aggregate_swallow_summary` at the right cadence (currently only the helper is tested).
