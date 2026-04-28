# Next Loop Candidates

1. (P5) `_dump_logger_state()` SIGUSR1 handler — now actionable since `last_log_message` exists.
2. (P5) Audit `_apply_diff` reject-path log calls — likely missing rate limiting on some malformed-diff paths.
3. (P6) `_strip_fence` nested triple-backticks in code blocks.
4. (P5) Test `main()` calls `_log_aggregate_swallow_summary` at the right cadence.
5. (P5) Add `wall_s` analytics example (CLI) to README.
6. (P6) Sanity test wall_s >= sum(phases) for healthy iterations.
7. (P6) `_log_aggregate_swallow_summary` should include `last_log_message` for each logger when verbosity is high (env-tunable).
