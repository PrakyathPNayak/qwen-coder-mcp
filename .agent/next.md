# Next Loop Candidates

1. (P6) Document `_abort_rebase_if_any` + `_revert_changes` cascade as the canonical recovery contract in module docstring.
2. (P7) `_strip_fence` nested triple-backticks (low risk).
3. (P6) Audit other callers of `_log` for similar spam potential — `_run_git` failures, `_apply_diff` reject paths.
4. (P5) Cache `iter_ts_monotonic = time.monotonic()` in `_iteration` and emit alongside `iter_ts` in timing.log for sub-second analytics.
5. (P5) Periodic logger summary dump — at iteration boundaries, if any of the 3 module loggers has `suppressed > 0`, log a one-line summary (this delivers the "still failing" signal to runtime.log even when the rate limiter is suppressing).
6. (P6) `summary()` enables a future `_dump_logger_state()` SIGUSR1 handler — currently no signal handlers; could be a separate loop.
