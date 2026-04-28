# Next Loop Candidates

1. (P6) `_dump_logger_state()` SIGUSR1 handler for runtime introspection.
2. (P5) Audit `_apply_diff` reject-path log calls.
3. (P7) `_strip_fence` nested triple-backticks.
4. (P6) `_RateLimitedSwallowLogger.last_log_message` for SIGUSR1 dump.
5. (P5) Test `main()` calls `_log_aggregate_swallow_summary` at the right cadence.
6. (P5) Add `wall_s` analytics example (CLI script that parses timing.log) to README/docs.
7. (P5) `wall_s` could be < `sum(phases.values())` if monotonic skewed; sanity-check test that wall_s >= sum(phases) for healthy iterations.
